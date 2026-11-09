"""Offline gate for prompt bundles: integrity, then the regression suite with code graders and pass^k.

Report shape matches ``gates.run_offline_gate`` (``passed``, ``blocking_reasons``, ``checks`` with
name/passed/observed/limit/reason) so the controller, events, postmortem and UI treat both lanes alike.
Checks marked ``blocking: false`` are reported and never block (the smoke subset always; cost/latency unless
the policy sets ``cost_latency_blocking``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..artifacts import ArtifactError
from .bundles import BundlePool
from .client import Backend
from .evals import run_suite, summarize
from .policy import LLMReleasePolicy


def _check(name: str, passed: bool, observed: Any, limit: Any, reason: str, blocking: bool = True) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "observed": observed,
        "limit": limit,
        "reason": reason,
        "blocking": blocking,
    }


def run_llm_offline_gate(
    candidate: str,
    champion: str | None,
    pool: BundlePool,
    backend: Backend,
    cases: list[dict[str, Any]],
    gold_sha: str,
    policy: LLMReleasePolicy,
    eval_dir: Path,
    log: Any = None,
) -> dict[str, Any]:
    gp = policy.offline_gate
    checks: list[dict[str, Any]] = []
    try:
        cand = pool.get(candidate)
        checks.append(
            _check(
                "bundle_integrity",
                True,
                cand.sha256[:16],
                "registered sha256 + HMAC",
                "bundle bytes and signature verified",
            )
        )
    except ArtifactError as exc:
        checks.append(
            _check("bundle_integrity", False, str(exc), "registered sha256 + HMAC", "bundle failed verification")
        )
        return _report(candidate, champion, checks, None, None, gold_sha)
    digest = backend.model_digest(cand.model)
    pinned = cand.bundle.get("model_digest")
    checks.append(
        _check(
            "model_weights_pinned",
            digest == pinned,
            (digest or "missing")[:16],
            (pinned or "")[:16],
            "the weights behind the model tag must be the ones registered with the bundle",
        )
    )
    if digest != pinned:
        return _report(candidate, champion, checks, None, None, gold_sha)

    rows, path, reused = run_suite(
        cand, cases, backend, gp.k, eval_dir, gold_sha, gp.concurrency, policy.model_timeout_ms / 1000, log
    )
    cs = summarize(rows, gp.k, policy.price_dict)
    cs["raw_results"] = path.name
    cs["reused_previous_run"] = reused
    hs = None
    if champion:
        champ = pool.get(champion)
        hrows, hpath, hreused = run_suite(
            champ, cases, backend, gp.k, eval_dir, gold_sha, gp.concurrency, policy.model_timeout_ms / 1000, log
        )
        hs = summarize(hrows, gp.k, policy.price_dict)
        hs["raw_results"] = hpath.name
        hs["reused_previous_run"] = hreused

    sv = cs["schema_valid"]["rate"]
    checks.append(
        _check(
            "schema_validity",
            sv >= gp.min_schema_valid,
            sv,
            gp.min_schema_valid,
            "share of all tries that parse as the output contract (JSON, enum, boolean, rationale)",
        )
    )
    er, rr, pk = cs["escalation_recall"]["rate"], cs["reason_accuracy"]["rate"], cs["correct_pass_k"]["rate"]
    if hs is None:
        checks.append(
            _check(
                "escalation_recall_floor",
                er >= gp.min_escalation_recall,
                er,
                gp.min_escalation_recall,
                "bootstrap sanity floor on should-escalate cases (no champion to compare)",
            )
        )
        checks.append(
            _check(
                "reason_accuracy_floor",
                rr >= gp.min_reason_accuracy,
                rr,
                gp.min_reason_accuracy,
                "bootstrap sanity floor",
            )
        )
    else:
        her, hrr, hpk = hs["escalation_recall"]["rate"], hs["reason_accuracy"]["rate"], hs["correct_pass_k"]["rate"]
        lim = round(her - gp.max_escalation_recall_drop, 4)
        checks.append(
            _check(
                "escalation_recall_high_risk",
                er >= lim,
                er,
                lim,
                f"recall on all should-escalate cases vs champion {her} - {gp.max_escalation_recall_drop}",
            )
        )
        for seg, s in cs["segments"].items():
            h = hs["segments"][seg]
            if s["cases"] < gp.min_segment_cases or h["rate"] is None:
                checks.append(
                    _check(
                        f"segment_recall:{seg}",
                        True,
                        s["rate"],
                        None,
                        f"insufficient support ({s['cases']} cases < {gp.min_segment_cases}); not judged",
                        blocking=False,
                    )
                )
                continue
            lim = round(h["rate"] - gp.max_segment_recall_drop, 4)
            checks.append(
                _check(
                    f"segment_recall:{seg}",
                    s["rate"] >= lim,
                    s["rate"],
                    lim,
                    f"escalation recall in segment {seg} ({s['cases']} cases x k) vs champion {h['rate']}",
                )
            )
        fe, hfe = cs["false_escalation_rate"]["rate"], hs["false_escalation_rate"]["rate"]
        lim = round(hfe + gp.max_false_escalation_increase, 4)
        checks.append(
            _check(
                "false_escalation_rate",
                fe <= lim,
                fe,
                lim,
                f"escalations of cases that should not escalate vs champion {hfe}",
            )
        )
        lim = round(hrr - gp.max_reason_accuracy_drop, 4)
        checks.append(_check("reason_accuracy", rr >= lim, rr, lim, f"pass@1 reason code accuracy vs champion {hrr}"))
        lim = round(hpk - gp.max_pass_k_drop, 4)
        checks.append(
            _check(
                f"pass^{gp.k}",
                pk >= lim,
                pk,
                lim,
                f"share of cases fully correct on all {gp.k} tries vs champion {hpk}",
            )
        )
        cr = cs["cost_usd_per_1k_calls"] / hs["cost_usd_per_1k_calls"] if hs["cost_usd_per_1k_calls"] else None
        lr = (cs["latency_ms"]["p95"] / hs["latency_ms"]["p95"]) if hs["latency_ms"]["p95"] else None
        checks.append(
            _check(
                "cost_per_call_vs_champion",
                cr is None or cr <= gp.max_cost_ratio_vs_champion,
                None if cr is None else round(cr, 3),
                gp.max_cost_ratio_vs_champion,
                "notional token cost ratio; binding check is the canary SLO"
                if not gp.cost_latency_blocking
                else "notional token cost ratio",
                blocking=gp.cost_latency_blocking,
            )
        )
        checks.append(
            _check(
                "latency_p95_vs_champion",
                lr is None or lr <= gp.max_latency_p95_ratio_vs_champion,
                None if lr is None else round(lr, 3),
                gp.max_latency_p95_ratio_vs_champion,
                "sequential offline p95 on a shared machine; binding check is the canary SLO"
                if not gp.cost_latency_blocking
                else "offline p95 ratio",
                blocking=gp.cost_latency_blocking,
            )
        )
    sm = cs["smoke"]
    checks.append(
        _check(
            "smoke_subset",
            sm["passed_cases"] >= gp.smoke_min_passed,
            f"{sm['passed_cases']}/{sm['cases']}",
            f">={gp.smoke_min_passed}/{sm['cases']}",
            "informational: the 12-case textbook smoke check a quick review would run (escalate decision, try 0)",
            blocking=False,
        )
    )
    return _report(candidate, champion, checks, cs, hs, gold_sha)


def _report(
    candidate: str,
    champion: str | None,
    checks: list[dict[str, Any]],
    cs: dict[str, Any] | None,
    hs: dict[str, Any] | None,
    gold_sha: str,
) -> dict[str, Any]:
    blocking = [
        f"{c['name']}: {c['reason']} (observed {c['observed']}, limit {c['limit']})"
        for c in checks
        if c["blocking"] and not c["passed"]
    ]
    return {
        "lane": "llm",
        "candidate": candidate,
        "champion": champion,
        "passed": not blocking,
        "blocking_reasons": blocking,
        "checks": checks,
        "candidate_summary": cs,
        "champion_summary": hs,
        "gold_sha256": gold_sha,
    }
