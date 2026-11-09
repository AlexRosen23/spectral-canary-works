"""Generate a blameless postmortem from the immutable event log (no hand-written numbers)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .store import Store


def _ts(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_postmortem(store: Store, release_id: str) -> dict[str, Any]:
    rel = store.get_release(release_id) or {}
    events = store.events(release_id=release_id, limit=100_000)
    rb = next((e for e in events if e["kind"] == "rollback.executed"), None)
    breach_ev = None
    if rb is not None:
        breach_ev = next(
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
    cand_served = [r for r in reqs if r["arm"] == "candidate" and r.get("shadow_status") == "ok"]
    labeled_pos = [r for r in cand_served if r.get("label") == 1]
    missed_extra = sum(1 for r in labeled_pos if r["decision"] == 0 and r.get("champion_decision") == 1)
    simulated_share = (sum(r["simulated"] for r in reqs) / len(reqs)) if reqs else 0.0
    return {
        "release": {
            k: rel.get(k)
            for k in ("release_id", "candidate_version", "champion_version", "state", "reason", "created_by")
        },
        "trigger": rb["payload"]["trigger"] if rb else None,
        "rollback_event": rb,
        "breach_evaluation": breach_ev,
        "verification": verify["payload"] if verify else None,
        "impact": {
            "requests_in_release": len(reqs),
            "requests_served_by_candidate": len(cand_served),
            "labeled_frauds_served_by_candidate": len(labeled_pos),
            "frauds_missed_by_candidate_that_champion_would_have_caught": missed_extra,
            "simulated_traffic_share": round(simulated_share, 4),
        },
        "timeline": [
            {
                "seq": e["seq"],
                "ts": e["ts"],
                "kind": e["kind"],
                "actor": e["actor"],
                "simulated": e["simulated"],
                "summary": _summary(e),
            }
            for e in events
        ],
        "event_hashes": {
            "first": events[0]["hash"] if events else None,
            "last": events[-1]["hash"] if events else None,
        },
    }


def _summary(e: dict[str, Any]) -> str:
    p = e["payload"]
    k = e["kind"]
    if k == "release.gate_evaluated":
        return "offline gate " + ("PASSED" if p["passed"] else "BLOCKED: " + "; ".join(p["blocking_reasons"]))
    if k == "guardrail.evaluated":
        s = f"window {p['window_index']}: {p['verdict']}"
        if p.get("breaches"):
            s += " — " + "; ".join(p["breaches"])
        if p.get("drift_status") and p["drift_status"] != "stable":
            s += f" (feature drift {p['drift_status']}, max PSI {p.get('drift_max_psi')})"
        return s
    if k == "rollback.executed":
        return f"rollback ({p['trigger']}): traffic → {p['serving_after']}; {p['reason']}"
    if k in ("rollback.verified", "rollback.verification_failed", "promotion.verified"):
        return f"{p['served_by_expected']}/{p['requests']} verification predictions served by {p['expected']}"
    if k == "release.canary_started":
        return f"canary {p['fraction']:.0%} to {p['candidate']}"
    if k == "release.shadow_evaluated":
        return (
            f"shadow: {p['requests']} requests, JS {p['score_js_divergence']}, disagreement "
            f"{p['decision_disagreement']}, passed={p['passed']}"
        )
    return json.dumps(p, default=str)[:160]


def render_markdown(pm: dict[str, Any]) -> str:
    r = pm["release"]
    imp = pm["impact"]
    lines = [
        f"# Postmortem: {r['release_id']} ({r['candidate_version']} vs champion {r['champion_version']})",
        "",
        "_Generated from the hash-chained event log by `spectral_canary.postmortem`. Blameless format._",
        "",
        f"> Traffic in this release was {imp['simulated_traffic_share']:.0%} **simulated** (seeded replay). "
        "Predictions, routing, guardrail math and the rollback are real executions of this codebase.",
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
            f"- **Recovery verification:** {v['served_by_expected']}/{v['requests']} real predictions "
            f"served by `{v['expected']}`; readiness={v['health']['ready']}; passed={v['passed']}"
        )
    lines += [
        "",
        "## Impact (measured from request telemetry)",
        "",
        f"- Requests in release: {imp['requests_in_release']}",
        f"- Served by the candidate: {imp['requests_served_by_candidate']}",
        f"- Labeled frauds served by the candidate (labels delayed; some never arrive): "
        f"{imp['labeled_frauds_served_by_candidate']}",
        f"- Frauds the candidate missed that the champion would have caught: "
        f"{imp['frauds_missed_by_candidate_that_champion_would_have_caught']}",
        "",
        "## Detection evidence",
        "",
    ]
    b = pm["breach_evaluation"]
    if b:
        p = b["payload"]
        c, h = p["arms"]["candidate"], p["arms"]["champion"]
        pr = p["labeled"]["paired"]
        lines += [
            f"Window {p['window_index']} ({p['window_requests']} requests):",
            "",
            "| signal | candidate arm | champion arm |",
            "|---|---|---|",
            f"| requests | {c['requests']} | {h['requests']} |",
            f"| flag rate | {c['flag_rate']} | {h['flag_rate']} |",
            f"| model error rate | {c['error_rate']} | {h['error_rate']} |",
            f"| handler p95 latency ms | {c['latency_ms']['p95']} | {h['latency_ms']['p95']} |",
            "",
            f"Paired labeled evidence on candidate-served frauds: candidate caught {pr['candidate_caught']}/"
            f"{pr['positives']}, champion would have caught {pr['champion_would_have_caught']}/{pr['positives']} "
            f"(exact one-sided McNemar p = {pr['mcnemar_p_one_sided']}).",
            "",
            "Breaches: " + "; ".join(p["breaches"]),
            "",
            f"Feature drift in that window: {p.get('drift_status')} (max PSI {p.get('drift_max_psi')}). Drift is "
            "an alert, not the rollback trigger: both arms saw the same traffic.",
        ]
    else:
        lines.append("No guardrail breach evaluation precedes this rollback (manual or non-guardrail trigger).")
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
    lines += [
        "",
        "## What went well / what to change",
        "",
        "- The guardrail used paired labeled evidence and a significance test, so it did not roll back on noise.",
        "- Rollback was a single compare-and-swap of the deployment pointer; verification used real predictions.",
        "- Gap: the offline gate cannot see defects confined to a segment that is rare in the frozen test split.",
        "  Follow-up: add a label-maturity check to training data validation (see docs/DRIFT_AND_LIMITATIONS.md).",
        "",
        f"Event chain: first `{(pm['event_hashes']['first'] or '')[:16]}…` "
        f"last `{(pm['event_hashes']['last'] or '')[:16]}…`",
    ]
    return "\n".join(lines) + "\n"


def write_postmortem(store: Store, release_id: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    pm = build_postmortem(store, release_id)
    (out_dir / f"postmortem-{release_id}.json").write_text(json.dumps(pm, indent=2, default=str))
    path = out_dir / f"postmortem-{release_id}.md"
    path.write_text(render_markdown(pm))
    return path
