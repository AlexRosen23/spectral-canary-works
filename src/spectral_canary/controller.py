"""Release controller: registered → validated → staging → shadow → canary → promoted, with rollback.

Every transition is (1) checked against an explicit transition table, (2) applied in one SQLite
transaction together with its audit event, and (3) for anything that moves traffic, applied as a
compare-and-swap on the deployment generation. Decisions that move traffic automatically (guardrail
rollback/pause, stuck-release revert) are policy-driven and recorded with their evidence.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from . import telemetry as tm
from .artifacts import ArtifactError
from .data import load_split_csv
from .gates import run_offline_gate
from .monitor import drift_report, evaluate_canary_window, shadow_summary
from .paths import StatePaths
from .policy import ReleasePolicy
from .schema import decode_row
from .serving import InferenceService
from .store import ConflictError, Store

log = logging.getLogger("scw.controller")

TRANSITIONS: dict[str, set[str]] = {
    "registered": {"validated", "blocked"},
    "validated": {"staging", "cancelled"},
    "staging": {"shadow", "rolled_back", "reverted_stuck"},
    "shadow": {"canary", "rolled_back", "reverted_stuck"},
    "canary": {"promoted", "rolled_back", "paused", "reverted_stuck"},
    "paused": {"canary", "rolled_back", "reverted_stuck"},
    "promoted": {"rolled_back"},
}
ACTIVE_STATES = {"staging", "shadow", "canary", "paused"}
ROLE_RANK = {"viewer": 0, "operator": 1, "release_manager": 2}


class ReleaseError(RuntimeError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class Controller:
    """The release state machine. Lane-specific evidence (offline gate, staging smoke, shadow summary, canary
    window, verification requests, postmortem) sits behind small hook methods so the LLM lane
    (``llm.controller.LLMController``) reuses every transition, CAS swap, approval rule and the rollback path."""

    release_prefix = "rel"

    def __init__(
        self,
        store: Store,
        service: InferenceService | Any,
        paths: StatePaths,
        policy: ReleasePolicy,
        postmortem_dir: Path | None = None,
    ):
        self.store = store
        self.service = service
        self.paths = paths
        self.policy = policy
        self.postmortem_dir = postmortem_dir or paths.reports
        self._lock = threading.RLock()
        self._pending: dict[str, int] = {}
        self.simulated_context = False  # set by demo/chaos drivers so their events are labeled
        service.on_request_recorded = self._on_request

    # -- helpers --------------------------------------------------------------------------------------------
    def _release(self, release_id: str) -> dict[str, Any]:
        rel = self.store.get_release(release_id)
        if rel is None:
            raise ReleaseError(f"unknown release {release_id}", 404)
        return rel

    def _check(self, rel: dict[str, Any], to: str) -> None:
        if to not in TRANSITIONS.get(rel["state"], set()):
            raise ReleaseError(f"illegal transition {rel['state']} -> {to} for {rel['release_id']}")

    def active_release(self) -> dict[str, Any] | None:
        for r in self.store.list_releases():
            if r["state"] in ACTIVE_STATES:
                return r
        return None

    @staticmethod
    def require_role(role: str, needed: str) -> None:
        if ROLE_RANK.get(role, -1) < ROLE_RANK[needed]:
            raise ReleaseError(f"role {role!r} may not perform this action (needs {needed})", 403)

    # -- bootstrap ------------------------------------------------------------------------------------------
    def bootstrap_champion(self, version: str, actor: str) -> None:
        """First deployment of a champion (no prior champion). Gate still runs (non-inferiority skipped)."""
        dep = self.store.deployment()
        if dep.champion_version:
            raise ReleaseError(f"champion already deployed ({dep.champion_version})")
        report = self._offline_gate(version, None)
        if not report["passed"]:
            raise ReleaseError(f"bootstrap gate failed: {report['blocking_reasons']}")
        self.store.swap_deployment(
            dep.generation,
            event_kind="deployment.bootstrapped",
            event_payload={"champion": version, "gate_passed": True},
            actor=actor,
            champion_version=version,
            last_known_good=version,
            mode="none",
        )
        self.service.snapshot()

    # -- lane hooks (the ML lane's evidence; overridden by llm.controller.LLMController) ---------------------
    def _offline_gate(self, candidate: str, champion: str | None) -> dict[str, Any]:
        return run_offline_gate(candidate, champion, self.service.pool, self.paths, self.policy.offline_gate)

    def _staging_smoke(self, version: str) -> dict[str, Any]:
        """Load the candidate for real and score one golden case. Raises ArtifactError if it cannot load."""
        from .gates import GOLDEN_HIGH
        from .schema import encode_records

        m = self.service.pool.get(version)
        return {"smoke_score": round(float(m.score(encode_records([GOLDEN_HIGH]))[0]), 6)}

    def _shadow_summary(self, rel: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
        rows = [r for r in rows if r.get("shadow_version") == rel["candidate_version"] and r["arm"] == "champion"]
        return shadow_summary(rows, self.policy.shadow)

    def _evaluate_window(
        self, rel: dict[str, Any], window: list[dict[str, Any]], cumulative: list[dict[str, Any]], idx: int
    ) -> dict[str, Any]:
        ev = evaluate_canary_window(window, cumulative, self.policy.canary, idx)
        ev["drift"] = drift_report(window, self.paths.data / "train.csv")
        return ev

    @staticmethod
    def _window_event_payload(ev: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "window_index",
            "verdict",
            "breaches",
            "insufficient",
            "healthy_windows",
            "promotion_eligible",
            "window_requests",
        )
        return {k: ev[k] for k in keys} | {
            "saturation": ev.get("saturation"),
            "arms": ev["arms"],
            "labeled": ev["labeled_cumulative"],
            "drift_status": ev["drift"]["status"],
            "drift_max_psi": ev["drift"].get("max_psi"),
            "drift_features": {k: v["psi"] for k, v in ev["drift"].get("features", {}).items()},
        }

    def _verification_payloads(self, release_id: str, why: str, n: int) -> list[dict[str, Any]]:
        X, _, _ = load_split_csv(self.paths.data / "val.csv")
        return [
            {
                "request_id": f"verify-{release_id}-{why}-{i}",
                "request_key": f"verify-{i}",
                "features": decode_row(X[i]),
                "context": {"simulated": False, "scenario": f"{why}_verification"},
            }
            for i in range(n)
        ]

    def _write_postmortem(self, release_id: str) -> Path:
        from .postmortem import write_postmortem

        return write_postmortem(self.store, release_id, self.postmortem_dir)

    # -- release lifecycle ----------------------------------------------------------------------------------
    def propose(self, candidate: str, actor: str) -> dict[str, Any]:
        with self._lock:
            if self.store.get_model(candidate) is None:
                raise ReleaseError(f"candidate {candidate} is not registered", 404)
            active = self.active_release()
            if active:
                raise ReleaseError(f"release {active['release_id']} is already active ({active['state']})")
            dep = self.store.deployment()
            if dep.champion_version == candidate:
                raise ReleaseError(f"{candidate} is already the champion")
            release_id = f"{self.release_prefix}-{len(self.store.list_releases()) + 1:04d}"
            self.store.create_release(release_id, candidate, dep.champion_version, "registered", actor)
            report = self._offline_gate(candidate, dep.champion_version)
            state = "validated" if report["passed"] else "blocked"
            self.store.update_release(
                release_id,
                state=state,
                gate_report=report,
                reason=None if report["passed"] else "; ".join(report["blocking_reasons"]),
                event_kind="release.gate_evaluated",
                actor=actor,
                event_payload={
                    "passed": report["passed"],
                    "blocking_reasons": report["blocking_reasons"],
                    "checks": [{k: c[k] for k in ("name", "passed", "observed", "limit")} for c in report["checks"]],
                    "policy_sha256": self.policy.sha256,
                    "to_state": state,
                },
            )
            tm.RELEASE_DECISIONS.labels(decision=f"gate_{state}").inc()
            return self._release(release_id)

    def to_staging(self, release_id: str, actor: str) -> dict[str, Any]:
        with self._lock:
            rel = self._release(release_id)
            self._check(rel, "staging")
            try:
                smoke = self._staging_smoke(rel["candidate_version"])
            except ArtifactError as exc:
                self.store.update_release(
                    release_id,
                    state="rolled_back",
                    reason=f"candidate failed to load: {exc}",
                    event_kind="release.staging_failed",
                    actor=actor,
                    event_payload={"error": str(exc)},
                )
                tm.RELEASE_DECISIONS.labels(decision="staging_failed").inc()
                raise ReleaseError(f"candidate failed to load in staging: {exc}") from exc
            self.store.update_release(
                release_id,
                state="staging",
                event_kind="release.staged",
                actor=actor,
                event_payload={"loaded": rel["candidate_version"], **smoke},
            )
            return self._release(release_id)

    def to_shadow(self, release_id: str, actor: str) -> dict[str, Any]:
        with self._lock:
            rel = self._release(release_id)
            self._check(rel, "shadow")
            dep = self.store.deployment()
            self.service.writer.flush()
            start = self.store.max_request_seq()
            self.store.swap_deployment(
                dep.generation,
                event_kind="release.shadow_started",
                actor=actor,
                release_id=release_id,
                event_payload={"candidate": rel["candidate_version"], "champion": dep.champion_version},
                release_update={"state": "shadow", "window_start_seq": start},
                candidate_version=rel["candidate_version"],
                mode="shadow",
                canary_fraction=0.0,
                dep_release_id=release_id,
            )
            self.service.snapshot()
            return self._release(release_id)

    def shadow_report(self, release_id: str) -> dict[str, Any]:
        rel = self._release(release_id)
        self.service.writer.flush()
        return self._shadow_summary(rel, self.store.requests_since(rel["window_start_seq"], release_id))

    def to_canary(self, release_id: str, actor: str) -> dict[str, Any]:
        with self._lock:
            rel = self._release(release_id)
            self._check(rel, "canary")
            if rel["state"] == "shadow":
                summary = self.shadow_report(release_id)
                self.store.update_release(
                    release_id,
                    shadow_summary=summary,
                    event_kind="release.shadow_evaluated",
                    actor=actor,
                    event_payload=summary,
                )
                if not summary["passed"]:
                    tm.RELEASE_DECISIONS.labels(decision="canary_refused").inc()
                    raise ReleaseError(f"shadow evidence does not allow canary: {summary['reasons']}")
            dep = self.store.deployment()
            self.service.writer.flush()
            start = self.store.max_request_seq()
            frac = self.policy.canary.fraction
            self.store.swap_deployment(
                dep.generation,
                event_kind="release.canary_started",
                actor=actor,
                release_id=release_id,
                event_payload={
                    "candidate": rel["candidate_version"],
                    "fraction": frac,
                    "routing": "sha256(salt:request_key) bucket < fraction*10000",
                },
                release_update={
                    "state": "canary",
                    "canary_start_seq": start,
                    "last_eval_seq": start,
                    "healthy_windows": 0,
                },
                candidate_version=rel["candidate_version"],
                mode="canary",
                canary_fraction=frac,
                dep_release_id=release_id,
            )
            self._pending[release_id] = 0
            self.service.snapshot()
            return self._release(release_id)

    # -- guardrails -----------------------------------------------------------------------------------------
    def _on_request(self, row: dict[str, Any]) -> None:
        rid = row.get("release_id")
        dep = self.service.snapshot().deployment
        if not rid or dep.mode != "canary" or dep.release_id != rid:
            return
        with self._lock:
            self._pending[rid] = self._pending.get(rid, 0) + 1
            due = self._pending[rid] >= self.policy.canary.window_requests
        if due:
            self.evaluate(rid)

    def evaluate(self, release_id: str, actor: str = "controller") -> dict[str, Any]:
        with self._lock:
            rel = self._release(release_id)
            if rel["state"] != "canary":
                return rel.get("last_evaluation") or {}
            self.service.writer.flush()
            last = rel["last_eval_seq"] or rel["canary_start_seq"] or 0
            window = self.store.requests_since(last, release_id)
            cumulative = self.store.requests_since(rel["canary_start_seq"] or 0, release_id)
            idx = rel["windows_evaluated"] + 1
            ev = self._evaluate_window(rel, window, cumulative, idx)
            new_last = max([r["seq"] for r in window], default=last)
            self._pending[release_id] = 0
            healthy = (
                rel["healthy_windows"] + 1
                if ev["verdict"] == "healthy"
                else (rel["healthy_windows"] if ev["verdict"] == "insufficient_evidence" else 0)
            )
            ev["healthy_windows"] = healthy
            ev["promotion_eligible"] = (
                healthy >= self.policy.canary.healthy_windows_to_promote
                and ev["labels_sufficient"]
                and ev["verdict"] == "healthy"
            )
            self.store.update_release(
                release_id,
                last_eval_seq=new_last,
                windows_evaluated=idx,
                healthy_windows=healthy,
                last_evaluation=ev,
                heartbeat_at=self.store.clock(),
                event_kind="guardrail.evaluated",
                actor=actor,
                simulated=bool(ev.get("simulated_traffic_share")),
                event_payload=self._window_event_payload(ev),
            )
            tm.GUARDRAIL_EVALS.labels(verdict=ev["verdict"]).inc()
            if ev.get("drift", {}).get("status") == "major" and not ev.get("drift_is_breach"):
                self.store.append_event(
                    "drift.alert",
                    {
                        "window_index": idx,
                        "max_psi": ev["drift"]["max_psi"],
                        "note": "affects both arms; not a rollback trigger",
                    },
                    actor="controller",
                    simulated=bool(ev.get("simulated_traffic_share")),
                    release_id=release_id,
                )
            if ev["verdict"] == "breach":
                if self.policy.canary.on_breach == "rollback":
                    self.rollback(
                        release_id,
                        actor="controller",
                        trigger="guardrail_breach",
                        reason="; ".join(ev["breaches"]),
                        evidence=ev,
                    )
                else:
                    self.pause(release_id, actor="controller", reason="; ".join(ev["breaches"]))
            elif ev["verdict"] == "inconclusive_saturated":
                # The gateway, not the candidate, is the likely cause: take the candidate out of traffic (fail-safe,
                # and it sheds the paired-call load) without a rollback, a postmortem or a verdict on the model.
                # An operator resumes the canary once capacity is back, or rolls it back.
                self.pause(release_id, actor="controller", reason=ev["saturation"]["reason"])
            return ev

    # -- promotion ------------------------------------------------------------------------------------------
    def approve(self, release_id: str, actor: str, role: str) -> dict[str, Any]:
        self.require_role(role, "release_manager")
        with self._lock:
            rel = self._release(release_id)
            self._check(rel, "promoted")
            if actor == rel.get("created_by"):
                raise ReleaseError("separation of duties: the release creator cannot approve it", 403)
            ev = rel.get("last_evaluation") or {}
            if not ev.get("promotion_eligible"):
                raise ReleaseError(
                    f"not eligible for promotion: healthy_windows={rel['healthy_windows']}, "
                    f"labels_sufficient={ev.get('labels_sufficient')}, verdict={ev.get('verdict')}"
                )
            last_seen = max(
                [r["ts"] for r in self.store.requests_since(rel["last_eval_seq"] or 0, release_id)]
                or [rel["updated_at"]]
            )
            if self.store.clock() - max(last_seen, rel["updated_at"]) > self.policy.canary.stale_after_seconds:
                raise ReleaseError("canary evidence is stale; send fresh traffic before promoting")
            try:
                self.service.pool.reverify(rel["candidate_version"])
            except ArtifactError as exc:
                self.rollback(
                    release_id,
                    actor="controller",
                    trigger="artifact_changed_before_promotion",
                    reason=str(exc),
                )
                raise ReleaseError(f"candidate artifact changed since validation: {exc}") from exc
            dep = self.store.deployment()
            approvals = [*rel["approvals"], {"actor": actor, "role": role, "ts": self.store.clock()}]
            self.store.swap_deployment(
                dep.generation,
                event_kind="release.promoted",
                actor=actor,
                release_id=release_id,
                event_payload={
                    "new_champion": rel["candidate_version"],
                    "previous_champion": dep.champion_version,
                    "approved_by": actor,
                    "evidence_window": ev.get("window_index"),
                },
                release_update={"state": "promoted", "approvals": approvals},
                champion_version=rel["candidate_version"],
                last_known_good=dep.champion_version,
                candidate_version=None,
                mode="none",
                canary_fraction=0.0,
                dep_release_id=None,
            )
            tm.RELEASE_DECISIONS.labels(decision="promoted").inc()
            self.service.snapshot()
            self._verify_serving(release_id, rel["candidate_version"], "promotion")
            return self._release(release_id)

    def pause(self, release_id: str, actor: str, reason: str) -> dict[str, Any]:
        with self._lock:
            rel = self._release(release_id)
            self._check(rel, "paused")
            dep = self.store.deployment()
            self.store.swap_deployment(
                dep.generation,
                event_kind="release.paused",
                actor=actor,
                release_id=release_id,
                event_payload={"reason": reason},
                release_update={"state": "paused", "reason": reason},
                mode="none",
                candidate_version=None,
                canary_fraction=0.0,
                dep_release_id=None,
            )
            tm.RELEASE_DECISIONS.labels(decision="paused").inc()
            self.service.snapshot()
            return self._release(release_id)

    # -- rollback -------------------------------------------------------------------------------------------
    def rollback(
        self, release_id: str, actor: str, trigger: str, reason: str, evidence: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._lock:
            rel = self._release(release_id)
            if rel["state"] in ("rolled_back", "reverted_stuck"):
                self.store.append_event(
                    "rollback.duplicate_ignored",
                    {"trigger": trigger, "state": rel["state"]},
                    actor=actor,
                    release_id=release_id,
                )
                return {**rel, "rollback_status": "already_rolled_back"}
            self._check(rel, "rolled_back")
            for _attempt in range(3):
                dep = self.store.deployment()
                if rel["state"] == "promoted":
                    target = rel["champion_version"]
                    if not target:
                        raise ReleaseError("no previous champion to roll back to")
                    self.service.pool.reverify(target)  # never roll back onto a tampered artifact
                    fields: dict[str, Any] = {
                        "champion_version": target,
                        "last_known_good": target,
                        "candidate_version": None,
                        "mode": "none",
                        "canary_fraction": 0.0,
                        "dep_release_id": None,
                    }
                else:
                    target = dep.champion_version
                    fields = {
                        "candidate_version": None,
                        "mode": "none",
                        "canary_fraction": 0.0,
                        "dep_release_id": None,
                    }
                try:
                    self.store.swap_deployment(
                        dep.generation,
                        event_kind="rollback.executed",
                        actor=actor,
                        release_id=release_id,
                        simulated=self.simulated_context,
                        event_payload={
                            "trigger": trigger,
                            "reason": reason,
                            "from_state": rel["state"],
                            "candidate": rel["candidate_version"],
                            "serving_after": target,
                            "evidence_window": (evidence or {}).get("window_index"),
                        },
                        release_update={"state": "rolled_back", "reason": reason},
                        **fields,
                    )
                    break
                except ConflictError:
                    rel = self._release(release_id)
                    if rel["state"] == "rolled_back":
                        return {**rel, "rollback_status": "already_rolled_back"}
            else:  # pragma: no cover
                raise ReleaseError("could not acquire deployment pointer after 3 attempts")
            tm.ROLLBACKS.labels(trigger=trigger).inc()
            self.service.snapshot()
            verification = self._verify_serving(release_id, target, "rollback")
            path = self._write_postmortem(release_id)
            self.store.append_event(
                # relative to the run directory: event logs get copied into public evidence, and an absolute
                # path would carry the local username and folder layout with it
                "postmortem.written",
                {"path": f"{path.parent.name}/{path.name}"},
                actor="controller",
                release_id=release_id,
            )
            return {
                **self._release(release_id),
                "rollback_status": "rolled_back",
                "verification": verification,
            }

    def _verify_serving(self, release_id: str, expected_version: str | None, why: str) -> dict[str, Any]:
        """Send real predictions through the gateway and confirm the expected version answers them."""
        n = self.policy.rollback.verification_requests
        ok = 0
        versions: dict[str, int] = {}
        lat = []
        for payload in self._verification_payloads(release_id, why, n):
            out = self.service.predict(payload)
            v = out.body.get("model_version", "none")
            versions[v] = versions.get(v, 0) + 1
            if out.http_status == 200 and v == expected_version:
                ok += 1
                lat.append(out.body["latency_ms"])
        self.service.writer.flush()
        health = self.health()
        result = {
            "requests": n,
            "served_by_expected": ok,
            "versions": versions,
            "expected": expected_version,
            "passed": ok == n and health["ready"],
            "health": health,
            "latency_ms_p95": sorted(lat)[int(0.95 * (len(lat) - 1))] if lat else None,
        }
        self.store.append_event(
            f"{why}.verified" if result["passed"] else f"{why}.verification_failed",
            result,
            actor="controller",
            release_id=release_id,
        )
        return result

    def health(self) -> dict[str, Any]:
        dep = self.store.deployment()
        problems = []
        if not dep.champion_version:
            problems.append("no champion")
        else:
            try:
                self.service.pool.get(dep.champion_version)
                self.service.pool.reverify(dep.champion_version)
            except ArtifactError as exc:
                problems.append(f"champion artifact: {exc}")
        out = {
            "ready": not problems,
            "problems": problems,
            "champion": dep.champion_version,
            "generation": dep.generation,
            "mode": dep.mode,
        }
        scoring_paths = getattr(self.service.pool, "scoring_paths", None)  # ML model pool only, not LLM bundles
        if scoring_paths is not None:
            out["scoring"] = scoring_paths()
        return out

    # -- recovery / housekeeping ----------------------------------------------------------------------------
    def recover(self, actor: str = "controller") -> list[dict[str, Any]]:
        """Run at startup and periodically: make the pointer safe, revert stuck releases."""
        actions = []
        with self._lock:
            dep = self.store.deployment()
            # 1. Champion artifact tampered or missing -> revert to last known good if that one verifies.
            if dep.champion_version:
                try:
                    self.service.pool.reverify(dep.champion_version)
                except ArtifactError as exc:
                    lkg = dep.last_known_good
                    if lkg and lkg != dep.champion_version:
                        try:
                            self.service.pool.reverify(lkg)
                            self.service.pool.evict(dep.champion_version)
                            self.store.swap_deployment(
                                dep.generation,
                                event_kind="recovery.champion_reverted",
                                actor=actor,
                                event_payload={"bad": dep.champion_version, "error": str(exc), "to": lkg},
                                champion_version=lkg,
                                candidate_version=None,
                                mode="none",
                                canary_fraction=0.0,
                                dep_release_id=None,
                            )
                            actions.append({"action": "champion_reverted", "to": lkg})
                        except ArtifactError:
                            self.store.append_event("recovery.no_safe_champion", {"error": str(exc)}, actor=actor)
                    else:
                        self.store.append_event("recovery.no_safe_champion", {"error": str(exc)}, actor=actor)
            # 2. Pointer says shadow/canary but release is not active -> inconsistent, revert traffic.
            dep = self.store.deployment()
            if dep.mode in ("shadow", "canary"):
                rel = self.store.get_release(dep.release_id) if dep.release_id else None
                if rel is None or rel["state"] not in ACTIVE_STATES:
                    self.store.swap_deployment(
                        dep.generation,
                        event_kind="recovery.pointer_reconciled",
                        actor=actor,
                        event_payload={"mode": dep.mode, "release_id": dep.release_id},
                        mode="none",
                        candidate_version=None,
                        canary_fraction=0.0,
                        dep_release_id=None,
                    )
                    actions.append({"action": "pointer_reconciled"})
            # 3. Stuck releases: no controller heartbeat for too long -> send all traffic to the champion.
            now = self.store.clock()
            for rel in self.store.list_releases():
                if rel["state"] in ACTIVE_STATES and now - rel["heartbeat_at"] > self.policy.canary.stuck_after_seconds:
                    dep = self.store.deployment()
                    self.store.swap_deployment(
                        dep.generation,
                        event_kind="release.stuck_reverted",
                        actor=actor,
                        release_id=rel["release_id"],
                        event_payload={
                            "state": rel["state"],
                            "seconds_since_heartbeat": round(now - rel["heartbeat_at"], 1),
                            "serving_after": dep.champion_version,
                        },
                        release_update={"state": "reverted_stuck", "reason": "no controller heartbeat"},
                        mode="none",
                        candidate_version=None,
                        canary_fraction=0.0,
                        dep_release_id=None,
                    )
                    actions.append({"action": "stuck_reverted", "release_id": rel["release_id"]})
                elif rel["state"] == "canary":
                    # resume counting where the persisted window left off
                    self.service.writer.flush()
                    n = len(self.store.requests_since(rel["last_eval_seq"] or 0, rel["release_id"]))
                    self._pending[rel["release_id"]] = n
            self.service.snapshot()
        return actions

    def tick(self) -> None:
        """Periodic heartbeat (keeps live releases from being judged stuck while this process is alive)."""
        rel = self.active_release()
        if rel:
            self.store.heartbeat(rel["release_id"])
