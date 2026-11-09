"""Typed release policy loaded from configs/release_policy.toml."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .paths import CONFIG_DIR


class OfflineGatePolicy(BaseModel):
    max_pr_auc_drop: float = 0.03
    max_recall_drop: float = 0.05
    max_f1_drop: float = 0.04
    max_ece: float = 0.05
    max_slice_recall_drop: float = 0.12
    min_slice_positives: int = 25
    latency_p95_budget_ms: float = 50.0
    latency_p95_ratio_vs_champion: float = 6.0
    reproducibility_rows: int = 200
    min_test_positives: int = 100


class ShadowPolicy(BaseModel):
    min_requests: int = 400
    max_error_rate: float = 0.01
    max_score_js_divergence: float = 0.20
    max_decision_disagreement: float = 0.15


class CanaryPolicy(BaseModel):
    fraction: float = Field(default=0.10, ge=0.0, le=1.0)
    window_requests: int = 1500
    min_candidate_requests: int = 120
    min_labeled_positives_per_arm: int = 20
    max_error_rate: float = 0.02
    latency_p95_budget_ms: float = 50.0
    latency_p95_ratio_vs_champion: float = 6.0
    max_flag_rate_ratio_drop: float = 0.35
    recall_noninferiority_margin: float = 0.0
    healthy_windows_to_promote: int = 2
    on_breach: Literal["rollback", "pause"] = "rollback"
    require_human_approval: bool = True
    stale_after_seconds: float = 900
    stuck_after_seconds: float = 1800


class ServingPolicy(BaseModel):
    model_timeout_ms: float = 500.0


class RollbackPolicy(BaseModel):
    verification_requests: int = 50


class ReleasePolicy(BaseModel):
    offline_gate: OfflineGatePolicy = OfflineGatePolicy()
    shadow: ShadowPolicy = ShadowPolicy()
    canary: CanaryPolicy = CanaryPolicy()
    rollback: RollbackPolicy = RollbackPolicy()
    serving: ServingPolicy = ServingPolicy()
    sha256: str = ""

    @property
    def model_timeout_ms(self) -> float:
        return self.serving.model_timeout_ms

    @classmethod
    def load(cls, path: Path | None = None) -> ReleasePolicy:
        path = path or CONFIG_DIR / "release_policy.toml"
        raw = Path(path).read_bytes()
        p = cls.model_validate(tomllib.loads(raw.decode()))
        p.sha256 = hashlib.sha256(raw).hexdigest()
        return p
