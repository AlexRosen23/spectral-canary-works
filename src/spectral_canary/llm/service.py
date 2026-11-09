"""Triage gateway core for the LLM lane. Same contract the controller expects from ``serving.InferenceService``:
``pool`` (verified bundles), ``writer`` (bounded telemetry), ``snapshot()``, ``predict()``, ``on_request_recorded``.

Per request (one immutable snapshot, exactly as in the ML lane):

- route with the same sticky hash (``serving.routes_to_candidate``) on ``request_key`` (a customer id);
- canary arm: the candidate answers if its call succeeds **and** its reply passes the output contract; otherwise
  the champion answers (``fallback``) and the candidate failure is recorded. The champion is then also run on the
  same alert (paired evidence for online grading);
- shadow mode: the champion's answer is final before the candidate runs; the candidate's result is only recorded;
- champion failure or an invalid champion reply fails closed: HTTP 503, ``routed_to_human: true``. A triage
  decision is never invented.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .. import artifacts as art
from ..paths import StatePaths
from ..serving import PredictOutcome, TelemetryWriter, routes_to_candidate
from ..store import Deployment
from . import otel
from .bundles import BundlePool, LoadedBundle
from .client import Backend, call_seed
from .graders import grade
from .policy import LLMReleasePolicy
from .store import LLMStore

MAX_SUMMARY_CHARS = 2000


@dataclass(frozen=True)
class LLMSnapshot:
    deployment: Deployment
    champion: LoadedBundle | None
    candidate: LoadedBundle | None
    candidate_error: str | None = None
    champion_error: str | None = None


def validate_alert(alert: Any) -> list[str]:
    if not isinstance(alert, dict):
        return ["alert must be an object"]
    p = []
    s = alert.get("summary")
    if not isinstance(s, str) or len(s.strip()) < 20:
        p.append("alert.summary must be a string of at least 20 characters")
    elif len(s) > MAX_SUMMARY_CHARS:
        p.append(f"alert.summary longer than {MAX_SUMMARY_CHARS} characters")
    if "fields" in alert and not isinstance(alert["fields"], dict):
        p.append("alert.fields must be an object when present")
    return p


class LLMService:
    def __init__(
        self, store: LLMStore, paths: StatePaths, policy: LLMReleasePolicy, backend: Backend, max_workers: int = 4
    ):
        self.store = store
        self.paths = paths
        self.policy = policy
        self.backend = backend
        self.pool = BundlePool(store, paths)
        self.writer = TelemetryWriter(store)
        self.executors = {
            "champion": ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scw-llm-champion"),
            "candidate": ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scw-llm-candidate"),
        }
        self._snap: LLMSnapshot | None = None
        self._snap_lock = threading.Lock()
        self.on_request_recorded: Any = None

    def snapshot(self) -> LLMSnapshot:
        dep = self.store.deployment()
        snap = self._snap
        if snap is not None and snap.deployment.generation == dep.generation:
            return snap
        with self._snap_lock:
            champion, champ_err = None, None
            if dep.champion_version:
                try:
                    champion = self.pool.get(dep.champion_version)
                except art.ArtifactError as exc:
                    champ_err = str(exc)
            candidate, cand_err = None, None
            if dep.candidate_version and dep.mode in ("shadow", "canary"):
                try:
                    candidate = self.pool.get(dep.candidate_version)
                except art.ArtifactError as exc:
                    cand_err = str(exc)
            snap = LLMSnapshot(dep, champion, candidate, cand_err, champ_err)
            if champ_err is None:
                self._snap = snap
            return snap

    def _call(
        self,
        lb: LoadedBundle,
        alert: dict[str, Any],
        request_id: str,
        lane: str,
        arm: str,
        expected: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One model call, graded by code. Never raises: failures come back as ``status``."""
        seed = call_seed(request_id, lb.version)
        timeout = self.policy.model_timeout_ms / 1000
        fut = self.executors[lane].submit(self.backend.chat, lb.bundle, alert, seed, timeout)
        r = fut.result()
        g = grade(r.text, expected)
        status = "ok" if r.ok and g["valid"] else ("invalid_output" if r.ok else (r.error_type or "error"))
        otel.record_call(self.backend, lb, r, arm)
        return {
            "version": lb.version,
            "status": status,
            "latency_ms": round(r.latency_ms, 2),
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "valid": g["valid"],
            "problems": g["problems"],
            "reason_code": g["reason_code"],
            "escalate": g["escalate"],
            "rationale": _rationale(r.text) if g["valid"] else None,
            "one_sentence": g["one_sentence"],
            "error": r.error,
        }

    def predict(self, payload: dict[str, Any], trace_id: str | None = None) -> PredictOutcome:
        t0 = time.perf_counter()
        trace_id = trace_id or uuid.uuid4().hex[:16]
        request_id = str(payload.get("request_id") or f"tri-{uuid.uuid4().hex[:20]}")
        request_key = str(payload.get("request_key") or request_id)
        ctx = payload.get("context") or {}
        alert = payload.get("alert")
        simulated = bool(ctx.get("simulated", False))
        snap = self.snapshot()
        dep = snap.deployment
        row: dict[str, Any] = {
            "ts": self.store.clock(),
            "request_id": request_id,
            "request_key": request_key,
            "release_id": dep.release_id if dep.mode in ("shadow", "canary") else None,
            "generation": dep.generation,
            "simulated": int(simulated),
            "scenario": ctx.get("scenario"),
        }
        problems = validate_alert(alert)
        if problems:
            self._record(
                {
                    **row,
                    "arm": "rejected",
                    "status": "schema_error",
                    "error": "; ".join(problems),
                    "latency_ms": (time.perf_counter() - t0) * 1000,
                }
            )
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
        assert isinstance(alert, dict)
        row["alert"] = {"fields": alert.get("fields"), "summary_chars": len(alert["summary"])}
        if snap.champion is None:
            return PredictOutcome(
                503,
                {
                    "error": "no_champion_deployed" if not snap.champion_error else "champion_unavailable",
                    "detail": snap.champion_error,
                    "request_id": request_id,
                    "trace_id": trace_id,
                    "routed_to_human": True,
                },
                {"X-Trace-Id": trace_id, "Retry-After": "5"},
            )
        arm = "champion"
        if dep.mode == "canary" and dep.candidate_version and routes_to_candidate(request_key, dep.canary_fraction):
            arm = "candidate"
        row["arm"] = arm
        served: dict[str, Any] | None = None
        served_bundle: LoadedBundle = snap.champion
        fallback = None
        cand_call = None
        champ_call = None
        if arm == "candidate":
            if snap.candidate is None:
                fallback = "candidate_unavailable"
                cand_call = {"version": dep.candidate_version, "status": "unavailable", "error": snap.candidate_error}
            else:
                cand_call = self._call(snap.candidate, alert, request_id, "candidate", "candidate")
                if cand_call["status"] == "ok":
                    served, served_bundle = cand_call, snap.candidate
                else:
                    fallback = f"candidate_{cand_call['status']}"
        if served is None:
            champ_call = self._call(snap.champion, alert, request_id, "champion", "champion")
            if champ_call["status"] == "ok":
                served, served_bundle = champ_call, snap.champion
        handler_ms = (time.perf_counter() - t0) * 1000
        if served is None:
            assert champ_call is not None
            self._record(
                {
                    **row,
                    "status": "model_error",
                    "error": champ_call.get("error") or champ_call["status"],
                    "fallback": fallback,
                    "latency_ms": handler_ms,
                    "champion": champ_call,
                    "candidate": cand_call,
                }
            )
            return PredictOutcome(
                503,
                {
                    "error": "triage_unavailable",
                    "detail": champ_call["status"],
                    "request_id": request_id,
                    "trace_id": trace_id,
                    "routed_to_human": True,
                },
                {"X-Trace-Id": trace_id, "Retry-After": "1"},
            )
        if arm == "candidate" and served is cand_call:
            # paired evidence for online grading: the champion on the same alert (not returned to the caller)
            champ_call = self._call(snap.champion, alert, request_id, "champion", "champion_paired")
        if dep.mode == "shadow" and dep.candidate_version:
            if snap.candidate is None:
                cand_call = {"version": dep.candidate_version, "status": "unavailable", "error": snap.candidate_error}
            else:
                cand_call = self._call(snap.candidate, alert, request_id, "candidate", "shadow")
        assert served is not None
        body = {
            "request_id": request_id,
            "trace_id": trace_id,
            "bundle_version": served_bundle.version,
            "model_version": served_bundle.version,
            "model": served_bundle.model,
            "bundle_sha256": served_bundle.sha256,
            "arm": arm,
            "triage": {k: served[k] for k in ("reason_code", "escalate", "rationale")},
            "latency_ms": round(handler_ms, 2),
            "model_latency_ms": served["latency_ms"],
            "tokens": {"input": served["input_tokens"], "output": served["output_tokens"]},
            "fallback": fallback,
            "simulated": simulated,
            "routed_to_human": False,
        }
        self._record(
            {
                **row,
                "served_version": served_bundle.version,
                "status": "ok",
                "fallback": fallback,
                "latency_ms": handler_ms,
                "served": served,
                "champion": champ_call,
                "candidate": cand_call,
            }
        )
        headers = {
            "X-Model-Version": served_bundle.version,
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


def _rationale(text: str | None) -> str | None:
    try:
        return str(json.loads(text or "")["rationale"])[:300]
    except (ValueError, KeyError, TypeError):
        return None
