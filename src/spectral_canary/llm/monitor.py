"""Shadow summary, canary guardrails (online code grading + latency/cost SLOs) and input drift for the LLM lane.

Definitions:
- *Candidate failure*: the candidate call errored, timed out, was unavailable, or its reply failed the output
  contract (``invalid_output``). The caller still got the champion's answer (fallback).
- *Latency SLO*: candidate model-call p95 above the absolute budget AND above ratio x the champion arm's
  model-call p95 in the same window (both arms share the machine, so a whole-machine slowdown does not count).
- *Cost SLO*: candidate mean notional cost per call above ratio x the champion arm's mean in the same window.
- *Online grading (labeled, delayed, paired)*: on candidate-served requests whose analyst label has arrived, the
  champion was also run on the same alert. Escalation recall is compared with an exact one-sided McNemar test.
- *Input drift*: PSI of alert fields in the window vs a fixed reference sample of the baseline traffic mix the
  bundles were validated against. With ``drift_action = "rollback"`` a major shift is a breach: the offline
  evidence that justified the canary no longer describes the traffic, so the release returns to the champion.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import numpy as np

from ..metrics import categorical_psi, mcnemar_exact_p, percentiles, psi
from .alerts import MERCHANTS, iter_traffic
from .evals import cost_units
from .policy import LLMCanaryPolicy, LLMShadowPolicy

REFERENCE_SEED = 424242
REFERENCE_N = 3000
PSI_MODERATE = 0.10


def _ok(call: dict[str, Any] | None) -> bool:
    return bool(call) and call.get("status") == "ok"  # type: ignore[union-attr]


def _cost(call: dict[str, Any], price: dict[str, float]) -> float:
    return cost_units(call.get("input_tokens", 0), call.get("output_tokens", 0), price) / 1000


def _call_stats(calls: list[dict[str, Any]], price: dict[str, float]) -> dict[str, Any]:
    ok = [c for c in calls if _ok(c)]
    return {
        "calls": len(calls),
        "ok": len(ok),
        "failures": len(calls) - len(ok),
        "invalid_outputs": sum(1 for c in calls if c.get("status") == "invalid_output"),
        "latency_ms": percentiles([c["latency_ms"] for c in ok]),
        "input_tokens_mean": round(float(np.mean([c["input_tokens"] for c in ok])), 1) if ok else None,
        "output_tokens_mean": round(float(np.mean([c["output_tokens"] for c in ok])), 1) if ok else None,
        "cost_usd_per_1k_calls": round(float(np.mean([_cost(c, price) for c in ok])), 5) if ok else None,
        "escalation_rate": round(float(np.mean([bool(c.get("escalate")) for c in ok])), 4) if ok else None,
    }


def _paired(rows: list[dict[str, Any]], cand_version: str | None) -> dict[str, Any]:
    """Candidate-served, champion-paired, labeled rows: escalation recall and full correctness."""
    pr = [
        r
        for r in rows
        if r["arm"] == "candidate"
        and r.get("served_version") == cand_version
        and _ok(r.get("champion"))
        and r.get("label") is not None
    ]
    pos = [r for r in pr if r["label"]["escalate"]]
    b = sum(1 for r in pos if r["champion"]["escalate"] and not r["served"]["escalate"])
    c = sum(1 for r in pos if r["served"]["escalate"] and not r["champion"]["escalate"])

    def correct(call: dict[str, Any], lab: dict[str, Any]) -> bool:
        return call.get("reason_code") == lab["reason_code"] and call.get("escalate") == lab["escalate"]

    cc = sum(1 for r in pr if correct(r["served"], r["label"]))
    hc = sum(1 for r in pr if correct(r["champion"], r["label"]))
    return {
        "labeled": len(pr),
        "escalations": len(pos),
        "candidate_escalated": sum(1 for r in pos if r["served"]["escalate"]),
        "champion_would_have_escalated": sum(1 for r in pos if r["champion"]["escalate"]),
        "candidate_recall": round(sum(1 for r in pos if r["served"]["escalate"]) / len(pos), 4) if pos else None,
        "champion_recall": round(sum(1 for r in pos if r["champion"]["escalate"]) / len(pos), 4) if pos else None,
        "discordant_champion_only": b,
        "discordant_candidate_only": c,
        "mcnemar_p_one_sided": round(mcnemar_exact_p(b, c), 6),
        "candidate_correct": cc,
        "champion_correct": hc,
        "candidate_accuracy": round(cc / len(pr), 4) if pr else None,
        "champion_accuracy": round(hc / len(pr), 4) if pr else None,
    }


def llm_shadow_summary(rows: list[dict[str, Any]], policy: LLMShadowPolicy, price: dict[str, float]) -> dict[str, Any]:
    sh = [r for r in rows if r["arm"] == "champion" and r["status"] == "ok" and r.get("candidate")]
    both = [r for r in sh if _ok(r["candidate"]) and _ok(r.get("served"))]
    cand = _call_stats([r["candidate"] for r in sh], price)
    champ = _call_stats([r["served"] for r in sh], price)
    lab = [r for r in both if r.get("label") is not None and r["label"]["escalate"]]
    cr = sum(1 for r in lab if r["candidate"]["escalate"]) / len(lab) if lab else None
    hr = sum(1 for r in lab if r["served"]["escalate"]) / len(lab) if lab else None
    n = len(sh)
    err = cand["failures"] / n if n else 0.0
    out: dict[str, Any] = {
        "requests": n,
        "candidate_failures": cand["failures"],
        "error_rate": round(err, 4),
        "reason_agreement": round(
            float(np.mean([r["candidate"]["reason_code"] == r["served"]["reason_code"] for r in both])), 4
        )
        if both
        else None,
        "escalate_agreement": round(
            float(np.mean([r["candidate"]["escalate"] == r["served"]["escalate"] for r in both])), 4
        )
        if both
        else None,
        "candidate": cand,
        "champion": champ,
        "labeled_escalations": len(lab),
        "candidate_recall_on_labeled": None if cr is None else round(cr, 4),
        "champion_recall_on_labeled": None if hr is None else round(hr, 4),
        "returned_predictions_from": "champion only (shadow never influences responses)",
    }
    reasons = []
    if n < policy.min_requests:
        reasons.append(f"insufficient shadow requests: {n} < {policy.min_requests}")
    if err > policy.max_error_rate:
        reasons.append(f"candidate failure rate {err:.3f} > {policy.max_error_rate} (errors + invalid outputs)")
    enough = len(lab) >= policy.min_labeled_escalations and cr is not None and hr is not None
    if enough and cr is not None and hr is not None and cr < hr - policy.max_escalation_recall_drop:
        reasons.append(
            f"candidate escalation recall {cr:.2f} < champion {hr:.2f} - "
            f"{policy.max_escalation_recall_drop} on {len(lab)} labeled escalations"
        )
    out["passed"] = not reasons
    out["reasons"] = reasons
    # keys the shared postmortem/event summarizers read
    out["score_js_divergence"] = None
    out["decision_disagreement"] = (
        None if out["escalate_agreement"] is None else round(1 - out["escalate_agreement"], 4)
    )
    return out


def evaluate_llm_window(
    window: list[dict[str, Any]],
    cumulative: list[dict[str, Any]],
    policy: LLMCanaryPolicy,
    idx: int,
    cand_version: str | None,
    price: dict[str, float],
) -> dict[str, Any]:
    cand_rows = [r for r in window if r["arm"] == "candidate" and r.get("candidate")]
    champ_rows = [r for r in window if r["arm"] == "champion" and r.get("champion")]
    cand = _call_stats([r["candidate"] for r in cand_rows], price)
    champ = _call_stats([r["champion"] for r in champ_rows], price)
    cand["requests"], champ["requests"] = len([r for r in window if r["arm"] == "candidate"]), len(champ_rows)
    cand["error_rate"] = round(cand["failures"] / cand["calls"], 4) if cand["calls"] else 0.0
    champ["error_rate"] = round(champ["failures"] / champ["calls"], 4) if champ["calls"] else 0.0
    paired = _paired(cumulative, cand_version)
    drift = input_drift(window, policy.drift_psi_major)
    breaches: list[str] = []
    insufficient: list[str] = []
    if cand["calls"] < policy.min_candidate_requests:
        insufficient.append(f"candidate requests {cand['calls']} < {policy.min_candidate_requests}")
    else:
        if cand["error_rate"] > policy.max_error_rate:
            breaches.append(
                f"candidate failure rate {cand['error_rate']:.3f} > budget {policy.max_error_rate} "
                f"({cand['invalid_outputs']} invalid outputs)"
            )
        p95, ref = cand["latency_ms"]["p95"], champ["latency_ms"]["p95"]
        if p95 and ref and p95 > policy.latency_p95_budget_ms and p95 > policy.latency_p95_ratio_vs_champion * ref:
            breaches.append(
                f"latency SLO: candidate model p95 {p95:.0f} ms > budget {policy.latency_p95_budget_ms:.0f} "
                f"ms and {p95 / ref:.2f}x the champion's {ref:.0f} ms in the same window "
                f"(limit {policy.latency_p95_ratio_vs_champion}x)"
            )
        cc, hc = cand["cost_usd_per_1k_calls"], champ["cost_usd_per_1k_calls"]
        if cc and hc:
            cand["cost_ratio_vs_champion"] = round(cc / hc, 3)
            if cc > policy.cost_ratio_vs_champion * hc:
                breaches.append(
                    f"token-cost SLO: candidate ${cc:.4f} per 1k calls is {cc / hc:.2f}x the champion's "
                    f"${hc:.4f} (limit {policy.cost_ratio_vs_champion}x; output tokens "
                    f"{cand['output_tokens_mean']} vs {champ['output_tokens_mean']})"
                )
    labels_sufficient = paired["escalations"] >= policy.min_paired_escalations
    if not labels_sufficient:
        insufficient.append(
            f"paired labeled escalations {paired['escalations']} < {policy.min_paired_escalations}"
            " (delayed labels): no quality claim made"
        )
    elif paired["mcnemar_p_one_sided"] < policy.mcnemar_alpha and (paired["candidate_recall"] or 0) < (
        paired["champion_recall"] or 0
    ):
        breaches.append(
            f"online grading: on {paired['escalations']} labeled must-escalate alerts served by the "
            f"candidate it escalated {paired['candidate_escalated']} vs "
            f"{paired['champion_would_have_escalated']} for the champion "
            f"(McNemar p={paired['mcnemar_p_one_sided']:.2g})"
        )
    drift_breach = (
        policy.drift_action == "rollback" and drift["status"] == "major" and drift["n"] >= policy.drift_min_rows
    )
    if drift_breach:
        top = sorted(drift["features"].items(), key=lambda kv: -kv[1]["psi"])[:3]
        breaches.append(
            "input drift: alert mix moved outside the validated envelope (max PSI "
            f"{drift['max_psi']}: " + ", ".join(f"{k} {v['psi']}" for k, v in top) + ")"
        )
    verdict = (
        "breach"
        if breaches
        else ("insufficient_evidence" if any(s.startswith("candidate requests") for s in insufficient) else "healthy")
    )
    return {
        "window_index": idx,
        "verdict": verdict,
        "breaches": breaches,
        "insufficient": insufficient,
        "labels_sufficient": labels_sufficient,
        "window_requests": len(window),
        "arms": {"candidate": cand, "champion": champ},
        "labeled_cumulative": {"paired": paired},
        "drift": drift,
        "drift_is_breach": drift_breach,
        "simulated_traffic_share": round(float(np.mean([r.get("simulated", 0) for r in window])), 4)
        if window
        else None,
    }


# -- input drift ---------------------------------------------------------------------------------------------
def _encode(fields: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    def col(fn: Any) -> np.ndarray:
        return np.array([fn(f) for f in fields], dtype=float)

    return {
        "amount": col(lambda f: f["amount"]),
        "card_not_present": col(lambda f: f["channel"] == "card_not_present"),
        "new_device": col(lambda f: f["device_age_hours"] < 24),
        "recent_password_reset": col(
            lambda f: f["password_reset_hours_ago"] is not None and f["password_reset_hours_ago"] < 24
        ),
        "new_ship_to": col(lambda f: f["ship_to_added_hours_ago"] is not None and f["ship_to_added_hours_ago"] < 24),
        "micro_auths_bucket": col(lambda f: min(2, (f["small_auths_1h"] >= 1) + (f["small_auths_1h"] >= 3))),
        "declines_bucket": col(lambda f: min(3, f["declines_1h"])),
        "chargebacks_bucket": col(lambda f: min(2, f["prior_chargebacks"])),
        "merchant": col(lambda f: MERCHANTS.index(f["merchant"]) if f["merchant"] in MERCHANTS else 0),
    }


_K = {
    "card_not_present": 2,
    "new_device": 2,
    "recent_password_reset": 2,
    "new_ship_to": 2,
    "micro_auths_bucket": 3,
    "declines_bucket": 4,
    "chargebacks_bucket": 3,
    "merchant": len(MERCHANTS),
}


@lru_cache(maxsize=1)
def _reference() -> dict[str, np.ndarray]:
    return _encode([t.payload["alert"]["fields"] for t in iter_traffic("reference", REFERENCE_N, REFERENCE_SEED)])


def input_drift(rows: list[dict[str, Any]], major: float = 0.25) -> dict[str, Any]:
    fields = [r["alert"]["fields"] for r in rows if r.get("alert") and r["alert"].get("fields")]
    if not fields:
        return {"n": 0, "status": "no_data", "features": {}, "max_psi": None}
    live, ref = _encode(fields), _reference()
    feats = {}
    worst = 0.0
    for name, arr in live.items():
        v = psi(ref[name], arr) if name == "amount" else categorical_psi(ref[name], arr, _K[name])
        worst = max(worst, v)
        feats[name] = {
            "psi": round(v, 4),
            "reference_mean": round(float(ref[name].mean()), 4),
            "live_mean": round(float(arr.mean()), 4),
        }
    status = "major" if worst > major else ("moderate" if worst > PSI_MODERATE else "stable")
    return {
        "n": len(fields),
        "features": feats,
        "max_psi": round(worst, 4),
        "status": status,
        "reference": f"{REFERENCE_N} alerts from the baseline traffic mix, seed {REFERENCE_SEED}",
        "caveat": "fewer than 150 rows: PSI is noisy" if len(fields) < 150 else None,
    }
