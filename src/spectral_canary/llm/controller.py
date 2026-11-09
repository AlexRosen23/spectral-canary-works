"""The LLM lane's controller: the ML ``Controller`` (transition table, CAS swaps, separation of duties, stale
evidence, artifact re-verification, idempotent rollback, recovery, heartbeat) with LLM evidence plugged into
its hooks. Nothing about *how* a release moves or rolls back is reimplemented here."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..artifacts import ArtifactError
from ..controller import Controller
from ..paths import StatePaths
from .client import call_seed
from .gates import run_llm_offline_gate
from .graders import grade
from .monitor import evaluate_llm_window, llm_shadow_summary
from .policy import LLMReleasePolicy
from .postmortem import write_llm_postmortem
from .service import LLMService
from .store import LLMStore


class LLMController(Controller):
    release_prefix = "llm-rel"
    service: LLMService

    def __init__(
        self,
        store: LLMStore,
        service: LLMService,
        paths: StatePaths,
        policy: LLMReleasePolicy,
        gold_test: list[dict[str, Any]],
        gold_dev: list[dict[str, Any]],
        gold_sha: str,
        eval_dir: Path,
        postmortem_dir: Path | None = None,
        log: Any = None,
    ):
        super().__init__(store, service, paths, policy, postmortem_dir=postmortem_dir)  # type: ignore[arg-type]
        self.llm_policy = policy
        self.gold_test = gold_test
        self.gold_dev = gold_dev
        self.gold_sha = gold_sha
        self.eval_dir = eval_dir
        self.log = log

    @property
    def backend(self) -> Any:
        return self.service.backend

    def _offline_gate(self, candidate: str, champion: str | None) -> dict[str, Any]:
        return run_llm_offline_gate(
            candidate,
            champion,
            self.service.pool,
            self.backend,
            self.gold_test,
            self.gold_sha,
            self.llm_policy,
            self.eval_dir,
            self.log,
        )

    def _staging_smoke(self, version: str) -> dict[str, Any]:
        lb = self.service.pool.get(version)
        digest = self.backend.model_digest(lb.model)
        if digest != lb.bundle.get("model_digest"):
            raise ArtifactError(f"model weights behind {lb.model!r} changed since registration")
        case = self.gold_dev[0]
        r = self.backend.chat(
            lb.bundle,
            {"summary": case["summary"], "fields": case["fields"]},
            call_seed("staging", version),
            self.llm_policy.model_timeout_ms / 1000,
        )
        g = grade(r.text, case["expected"])
        if not r.ok or not g["valid"]:
            raise ArtifactError(f"staging call failed: {r.error or g['problems']}")
        return {
            "smoke_reply_valid": True,
            "smoke_latency_ms": round(r.latency_ms, 1),
            "model_digest": (digest or "")[:16],
        }

    def _shadow_summary(self, rel: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
        rows = [r for r in rows if (r.get("candidate") or {}).get("version") == rel["candidate_version"]]
        return llm_shadow_summary(rows, self.llm_policy.shadow, self.llm_policy.price_dict)

    def _evaluate_window(
        self, rel: dict[str, Any], window: list[dict[str, Any]], cumulative: list[dict[str, Any]], idx: int
    ) -> dict[str, Any]:
        return evaluate_llm_window(
            window, cumulative, self.llm_policy.canary, idx, rel["candidate_version"], self.llm_policy.price_dict
        )

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
        d = ev["drift"]
        return {k: ev[k] for k in keys} | {
            "arms": ev["arms"],
            "labeled": ev["labeled_cumulative"],
            "drift_status": d["status"],
            "drift_max_psi": d.get("max_psi"),
            "drift_features": {k: v["psi"] for k, v in d.get("features", {}).items()},
            "drift_is_breach": ev["drift_is_breach"],
        }

    def _verification_payloads(self, release_id: str, why: str, n: int) -> list[dict[str, Any]]:
        cases = (self.gold_dev * (n // max(1, len(self.gold_dev)) + 1))[:n]
        return [
            {
                "request_id": f"verify-{release_id}-{why}-{i}",
                "request_key": f"verify-{i}",
                "alert": {"summary": c["summary"], "fields": c["fields"]},
                "context": {"simulated": False, "scenario": f"{why}_verification"},
            }
            for i, c in enumerate(cases)
        ]

    def _write_postmortem(self, release_id: str) -> Path:
        return write_llm_postmortem(self.store, release_id, self.postmortem_dir)

    def health(self) -> dict[str, Any]:
        h = super().health()
        avail = getattr(self.backend, "available", None)
        if avail is not None and not avail():
            h["problems"].append("llm backend unreachable")
            h["ready"] = False
        return h
