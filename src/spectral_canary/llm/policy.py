"""Typed release policy for the LLM lane, loaded from ``configs/llm/release_policy.toml``.

It mirrors ``ReleasePolicy`` (the controller reads ``canary.window_requests``, ``canary.on_breach``,
``rollback.verification_requests``, ``sha256`` ... from either) and adds the LLM-specific gates.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..paths import CONFIG_DIR
from ..policy import CanaryPolicy, RollbackPolicy


class LLMOfflineGatePolicy(BaseModel):
    k: int = 3
    concurrency: int = 1
    min_schema_valid: float = 0.98
    # Sanity floors used when there is no champion (bootstrap). Not a quality bar.
    min_escalation_recall: float = 0.70
    min_reason_accuracy: float = 0.40
    # Non-inferiority vs the champion (same gold test split, same k, same seeds).
    max_escalation_recall_drop: float = 0.05
    max_segment_recall_drop: float = 0.10
    min_segment_cases: int = 15
    max_false_escalation_increase: float = 0.10
    max_reason_accuracy_drop: float = 0.05
    max_pass_k_drop: float = 0.05
    # Cost/latency offline: measured on the gate run, compared with the champion's gate run.
    max_cost_ratio_vs_champion: float = 1.25
    max_latency_p95_ratio_vs_champion: float = 1.5
    cost_latency_blocking: bool = False
    smoke_min_passed: int = 10  # informational smoke check: >= 10 of the 12 textbook cases


class LLMShadowPolicy(BaseModel):
    min_requests: int = 60
    max_error_rate: float = 0.02  # candidate call errors + timeouts + invalid outputs
    min_labeled_escalations: int = 10
    max_escalation_recall_drop: float = 0.10  # on labeled shadow requests, vs the champion on the same requests


class LLMCanaryPolicy(CanaryPolicy):
    max_invalid_output_rate: float = 0.02
    cost_ratio_vs_champion: float = 1.25  # candidate mean cost per call vs the champion arm in the same window
    min_paired_escalations: int = 10
    mcnemar_alpha: float = 0.05
    drift_action: Literal["rollback", "alert"] = "rollback"
    drift_min_rows: int = 150
    drift_psi_major: float = 0.25


class PricePolicy(BaseModel):
    input_usd_per_mtok: float = 0.10
    output_usd_per_mtok: float = 0.40


class LLMServingPolicy(BaseModel):
    model_timeout_ms: float = 30000.0


class LLMReleasePolicy(BaseModel):
    offline_gate: LLMOfflineGatePolicy = LLMOfflineGatePolicy()
    shadow: LLMShadowPolicy = LLMShadowPolicy()
    canary: LLMCanaryPolicy = LLMCanaryPolicy()
    rollback: RollbackPolicy = RollbackPolicy()
    serving: LLMServingPolicy = LLMServingPolicy()
    price: PricePolicy = PricePolicy()
    sha256: str = ""

    @property
    def model_timeout_ms(self) -> float:
        return self.serving.model_timeout_ms

    @property
    def price_dict(self) -> dict[str, float]:
        return self.price.model_dump()

    @classmethod
    def load(cls, path: Path | None = None) -> LLMReleasePolicy:
        path = path or CONFIG_DIR / "llm" / "release_policy.toml"
        raw = Path(path).read_bytes()
        p = cls.model_validate(tomllib.loads(raw.decode()))
        p.sha256 = hashlib.sha256(raw).hexdigest()
        return p
