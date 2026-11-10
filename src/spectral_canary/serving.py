"""Inference gateway core: verified model pool, deterministic router, shadow/canary execution, faults.

In-flight safety: each request captures one immutable ``Snapshot`` (deployment pointer + loaded model
objects) at its start. A promotion or rollback replaces the snapshot reference atomically; requests that
already hold the old snapshot finish on the models they started with, and loaded models are never
unloaded while referenced, so a pointer swap cannot crash an in-flight request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import random
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import artifacts as art
from . import fastscore
from . import telemetry as tm
from .paths import StatePaths
from .policy import ReleasePolicy
from .schema import encode_records, validate_record
from .store import Deployment, Store

log = logging.getLogger("scw.serving")
_THREADS_LIMITED = False


def limit_native_threads() -> None:
    """Cap OpenMP/BLAS pools at 1 thread for this process (works even if sklearn is already imported).

    Single-row inference gains nothing from threads; on a shared machine OpenMP spin-waiting made a
    calibrated HGB call ~95 ms p50 instead of ~5 ms (measured; see docs/LOAD_TEST_RESULTS.md).
    """
    global _THREADS_LIMITED
    if not _THREADS_LIMITED:
        from threadpoolctl import threadpool_limits

        threadpool_limits(limits=int(os.environ.get("OMP_NUM_THREADS", "1")))
        _THREADS_LIMITED = True


@dataclass(frozen=True)
class LoadedModel:
    version: str
    estimator: Any
    threshold: float
    sha256: str
    # Bit-identical single-row scorer built at load (fastscore.py); None -> sklearn for every call.
    fast: Callable[[Sequence[float]], float] | None = field(default=None, compare=False, repr=False)
    scoring_path: str = "sklearn"

    def score(self, X: np.ndarray) -> np.ndarray:
        if self.fast is not None and X.shape[0] == 1:
            return np.array([self.fast(X[0].tolist())], dtype=np.float64)
        return self.estimator.predict_proba(X)[:, 1]


class ModelPool:
    """Loads registered versions after verifying hash, signature and type allowlist; caches them."""

    def __init__(self, store: Store, paths: StatePaths):
        self.store = store
        self.paths = paths
        self._models: dict[str, LoadedModel] = {}
        self._lock = threading.Lock()
        self.load_failures: dict[str, str] = {}

    def get(self, version: str) -> LoadedModel:
        with self._lock:
            m = self._models.get(version)
        if m is not None:
            return m
        rec = self.store.get_model(version)
        if rec is None:
            raise art.ArtifactError(f"unknown_version: {version}")
        if not art.verify_signature(rec["manifest"], rec["signature"], art.signing_key(self.paths)):
            raise art.ArtifactError("manifest_signature_invalid")
        try:
            est = art.load_verified(Path(rec["artifact_path"]), rec["artifact_sha256"])
        except art.ArtifactError as exc:
            self.load_failures[version] = str(exc)
            tm.MODEL_LOAD_FAILURES.labels(model_version=version).inc()
            raise
        fast, scoring_path = fastscore.build(est)
        if fast is None:
            log.info("model %s: single-row scoring via sklearn (%s)", version, scoring_path)
        m = LoadedModel(
            version,
            est,
            float(rec["manifest"]["threshold"]["value"]),
            rec["artifact_sha256"],
            fast=fast,
            scoring_path=scoring_path,
        )
        with self._lock:
            self._models.setdefault(version, m)
            self.load_failures.pop(version, None)
            return self._models[version]

    def reverify(self, version: str) -> None:
        """Re-hash the artifact on disk even if cached (used before every promotion)."""
        rec = self.store.get_model(version)
        if rec is None:
            raise art.ArtifactError(f"unknown_version: {version}")
        if art.sha256_file(Path(rec["artifact_path"])) != rec["artifact_sha256"]:
            raise art.ArtifactError("artifact_hash_mismatch")

    def evict(self, version: str) -> None:
        with self._lock:
            self._models.pop(version, None)

    def loaded_versions(self) -> list[str]:
        with self._lock:
            return sorted(self._models)

    def scoring_paths(self) -> dict[str, str]:
        """Per loaded version: "compiled" (fastscore) or why it scores through sklearn."""
        with self._lock:
            return {v: m.scoring_path for v, m in sorted(self._models.items())}


# -- simulated faults (test doubles, always labeled) -------------------------------------------------------
class SimulatedFault(RuntimeError):
    """An error raised on purpose by the chaos fault injector. Never a real model failure."""


@dataclass
class Fault:
    latency_ms: float = 0.0
    error_rate: float = 0.0
    seed: int = 0
    note: str = ""


class FaultInjector:
    """Per-version faults. Decisions are deterministic per request key so replays are reproducible."""

    def __init__(self) -> None:
        self._faults: dict[str, Fault] = {}
        self._lock = threading.Lock()

    def set(self, version: str, fault: Fault) -> None:
        with self._lock:
            self._faults[version] = fault

    def clear(self, version: str | None = None) -> None:
        with self._lock:
            if version is None:
                self._faults.clear()
            else:
                self._faults.pop(version, None)

    def active(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {v: f.__dict__.copy() for v, f in self._faults.items()}

    def apply(self, version: str, request_key: str) -> None:
        with self._lock:
            f = self._faults.get(version)
        if f is None:
            return
        if f.latency_ms > 0:
            time.sleep(f.latency_ms / 1000.0)
        if f.error_rate > 0:
            r = random.Random(f"{f.seed}:{version}:{request_key}").random()
            if r < f.error_rate:
                raise SimulatedFault(f"simulated fault on {version} ({f.note or 'error injection'})")


# -- routing -----------------------------------------------------------------------------------------------
ROUTING_SALT = "scw-canary-v1"


def bucket(request_key: str, salt: str = ROUTING_SALT) -> int:
    """Stable bucket in [0, 10000) for a request key (same key -> same arm for a release)."""
    return int(hashlib.sha256(f"{salt}:{request_key}".encode()).hexdigest()[:8], 16) % 10_000


def routes_to_candidate(request_key: str, fraction: float, salt: str = ROUTING_SALT) -> bool:
    return bucket(request_key, salt) < round(fraction * 10_000)


@dataclass(frozen=True)
class Snapshot:
    deployment: Deployment
    champion: LoadedModel | None
    candidate: LoadedModel | None
    candidate_error: str | None = None
    champion_error: str | None = None


@dataclass
class PredictOutcome:
    http_status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


class TelemetryWriter:
    """Bounded, batched writer for request rows. When the queue is full rows are dropped and counted
    (backpressure never blocks or fails a prediction)."""

    def __init__(self, store: Store, maxsize: int = 20_000, batch: int = 256, interval_s: float = 0.2):
        self.store = store
        self.q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)
        self.batch = batch
        self.interval_s = interval_s
        self.dropped = 0
        self._stop = threading.Event()
        self._flush_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="scw-telemetry", daemon=True)
        self._thread.start()

    def put(self, row: dict[str, Any]) -> None:
        try:
            self.q.put_nowait(row)
        except queue.Full:
            self.dropped += 1
            tm.TELEMETRY_DROPPED.inc()

    def flush(self) -> None:
        with self._flush_lock:
            rows = []
            while True:
                try:
                    rows.append(self.q.get_nowait())
                except queue.Empty:
                    break
            if rows:
                self.store.insert_requests(rows)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.flush()
            except Exception:  # pragma: no cover - logged, retried next tick
                log.exception("telemetry flush failed")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.flush()


class InferenceService:
    def __init__(
        self,
        store: Store,
        paths: StatePaths,
        policy: ReleasePolicy,
        faults: FaultInjector | None = None,
        max_workers: int = 8,
    ):
        limit_native_threads()
        self.store = store
        self.paths = paths
        self.policy = policy
        self.pool = ModelPool(store, paths)
        self.faults = faults or FaultInjector()
        # Bulkhead: a hung candidate can exhaust only its own lane, never the champion's.
        self.executors = {
            "champion": ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scw-champion"),
            "candidate": ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scw-candidate"),
        }
        self.writer = TelemetryWriter(store)
        self._snap: Snapshot | None = None
        self._snap_lock = threading.Lock()
        self.on_request_recorded: Any = None  # controller hook (logical-clock guardrail windows)

    # -- snapshot management --------------------------------------------------------------------------------
    def snapshot(self) -> Snapshot:
        dep = self.store.deployment()
        snap = self._snap
        if snap is not None and snap.deployment.generation == dep.generation:
            return snap
        with self._snap_lock:
            snap = self._snap
            if snap is not None and snap.deployment.generation == dep.generation:
                return snap
            champion, champ_err = None, None
            if dep.champion_version:
                try:
                    champion = self.pool.get(dep.champion_version)
                except art.ArtifactError as exc:
                    # Fail closed but stay up: /readyz reports it and predictions return 503. Not cached, so
                    # a fixed artifact or a recovery swap is picked up on the next request.
                    champ_err = str(exc)
            candidate, cand_err = None, None
            if dep.candidate_version and dep.mode in ("shadow", "canary"):
                try:
                    candidate = self.pool.get(dep.candidate_version)
                except art.ArtifactError as exc:
                    cand_err = str(exc)  # candidate unhealthy: routing falls back to champion
            snap = Snapshot(dep, champion, candidate, cand_err, champ_err)
            if champ_err is None:
                self._snap = snap
            tm.DEPLOYMENT_GENERATION.set(dep.generation)
            tm.CANARY_FRACTION.set(dep.canary_fraction if dep.mode == "canary" else 0.0)
            return snap

    def _call(self, model: LoadedModel, X: np.ndarray, request_key: str, lane: str = "champion") -> tuple[float, float]:
        def run() -> float:
            self.faults.apply(model.version, request_key)
            return float(model.score(X)[0])

        t0 = time.perf_counter()
        fut = self.executors[lane].submit(run)
        try:
            score = fut.result(timeout=self.policy.model_timeout_ms / 1000.0)
        except FutureTimeout:
            raise TimeoutError(f"model {model.version} exceeded {self.policy.model_timeout_ms:.0f} ms") from None
        ms = (time.perf_counter() - t0) * 1000
        tm.MODEL_INFERENCE.labels(model_version=model.version).observe(ms / 1000)
        return score, ms

    # -- main entry -----------------------------------------------------------------------------------------
    def predict(self, payload: dict[str, Any], trace_id: str | None = None) -> PredictOutcome:
        t0 = time.perf_counter()
        trace_id = trace_id or uuid.uuid4().hex[:16]
        request_id = str(payload.get("request_id") or f"req-{uuid.uuid4().hex[:20]}")
        features = payload.get("features")
        ctx = payload.get("context") or {}
        simulated = bool(ctx.get("simulated", False))
        scenario = ctx.get("scenario")
        request_key = str(payload.get("request_key") or request_id)
        snap = self.snapshot()
        dep = snap.deployment
        base_row: dict[str, Any] = {
            "ts": self.store.clock(),
            "request_id": request_id,
            "request_key": request_key,
            "release_id": dep.release_id if dep.mode in ("shadow", "canary") else None,
            "generation": dep.generation,
            "simulated": int(simulated),
            "scenario": scenario,
        }

        problems = validate_record(features) if isinstance(features, dict) else ["features must be an object"]
        if problems:
            tm.SCHEMA_ERRORS.inc()
            ms = (time.perf_counter() - t0) * 1000
            self._record(
                {
                    **base_row,
                    "arm": "rejected",
                    "latency_ms": ms,
                    "status": "schema_error",
                    "error": "; ".join(problems)[:500],
                }
            )
            tm.REQUESTS.labels(arm="rejected", model_version="none", status="schema_error").inc()
            return PredictOutcome(
                422,
                {
                    "error": "schema_validation_failed",
                    "problems": problems,
                    "request_id": request_id,
                    "trace_id": trace_id,
                },
                {"X-Trace-Id": trace_id},
            )
        if snap.champion is None:
            return PredictOutcome(
                503,
                {
                    "error": "champion_unavailable" if snap.champion_error else "no_champion_deployed",
                    "detail": snap.champion_error,
                    "request_id": request_id,
                    "trace_id": trace_id,
                },
                {"X-Trace-Id": trace_id, "Retry-After": "5"},
            )

        assert isinstance(features, dict)
        X = encode_records([features])
        arm = "champion"
        if dep.mode == "canary" and dep.candidate_version and routes_to_candidate(request_key, dep.canary_fraction):
            arm = "candidate"

        row = dict(base_row)
        row["arm"] = arm
        fallback = None
        served: LoadedModel = snap.champion
        score: float | None = None
        cand_ms: float | None = None
        champ_ms: float | None = None
        cand_status, cand_error = None, None

        if arm == "candidate":
            if snap.candidate is None:
                fallback = "candidate_unavailable"
                cand_status, cand_error = "unavailable", snap.candidate_error
            else:
                try:
                    score, cand_ms = self._call(snap.candidate, X, request_key, "candidate")
                    served = snap.candidate
                    cand_status = "ok"
                except TimeoutError as exc:
                    fallback, cand_status, cand_error = "candidate_timeout", "timeout", str(exc)
                except Exception as exc:
                    fallback, cand_status, cand_error = (
                        "candidate_error",
                        "error",
                        f"{type(exc).__name__}: {exc}",
                    )
            if fallback:
                tm.FALLBACKS.labels(reason=fallback).inc()

        champion_score = None
        if score is not None:
            # Candidate served; also score the champion on the same request for a paired comparison.
            # A champion failure here must not fail a request the candidate already answered.
            try:
                champion_score, champ_ms = self._call(snap.champion, X, request_key)
            except Exception:
                champion_score = None
        else:
            try:
                score, champ_ms = self._call(snap.champion, X, request_key)
                champion_score = score
                served = snap.champion
            except Exception as exc:
                ms = (time.perf_counter() - t0) * 1000
                err = f"{type(exc).__name__}: {exc}"
                self._record({**row, "latency_ms": ms, "status": "error", "error": err[:500], "fallback": fallback})
                tm.REQUESTS.labels(arm=arm, model_version=snap.champion.version, status="error").inc()
                return PredictOutcome(
                    503,
                    {
                        "error": "model_unavailable",
                        "detail": "champion model failed or timed out; no prediction was made",
                        "request_id": request_id,
                        "trace_id": trace_id,
                    },
                    {"X-Trace-Id": trace_id, "Retry-After": "1"},
                )

        decision = int(score >= served.threshold)
        ms = (time.perf_counter() - t0) * 1000
        row.update(
            {
                "served_version": served.version,
                "score": score,
                "decision": decision,
                "latency_ms": ms,
                "status": "ok",
                "fallback": fallback,
                "features_json": json.dumps(features, sort_keys=True),
            }
        )
        row["champion_latency_ms"] = champ_ms
        if champion_score is not None:
            row["champion_score"] = champion_score
            row["champion_decision"] = int(champion_score >= snap.champion.threshold)
        if arm == "candidate":
            # The candidate's own outcome on this request (also when it failed and we fell back).
            row["shadow_version"] = dep.candidate_version
            row["shadow_status"] = cand_status
            row["error"] = cand_error
            if served is snap.candidate:
                row["shadow_score"] = score
                row["shadow_decision"] = decision
                row["shadow_latency_ms"] = cand_ms

        # Shadow mode: the response is already decided above; the candidate runs afterwards and its
        # result is only recorded. Exceptions and timeouts are swallowed into telemetry.
        if dep.mode == "shadow" and dep.candidate_version:
            row["shadow_version"] = dep.candidate_version
            if snap.candidate is None:
                row["shadow_status"], row["error"] = "unavailable", snap.candidate_error
            else:
                try:
                    ss, sms = self._call(snap.candidate, X, request_key, "candidate")
                    row.update(
                        {
                            "shadow_score": ss,
                            "shadow_decision": int(ss >= snap.candidate.threshold),
                            "shadow_latency_ms": sms,
                            "shadow_status": "ok",
                        }
                    )
                except TimeoutError as exc:
                    row["shadow_status"], row["error"] = "timeout", str(exc)
                except Exception as exc:
                    row["shadow_status"], row["error"] = "error", f"{type(exc).__name__}: {exc}"
            tm.SHADOW.labels(status=row["shadow_status"] or "none").inc()

        self._record(row)
        tm.REQUESTS.labels(arm=arm, model_version=served.version, status="ok").inc()
        tm.REQUEST_LATENCY.labels(arm=arm, model_version=served.version).observe(ms / 1000)
        body = {
            "request_id": request_id,
            "trace_id": trace_id,
            "model_version": served.version,
            "arm": arm,
            "score": round(score, 6),
            "decision": bool(decision),
            "threshold": served.threshold,
            "model_sha256": served.sha256,
            "schema_version": "txn-risk-v1",
            "latency_ms": round(ms, 3),
            "fallback": fallback,
            "simulated": simulated,
        }
        headers = {
            "X-Model-Version": served.version,
            "X-Release-Arm": arm,
            "X-Trace-Id": trace_id,
            "X-Deployment-Generation": str(dep.generation),
        }
        if fallback:
            headers["X-Fallback"] = fallback
        return PredictOutcome(200, body, headers)

    def _record(self, row: dict[str, Any]) -> None:
        self.writer.put(row)
        hook = self.on_request_recorded
        if hook is not None and row.get("release_id"):
            hook(row)

    def close(self) -> None:
        self.writer.close()
        for ex in self.executors.values():
            ex.shutdown(wait=False, cancel_futures=True)
