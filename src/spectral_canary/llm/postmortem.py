"""Blameless postmortem for an LLM-lane rollback, generated from the hash-chained event log and request telemetry
(no hand-written numbers). Same shape and file names as ``spectral_canary.postmortem``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..postmortem import _summary, _ts
from ..store import Store


def build_llm_postmortem(store: Store, release_id: str) -> dict[str, Any]:
    rel = store.get_release(release_id) or {}
    events = store.events(release_id=release_id, limit=100_000)
    rb = next((e for e in events if e["kind"] == "rollback.executed"), None)
    breach = None
    if rb is not None:
        breach = next(
            (
                e
                for e in reversed(events)
                if e["kind"] == "guardrail.evaluated"
                and e["payload"].get("verdict") == "breach"
                and e["seq"] < rb["seq"]
            ),
            None,
        )
    verify = next((e for e in events if e["kind"] in ("rollback.verified", "rollback.verification_failed")), None)
    reqs = store.requests_since(0, release_id)
    cand_served = [
        r for r in reqs if r["arm"] == "candidate" and r.get("served_version") == rel.get("candidate_version")
    ]
    fallbacks = [r for r in reqs if r["arm"] == "candidate" and r.get("fallback")]
    pos = [r for r in cand_served if r.get("label") and r["label"]["escalate"]]
    missed = sum(1 for r in pos if not r["served"]["escalate"] and (r.get("champion") or {}).get("escalate"))
    tokens_c = sum(
        (r["served"] or {}).get("input_tokens", 0) + (r["served"] or {}).get("output_tokens", 0) for r in cand_served
    )
    return {
        "lane": "llm",
        "release": {
            k: rel.get(k)
            for k in ("release_id", "candidate_version", "champion_version", "state", "reason", "created_by")
        },
        "trigger": rb["payload"]["trigger"] if rb else None,
        "rollback_event": rb,
        "breach_evaluation": breach,
        "verification": verify["payload"] if verify else None,
        "impact": {
            "requests_in_release": len(reqs),
            "requests_served_by_candidate": len(cand_served),
            "candidate_fallbacks_to_champion": len(fallbacks),
            "tokens_spent_by_candidate_answers": tokens_c,
            "labeled_must_escalate_served_by_candidate": len(pos),
            "escalations_missed_by_candidate_that_champion_made": missed,
            "simulated_traffic_share": round(sum(r["simulated"] for r in reqs) / len(reqs), 4) if reqs else 0.0,
        },
        "timeline": [
            {
                "seq": e["seq"],
                "ts": e["ts"],
                "kind": e["kind"],
                "actor": e["actor"],
                "simulated": e["simulated"],
                "summary": _llm_summary(e),
            }
            for e in events
        ],
        "event_hashes": {
            "first": events[0]["hash"] if events else None,
            "last": events[-1]["hash"] if events else None,
        },
    }


def _llm_summary(e: dict[str, Any]) -> str:
    p = e["payload"]
    if e["kind"] == "release.shadow_evaluated":
        return (
            f"shadow: {p['requests']} requests, candidate failure rate {p['error_rate']}, escalate agreement "
            f"{p.get('escalate_agreement')}, passed={p['passed']}"
        )
    if e["kind"] == "guardrail.evaluated":
        s = f"window {p['window_index']}: {p['verdict']}"
        if p.get("breaches"):
            s += " — " + "; ".join(p["breaches"])
        return s
    return _summary(e)


def render_llm_markdown(pm: dict[str, Any]) -> str:
    r, imp = pm["release"], pm["impact"]
    lines = [
        f"# Postmortem: {r['release_id']} ({r['candidate_version']} vs champion {r['champion_version']})",
        "",
        "_LLM triage lane. Generated from the hash-chained event log by `spectral_canary.llm.postmortem`. Blameless._",
        "",
        f"> Alert traffic in this release was {imp['simulated_traffic_share']:.0%} **simulated** (seeded synthetic "
        "alerts with rule-derived labels). Every model call, grade, routing decision, SLO check and the rollback "
        "are real executions against a local Ollama server.",
        "",
        "## Summary",
        "",
        f"- **Outcome:** {r['state']}",
        f"- **Trigger:** {pm['trigger']}",
        f"- **Reason recorded:** {r['reason']}",
    ]
    v = pm["verification"]
    if v:
        lines.append(
            f"- **Recovery verification:** {v['served_by_expected']}/{v['requests']} real triage calls "
            f"answered by `{v['expected']}`; readiness={v['health']['ready']}; passed={v['passed']}"
        )
    lines += [
        "",
        "## Impact (from request telemetry)",
        "",
        f"- Requests in release: {imp['requests_in_release']}",
        f"- Answered by the candidate: {imp['requests_served_by_candidate']} "
        f"(plus {imp['candidate_fallbacks_to_champion']} candidate failures answered by the champion)",
        f"- Tokens spent on candidate answers: {imp['tokens_spent_by_candidate_answers']}",
        "- Labeled must-escalate alerts answered by the candidate: "
        f"{imp['labeled_must_escalate_served_by_candidate']}; "
        f"escalations it missed that the champion made: {imp['escalations_missed_by_candidate_that_champion_made']}",
        "",
        "## Detection evidence",
        "",
    ]
    b = pm["breach_evaluation"]
    if b:
        p = b["payload"]
        c, h = p["arms"]["candidate"], p["arms"]["champion"]
        pr = (p.get("labeled") or {}).get("paired") or {}
        lines += [
            f"Window {p['window_index']} ({p['window_requests']} routed requests):",
            "",
            "| signal | candidate | champion arm |",
            "|---|---|---|",
            f"| model calls | {c['calls']} | {h['calls']} |",
            f"| failures (error/timeout/invalid) | {c['failures']} | {h['failures']} |",
            f"| model-call p95 ms | {c['latency_ms']['p95']} | {h['latency_ms']['p95']} |",
            f"| mean input / output tokens | {c['input_tokens_mean']} / {c['output_tokens_mean']} | "
            f"{h['input_tokens_mean']} / {h['output_tokens_mean']} |",
            f"| notional cost per 1k calls (USD) | {c['cost_usd_per_1k_calls']} | {h['cost_usd_per_1k_calls']} |",
            f"| escalation rate | {c['escalation_rate']} | {h['escalation_rate']} |",
            "",
        ]
        if pr:
            lines += [
                f"Paired online grading so far: {pr.get('labeled')} labeled candidate answers; on "
                f"{pr.get('escalations')} must-escalate alerts the candidate escalated "
                f"{pr.get('candidate_escalated')}, the champion {pr.get('champion_would_have_escalated')} "
                f"(McNemar p = {pr.get('mcnemar_p_one_sided')}).",
                "",
            ]
        lines += ["Breaches: " + "; ".join(p["breaches"]), ""]
    else:
        lines.append("No guardrail breach precedes this rollback (manual or non-guardrail trigger).")
    lines += [
        "",
        "## Timeline",
        "",
        "| seq | time | event | actor | simulated | summary |",
        "|---|---|---|---|---|---|",
    ]
    for t in pm["timeline"]:
        lines.append(
            f"| {t['seq']} | {_ts(t['ts'])} | `{t['kind']}` | {t['actor']} | "
            f"{'yes' if t['simulated'] else 'no'} | {t['summary'].replace('|', '/')} |"
        )
    br = " ".join((b or {}).get("payload", {}).get("breaches", []) if b else [])
    lines += ["", "## What went well / what to change", ""]
    if "SLO" in br:
        lines += [
            "- The SLOs compared both arms in the same window on the same machine, so a busy laptop alone could not",
            "  trigger the rollback.",
            "- Gap: the offline gate report already showed the cost/latency ratio, but the policy treats it as",
            "  advisory (`cost_latency_blocking = false`). Teams that never accept a costlier model should make it",
            "  blocking.",
        ]
    if "online grading" in br:
        lines.append(
            "- Paired online grading (champion run on the same alerts) caught a quality regression offline missed."
        )
    if "input drift" in br:
        lines.append(
            "- The input mix left the envelope the bundle was validated on; the policy returns to the champion "
            "until the gold set covers the new mix."
        )
    lines += [
        "- Rollback was one compare-and-swap of the lane's deployment pointer; verification used real model calls.",
        "",
        f"Event chain: first `{(pm['event_hashes']['first'] or '')[:16]}…` "
        f"last `{(pm['event_hashes']['last'] or '')[:16]}…`",
    ]
    return "\n".join(lines) + "\n"


def write_llm_postmortem(store: Store, release_id: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    pm = build_llm_postmortem(store, release_id)
    (out_dir / f"postmortem-{release_id}.json").write_text(json.dumps(pm, indent=2, default=str))
    path = out_dir / f"postmortem-{release_id}.md"
    path.write_text(render_llm_markdown(pm))
    return path
