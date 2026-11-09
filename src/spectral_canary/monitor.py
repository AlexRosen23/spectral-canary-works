"""Shadow comparison, canary guardrails and feature drift, computed from recorded request rows.

Definitions (also in docs/DRIFT_AND_LIMITATIONS.md):
- *Candidate arm* = requests whose request key hashes into the canary bucket. The arms are a randomized
  split of the same traffic, so arm-vs-arm comparisons are fair without labels.
- *Error rate* = candidate-arm requests where the candidate errored, timed out or was unavailable
  (the user still got a champion answer via fallback, but the candidate failed its job).
- *Flag-rate guardrail (label-free)*: breach when the candidate flags materially fewer transactions than
  the champion arm and the 95% Newcombe CI of the difference excludes 0.
- *Recall guardrail (labeled, delayed)*: on candidate-arm requests whose ground-truth label has arrived
  and is positive, the champion was also scored (paired). Breach when the exact one-sided McNemar test
  says the candidate misses significantly more frauds (p < 0.01) and recall delta < -margin.
- Drift (PSI per feature vs the training split) is reported as an alert, never as a rollback trigger:
  both arms see the same shifted traffic, so drift alone says nothing about the candidate.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from .data import load_split_csv
from .metrics import (
    categorical_psi,
    js_divergence,
    mcnemar_exact_p,
    newcombe_diff_interval,
    percentiles,
    psi,
    wilson_interval,
)
from .policy import CanaryPolicy, ShadowPolicy
from .schema import FEATURE_NAMES, FEATURES, MERCHANT_CATEGORIES, encode_records

PSI_MODERATE = 0.10
PSI_MAJOR = 0.25


def shadow_summary(rows: list[dict[str, Any]], policy: ShadowPolicy) -> dict[str, Any]:
    shadow = [r for r in rows if r.get("shadow_version") and r["status"] == "ok"]
    ok = [r for r in shadow if r.get("shadow_status") == "ok"]
    n = len(shadow)
    errors = n - len(ok)
    champ = np.array([r["score"] for r in ok], dtype=float)
    cand = np.array([r["shadow_score"] for r in ok], dtype=float)
    disagree = float(np.mean([r["decision"] != r["shadow_decision"] for r in ok])) if ok else 0.0
    js = js_divergence(champ, cand)
    out: dict[str, Any]
    labeled = [r for r in ok if r.get("label") is not None]
    pos = [r for r in labeled if r["label"] == 1]
    out = {
        "requests": n,
        "candidate_errors": errors,
        "error_rate": round(errors / n, 4) if n else 0.0,
        "score_js_divergence": round(js, 4),
        "decision_disagreement": round(disagree, 4),
        "champion_flag_rate": round(float(np.mean([r["decision"] for r in ok])), 4) if ok else None,
        "candidate_flag_rate": round(float(np.mean([r["shadow_decision"] for r in ok])), 4) if ok else None,
        "latency_ms": {
            "champion_handler": percentiles([r["latency_ms"] for r in ok]),
            "candidate_model": percentiles(
                [r["shadow_latency_ms"] for r in ok if r.get("shadow_latency_ms") is not None]
            ),
        },
        "labeled": len(labeled),
        "labeled_positives": len(pos),
        "champion_recall_on_labeled": round(float(np.mean([r["decision"] for r in pos])), 4) if pos else None,
        "candidate_recall_on_labeled": round(float(np.mean([r["shadow_decision"] for r in pos])), 4) if pos else None,
        "returned_predictions_from": "champion only (shadow never influences responses)",
    }
    reasons: list[str] = []
    if n < policy.min_requests:
        reasons.append(f"insufficient shadow requests: {n} < {policy.min_requests}")
    if out["error_rate"] > policy.max_error_rate:
        reasons.append(f"candidate error rate {out['error_rate']} > {policy.max_error_rate}")
    if js > policy.max_score_js_divergence:
        reasons.append(f"score JS divergence {js:.3f} > {policy.max_score_js_divergence}")
    if disagree > policy.max_decision_disagreement:
        reasons.append(f"decision disagreement {disagree:.3f} > {policy.max_decision_disagreement}")
    out["passed"] = not reasons
    out["reasons"] = reasons
    return out


def _arm_stats(rows: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    a = [r for r in rows if r["arm"] == arm and r["status"] == "ok"]
    if arm == "candidate":
        served = [r for r in a if r.get("shadow_status") == "ok"]
        failed = [r for r in a if r.get("shadow_status") in ("error", "timeout", "unavailable")]
    else:
        served, failed = a, []
    flags = int(sum(r["decision"] for r in served))
    col = "shadow_latency_ms" if arm == "candidate" else "champion_latency_ms"
    return {
        "requests": len(a),
        "served_by_arm_model": len(served),
        "model_failures": len(failed),
        "error_rate": round(len(failed) / len(a), 4) if a else 0.0,
        "flagged": flags,
        "flag_rate": round(flags / len(served), 4) if served else None,
        "flag_rate_ci95": [round(v, 4) for v in wilson_interval(flags, len(served))],
        "latency_ms": percentiles([r["latency_ms"] for r in served]),
        # model-call latency only (what the arm's model itself costs), not the whole handler
        "model_latency_ms": percentiles([r[col] for r in served if r.get(col) is not None]),
    }


def _labeled(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Labeled evidence: per-arm recall (independent) and paired candidate-vs-champion on candidate arm."""
    out: dict[str, Any] = {}
    for arm in ("candidate", "champion"):
        a = [
            r
            for r in rows
            if r["arm"] == arm
            and r["status"] == "ok"
            and r.get("label") is not None
            and (arm == "champion" or r.get("shadow_status") == "ok")
        ]
        pos = [r for r in a if r["label"] == 1]
        tp = int(sum(r["decision"] for r in pos))
        out[arm] = {
            "labeled": len(a),
            "labeled_positives": len(pos),
            "tp": tp,
            "recall": round(tp / len(pos), 4) if pos else None,
            "recall_ci95": [round(v, 4) for v in wilson_interval(tp, len(pos))],
        }
    c, h = out["candidate"], out["champion"]
    lo, hi = newcombe_diff_interval(c["tp"], c["labeled_positives"], h["tp"], h["labeled_positives"])
    out["arm_recall_diff_ci95"] = [round(lo, 4), round(hi, 4)]
    paired = [
        r
        for r in rows
        if r["arm"] == "candidate"
        and r.get("shadow_status") == "ok"
        and r.get("label") == 1
        and r.get("champion_decision") is not None
    ]
    b = sum(1 for r in paired if r["champion_decision"] == 1 and r["decision"] == 0)
    cc = sum(1 for r in paired if r["champion_decision"] == 0 and r["decision"] == 1)
    cand_tp = sum(r["decision"] for r in paired)
    champ_tp = sum(r["champion_decision"] for r in paired)
    out["paired"] = {
        "positives": len(paired),
        "candidate_caught": int(cand_tp),
        "champion_would_have_caught": int(champ_tp),
        "missed_by_candidate_only": b,
        "missed_by_champion_only": cc,
        "candidate_recall": round(cand_tp / len(paired), 4) if paired else None,
        "champion_recall": round(champ_tp / len(paired), 4) if paired else None,
        "mcnemar_p_one_sided": round(mcnemar_exact_p(b, cc), 6),
    }
    return out


def evaluate_canary_window(
    window_rows: list[dict[str, Any]],
    cumulative_rows: list[dict[str, Any]],
    policy: CanaryPolicy,
    window_index: int,
) -> dict[str, Any]:
    cand = _arm_stats(window_rows, "candidate")
    champ = _arm_stats(window_rows, "champion")
    labeled = _labeled(cumulative_rows)
    breaches: list[str] = []
    insufficient: list[str] = []

    if cand["requests"] < policy.min_candidate_requests:
        insufficient.append(f"candidate requests {cand['requests']} < {policy.min_candidate_requests}")
    else:
        if cand["error_rate"] > policy.max_error_rate:
            breaches.append(f"candidate error rate {cand['error_rate']:.3f} > budget {policy.max_error_rate}")
        p95 = cand["model_latency_ms"]["p95"]
        ref = champ["model_latency_ms"]["p95"]
        if (
            p95 is not None
            and ref
            and p95 > policy.latency_p95_budget_ms
            and p95 > policy.latency_p95_ratio_vs_champion * ref
        ):
            breaches.append(
                f"candidate model p95 latency {p95:.1f} ms > budget {policy.latency_p95_budget_ms} ms "
                f"and {p95 / ref:.1f}x the champion's {ref:.1f} ms in the same window"
            )
        if cand["flag_rate"] is not None and champ["flag_rate"]:
            ratio = cand["flag_rate"] / champ["flag_rate"]
            lo, hi = newcombe_diff_interval(
                cand["flagged"], cand["served_by_arm_model"], champ["flagged"], champ["served_by_arm_model"]
            )
            cand["flag_rate_ratio_vs_champion"] = round(ratio, 4)
            cand["flag_rate_diff_ci95"] = [round(lo, 4), round(hi, 4)]
            if ratio < 1 - policy.max_flag_rate_ratio_drop and hi < 0:
                breaches.append(
                    f"candidate flags {ratio:.2f}x the champion's rate (95% CI of difference "
                    f"[{lo:.3f}, {hi:.3f}] excludes 0): it is letting risky traffic through"
                )

    pr = labeled["paired"]
    c_pos, h_pos = labeled["candidate"]["labeled_positives"], labeled["champion"]["labeled_positives"]
    labels_sufficient = min(c_pos, h_pos) >= policy.min_labeled_positives_per_arm
    if not labels_sufficient:
        insufficient.append(
            f"labeled positives per arm {c_pos}/{h_pos} < {policy.min_labeled_positives_per_arm}"
            " (delayed labels): no accuracy claim made"
        )
    elif pr["candidate_recall"] is not None and pr["champion_recall"] is not None:
        delta = pr["candidate_recall"] - pr["champion_recall"]
        if pr["mcnemar_p_one_sided"] < 0.01 and delta < -policy.recall_noninferiority_margin:
            breaches.append(
                f"on {pr['positives']} labeled frauds served by the candidate, it caught "
                f"{pr['candidate_caught']} vs {pr['champion_would_have_caught']} for the champion "
                f"(recall {pr['candidate_recall']:.2f} vs {pr['champion_recall']:.2f}, "
                f"McNemar p={pr['mcnemar_p_one_sided']:.2g})"
            )

    verdict = (
        "breach"
        if breaches
        else ("insufficient_evidence" if any(s.startswith("candidate requests") for s in insufficient) else "healthy")
    )
    return {
        "window_index": window_index,
        "verdict": verdict,
        "breaches": breaches,
        "insufficient": insufficient,
        "labels_sufficient": labels_sufficient,
        "window_requests": len(window_rows),
        "arms": {"candidate": cand, "champion": champ},
        "labeled_cumulative": labeled,
        "simulated_traffic_share": round(float(np.mean([r.get("simulated", 0) for r in window_rows])), 4)
        if window_rows
        else None,
    }


@lru_cache(maxsize=4)
def _reference(train_csv: str) -> np.ndarray:
    X, _, _ = load_split_csv(Path(train_csv))
    return X


def drift_report(rows: list[dict[str, Any]], train_csv: Path) -> dict[str, Any]:
    recs = [json.loads(r["features_json"]) for r in rows if r.get("features_json")]
    if not recs:
        return {"n": 0, "features": {}, "status": "no_data"}
    X = encode_records(recs)
    ref = _reference(str(train_csv))
    feats: dict[str, Any] = {}
    worst = 0.0
    for j, f in enumerate(FEATURES):
        if f.kind == "category":
            v = categorical_psi(ref[:, j], X[:, j], len(MERCHANT_CATEGORIES))
        elif f.kind == "bool":
            v = categorical_psi(ref[:, j], X[:, j], 2)
        else:
            v = psi(ref[:, j], X[:, j])
        worst = max(worst, v)
        feats[f.name] = {
            "psi": round(v, 4),
            "status": "major" if v > PSI_MAJOR else ("moderate" if v > PSI_MODERATE else "stable"),
            "reference_mean": round(float(ref[:, j].mean()), 4),
            "live_mean": round(float(X[:, j].mean()), 4),
        }
    status = "major" if worst > PSI_MAJOR else ("moderate" if worst > PSI_MODERATE else "stable")
    caveat = "fewer than 500 rows: PSI is noisy" if len(recs) < 500 else None
    return {
        "n": len(recs),
        "features": feats,
        "max_psi": round(worst, 4),
        "status": status,
        "caveat": caveat,
        "thresholds": {"moderate": PSI_MODERATE, "major": PSI_MAJOR},
        "feature_order": list(FEATURE_NAMES),
    }
