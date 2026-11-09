"""Offline release gate: is this candidate safe to expose to any traffic at all?

Every check returns observed value, limit and a human-actionable reason. Metric checks compare the
candidate with the current champion on the same frozen test split (non-inferiority with explicit
margins from the release policy); integrity checks run real code against the real artifact.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from .data import load_split_csv
from .paths import StatePaths
from .policy import OfflineGatePolicy
from .schema import FEATURE_NAMES, schema_hash
from .serving import ModelPool
from .train import prediction_hash


@dataclass
class Check:
    name: str
    passed: bool
    observed: Any
    limit: Any
    reason: str
    skipped: bool = False


# Hand-written golden cases: an obviously risky and an obviously routine transaction. A sane model must
# rank the first above the second. (Correctness smoke test, not a quality measure.)
GOLDEN_HIGH = {
    "amount": 2400.0,
    "merchant_category": "gift_cards",
    "hour_of_day": 3,
    "account_age_days": 12,
    "txn_count_24h": 14,
    "distance_from_home_km": 3200.0,
    "device_trust_score": 0.08,
    "card_present": False,
    "country_mismatch": True,
}
GOLDEN_LOW = {
    "amount": 23.5,
    "merchant_category": "grocery",
    "hour_of_day": 11,
    "account_age_days": 3400,
    "txn_count_24h": 1,
    "distance_from_home_km": 2.1,
    "device_trust_score": 0.93,
    "card_present": True,
    "country_mismatch": False,
}


def run_offline_gate(
    candidate_version: str,
    champion_version: str | None,
    pool: ModelPool,
    paths: StatePaths,
    policy: OfflineGatePolicy,
) -> dict[str, Any]:
    from .schema import encode_records

    checks: list[Check] = []
    store = pool.store
    cand_rec = store.get_model(candidate_version)
    if cand_rec is None:
        return _report(
            candidate_version,
            champion_version,
            [Check("registered", False, None, "registered", "candidate version is not registered")],
        )
    cm = cand_rec["manifest"]

    # 1. Artifact integrity + safe deserialization (real load through the verified pool).
    try:
        model = pool.get(candidate_version)
        checks.append(
            Check(
                "artifact_integrity",
                True,
                cand_rec["artifact_sha256"][:16],
                "sha256 match + skops type allowlist",
                "artifact bytes match the registered hash",
            )
        )
    except Exception as exc:
        checks.append(
            Check(
                "artifact_integrity",
                False,
                str(exc),
                "sha256 match + skops type allowlist",
                f"artifact cannot be loaded safely: {exc}",
            )
        )
        return _report(candidate_version, champion_version, checks)

    # 2. Schema compatibility with the gateway contract.
    ok = cm["input_schema"].get("schema_sha256") == schema_hash()
    checks.append(
        Check(
            "schema_compatible",
            ok,
            cm["input_schema"].get("schema_sha256", "")[:16],
            schema_hash()[:16],
            "input schema matches the gateway"
            if ok
            else "candidate expects a different input schema; ship a schema migration first",
        )
    )

    # 3. Predict shape/range + golden ordering.
    X_te, y_te, _ = load_split_csv(paths.data / "test.csv")
    p = model.score(X_te)
    ok = p.shape == (len(y_te),) and bool(np.all((p >= 0) & (p <= 1))) and bool(np.all(np.isfinite(p)))
    checks.append(
        Check(
            "predict_shape_and_range",
            ok,
            f"shape={p.shape}, min={p.min():.4f}, max={p.max():.4f}",
            "(n,) in [0,1]",
            "scores are finite probabilities" if ok else "invalid scores",
        )
    )
    g = model.score(encode_records([GOLDEN_HIGH, GOLDEN_LOW]))
    ok = bool(g[0] > g[1])
    checks.append(
        Check(
            "golden_ordering",
            ok,
            [round(float(g[0]), 4), round(float(g[1]), 4)],
            "high-risk golden > low-risk golden",
            "ranks obvious cases correctly"
            if ok
            else "model ranks an obviously routine transaction above an obviously risky one",
        )
    )

    # 4. Reproducibility: re-scoring the frozen test split must reproduce the recorded predictions.
    ok = prediction_hash(p) == cm.get("test_prediction_sha256")
    checks.append(
        Check(
            "reproducible_predictions",
            ok,
            prediction_hash(p)[:16],
            str(cm.get("test_prediction_sha256", ""))[:16],
            "loaded artifact reproduces training-time test predictions"
            if ok
            else "loaded artifact does not reproduce its recorded predictions (environment drift?)",
        )
    )

    # 5. Latency: measured now, single-row, interleaved with the champion so machine load hits both equally.
    #    Fails only when over the absolute budget AND (no champion, or > N x the champion's p95).
    champ_model = None
    if champion_version:
        try:
            champ_model = pool.get(champion_version)
        except Exception:
            champ_model = None
    # Best of 3 rounds of 100 interleaved calls: transient external load inflates single rounds.
    rounds: list[tuple[float, float | None]] = []
    model.score(X_te[:1])
    if champ_model is not None:
        champ_model.score(X_te[:1])
    for r in range(3):
        t_c: list[float] = []
        t_h: list[float] = []
        for i in range(r * 100, r * 100 + 100):
            t0 = time.perf_counter()
            model.score(X_te[i : i + 1])
            t_c.append((time.perf_counter() - t0) * 1000)
            if champ_model is not None:
                t0 = time.perf_counter()
                champ_model.score(X_te[i : i + 1])
                t_h.append((time.perf_counter() - t0) * 1000)
        rounds.append((float(np.percentile(t_c, 95)), float(np.percentile(t_h, 95)) if t_h else None))
    p95, ref = min(rounds, key=lambda x: x[0])
    over_abs = p95 > policy.latency_p95_budget_ms
    over_rel = ref is None or p95 > policy.latency_p95_ratio_vs_champion * ref
    ok = not (over_abs and over_rel)
    checks.append(
        Check(
            "latency_p95_ms",
            ok,
            {
                "candidate_p95": round(p95, 3),
                "champion_p95": round(ref, 3) if ref is not None else None,
                "rounds_candidate_p95": [round(c, 3) for c, _ in rounds],
            },
            f"<= {policy.latency_p95_budget_ms} ms, or <= {policy.latency_p95_ratio_vs_champion}x champion",
            "within budget"
            if not over_abs
            else (
                "over the absolute budget but in line with the champion measured alongside (machine load)"
                if ok
                else "single-row latency exceeds the budget and is out of line with the champion"
            ),
        )
    )

    # 6. Quality: absolute calibration + support, then non-inferiority vs champion.
    ct = cm["metrics"]["test"]
    ok = ct["positives"] >= policy.min_test_positives
    checks.append(
        Check(
            "test_support",
            ok,
            ct["positives"],
            policy.min_test_positives,
            "enough positives to judge" if ok else "too few test positives to evaluate",
        )
    )
    ok = ct["ece"] <= policy.max_ece
    checks.append(
        Check(
            "calibration_ece",
            ok,
            ct["ece"],
            policy.max_ece,
            "calibrated" if ok else "expected calibration error too high",
        )
    )

    champ = store.get_model(champion_version) if champion_version else None
    if champ is None:
        checks.append(Check("noninferiority", True, None, None, "no champion: first deployment", skipped=True))
    else:
        ht = champ["manifest"]["metrics"]["test"]
        for metric, margin in (
            ("pr_auc", policy.max_pr_auc_drop),
            ("recall", policy.max_recall_drop),
            ("f1", policy.max_f1_drop),
        ):
            delta = round(ct[metric] - ht[metric], 4)
            ok = delta >= -margin
            checks.append(
                Check(
                    f"{metric}_vs_champion",
                    ok,
                    {"candidate": ct[metric], "champion": ht[metric], "delta": delta},
                    f">= -{margin}",
                    "non-inferior" if ok else f"{metric} is {abs(delta):.3f} below the champion (margin {margin})",
                )
            )
        worst = None
        for name, cs in ct.get("slices", {}).items():
            hs = ht.get("slices", {}).get(name)
            if not hs:
                continue
            if min(cs["positives"], hs["positives"]) < policy.min_slice_positives:
                continue
            d = round(cs["recall"] - hs["recall"], 4)
            if worst is None or d < worst[1]:
                worst = (name, d)
            if d < -policy.max_slice_recall_drop:
                checks.append(
                    Check(
                        f"slice_recall:{name}",
                        False,
                        {"candidate": cs["recall"], "champion": hs["recall"], "delta": d},
                        f">= -{policy.max_slice_recall_drop}",
                        f"recall on slice {name} is {abs(d):.3f} below the champion",
                    )
                )
        checks.append(
            Check(
                "slice_recall_worst",
                worst is None or worst[1] >= -policy.max_slice_recall_drop,
                {"slice": worst[0], "delta": worst[1]} if worst else None,
                f">= -{policy.max_slice_recall_drop}",
                "no slice regresses beyond tolerance"
                if worst is None or worst[1] >= -policy.max_slice_recall_drop
                else "at least one slice regresses beyond tolerance",
            )
        )
    return _report(candidate_version, champion_version, checks)


def _report(candidate: str, champion: str | None, checks: list[Check]) -> dict[str, Any]:
    failed = [c for c in checks if not c.passed and not c.skipped]
    return {
        "candidate": candidate,
        "champion": champion,
        "passed": not failed,
        "blocking_reasons": [f"{c.name}: {c.reason}" for c in failed],
        "checks": [asdict(c) for c in checks],
        "features": list(FEATURE_NAMES),
    }
