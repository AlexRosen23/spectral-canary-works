"""Copy the recorded LLM-lane run into docs/evidence/llm/ (small, reviewable files) and render its results tables.

Usage: uv run python scripts/build_llm_evidence.py [RUN_DIR]   (default: var/runs/llm-latest)

Every number in the generated RESULTS.md comes from the run's report.json / gate reports / event log; nothing is
typed by hand. Raw per-try eval JSONL files are copied too (they are the source of every offline number).
"""

from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def pct(x: Any) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def main() -> int:
    run = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "var" / "runs" / "llm-latest"
    run = run.resolve()
    rep = json.loads((run / "report.json").read_text())
    out = ROOT / "docs" / "evidence" / "llm" / rep["run_id"]
    out.mkdir(parents=True, exist_ok=True)
    slim = {k: v for k, v in rep.items() if k not in ("gate_reports",)}
    (out / "report.json").write_text(json.dumps(slim, indent=2, default=str) + "\n")
    (out / "gate_reports.json").write_text(json.dumps(rep.get("gate_reports"), indent=2, default=str) + "\n")
    for p in sorted((run / "reports").glob("postmortem-llm-*")):
        shutil.copy2(p, out / p.name)
    ev = out / "evals"
    ev.mkdir(exist_ok=True)
    for p in sorted((run / "evals").glob("*.jsonl")):  # raw per-try results, gzipped (deterministic mtime)
        with gzip.GzipFile(ev / (p.name + ".gz"), "wb", mtime=0) as gz:
            gz.write(p.read_bytes())
    prom = (run / "metrics.prom").read_text() if (run / "metrics.prom").exists() else ""
    (out / "metrics-genai.prom").write_text(
        "".join(line + "\n" for line in prom.splitlines() if "gen_ai" in line or line.startswith("# HELP gen_ai"))
    )
    # events: the full hash-chained log of the LLM lane, as JSON lines (kind, actor, release, payload, hash)
    con = sqlite3.connect(run / "llm-registry.sqlite")
    con.row_factory = sqlite3.Row
    with gzip.open(out / "events.jsonl.gz", "wt") as fh:
        for r in con.execute(
            "SELECT seq, ts, kind, actor, simulated, release_id, payload_json, hash, prev_hash FROM events"
        ):
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json"))
            fh.write(json.dumps(d, sort_keys=True, default=str) + "\n")
    con.close()

    # -- tables ------------------------------------------------------------------------------------------------
    rows = []
    boot = rep.get("bootstrap_gate") or {}
    summaries: dict[str, Any] = {}
    for _rid, g in (rep.get("gate_reports") or {}).items():
        if not g:
            continue
        if g.get("candidate_summary"):
            summaries[g["candidate"]] = g["candidate_summary"]
        if g.get("champion_summary") and g.get("champion"):
            summaries.setdefault(g["champion"], g["champion_summary"])
    hdr = (
        "| bundle | model | schema valid | reason acc (pass@1) | fully correct pass@1 | pass^3 | escalation recall "
        "| takeover segment recall | high-value CNP recall | false escalation | escalate acc (all cases) | p95 ms "
        "| out tokens | $ / 1k calls |"
    )
    rows.append(hdr)
    rows.append("|" + "---|" * 14)
    for v, s in summaries.items():
        model = rep["models"].get(v, {}).get("model", "?")
        seg = s["segments"]
        rows.append(
            f"| `{v}` | {model} | {pct(s['schema_valid']['rate'])} | {pct(s['reason_accuracy']['rate'])} | "
            f"{pct(s['correct_pass1']['rate'])} | {pct(s['correct_pass_k']['rate'])} | "
            f"{pct(s['escalation_recall']['rate'])} [{pct(s['escalation_recall']['ci95'][0])}–"
            f"{pct(s['escalation_recall']['ci95'][1])}] | {pct(seg['account_takeover']['rate'])} | "
            f"{pct(seg['high_value_cnp']['rate'])} | {pct(s['false_escalation_rate']['rate'])} | "
            f"{pct(s['escalate_accuracy']['rate'])} | "
            f"{s['latency_ms']['p95']:.0f} | {s['tokens']['output_mean']} | {s['cost_usd_per_1k_calls']} |"
        )
    res = rep["result"]
    story = ["| step | expected | actual |", "|---|---|---|"]
    for k, v in res["expected"].items():
        story.append(f"| {k} | {v} | {res['actual'].get(k)} |")
    for k, v in (res.get("observations") or {}).items():
        story.append(f"| {k} (observation, not an expectation) | — | {v} |")
    for k in ("C_reason", "D_reason"):
        if res["actual"].get(k):
            story.append(f"| {k} | (recorded) | {res['actual'][k]} |")
    gate_lines = []
    for rid, g in (rep.get("gate_reports") or {}).items():
        if not g:
            continue
        gate_lines.append(
            f"**{rid}** `{g['candidate']}` vs `{g['champion']}`: {'PASSED' if g['passed'] else 'BLOCKED'}"
        )
        gate_lines.append("")
        gate_lines.append("| check | result | observed | limit | blocking |")
        gate_lines.append("|---|---|---|---|---|")
        for c in g["checks"]:
            gate_lines.append(
                f"| {c['name']} | {'pass' if c['passed'] else 'FAIL'} | {c['observed']} | {c['limit']} | "
                f"{'yes' if c.get('blocking', True) else 'no (advisory)'} |"
            )
        gate_lines.append("")
    online = [
        "| release | candidate | shadow (requests, candidate failures, escalate agreement) | canary windows "
        "(healthy) | last window | candidate p95 / champion-arm p95 ms | $ per 1k calls cand / champ | "
        "paired must-escalate: cand / champ of n | final state |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    con = sqlite3.connect(run / "llm-registry.sqlite")
    con.row_factory = sqlite3.Row
    for r in con.execute("SELECT * FROM releases ORDER BY created_at"):
        sh = json.loads(r["shadow_summary_json"] or "null")
        ev = json.loads(r["last_evaluation_json"] or "null")
        if not sh and not ev:
            continue
        shs = f"{sh['requests']}, {sh['candidate_failures']}, {pct(sh.get('escalate_agreement'))}" if sh else "—"
        if ev:
            c, h = ev["arms"]["candidate"], ev["arms"]["champion"]
            pr = ev["labeled_cumulative"]["paired"]
            last = f"#{ev['window_index']} {ev['verdict']}" + (
                ": " + "; ".join(ev["breaches"]) if ev["breaches"] else ""
            )
            lat = f"{c['latency_ms']['p95']} / {h['latency_ms']['p95']}"
            cost = f"{c['cost_usd_per_1k_calls']} / {h['cost_usd_per_1k_calls']}"
            paired = f"{pr['candidate_escalated']} / {pr['champion_would_have_escalated']} of {pr['escalations']}"
        else:
            last = lat = cost = paired = "—"
        online.append(
            f"| {r['release_id']} | `{r['candidate_version']}` | {shs} | {r['windows_evaluated']} "
            f"({r['healthy_windows']}) | {last} | {lat} | {cost} | {paired} | {r['state']} |"
        )
    con.close()
    m = rep["machine"]
    md = [
        f"# LLM lane recorded run `{rep['run_id']}`",
        "",
        "_Generated by `scripts/build_llm_evidence.py` from "
        f"`var/runs/{rep['run_id']}/report.json`. Do not edit by hand._",
        "",
        f"- Command: `{rep.get('command')}`; commit `{rep.get('commit')}`; duration {rep['duration_s']} s; "
        f"passed={rep['passed']}",
        f"- Machine: {m.get('cpu')}, {m.get('ram_gb')} GB RAM, {m.get('platform')}; load average at the end "
        f"{m.get('load_avg_1m')} (shared laptop; CPU/GPU and the Ollama server were shared with other workloads)",
        f"- Ollama {rep.get('ollama_url')}; gold set sha256 `{rep['gold_sha256'][:16]}`; LLM policy sha256 "
        f"`{rep['policy_sha256'][:16]}`; event chain ok={rep['event_chain']['ok']} ({rep['event_chain']['checked']} "
        "events); registry verified=" + str(all(r["ok"] for r in rep["registry_verify"])),
        f"- Bootstrap gate for the first champion: passed={boot.get('gate_passed')}",
        f"- Commit at start: `{rep.get('commit_at_start', rep.get('commit'))}`; offline suite results reused from an "
        "earlier run (same immutable bundle + gold set + k): "
        + (", ".join(rep.get("reused_offline_results") or []) or "none"),
        "",
        "## Release story: expected vs actual",
        "",
        *story,
        "",
        "## Online evidence (shadow and 10% canary, real HTTP, simulated alerts)",
        "",
        *online,
        "",
        "Canary windows are 200 routed requests; latency is model-call p95 within the window; cost is notional.",
        "",
        "## Offline regression suite (gold test split: 150 cases x k=3 seeded tries, code graders)",
        "",
        *rows,
        "",
        "Rates are per try (450 tries) except pass^3 (150 cases). Recall brackets are 95% Wilson intervals over tries,",
        "which overstate precision because the 3 tries of a case are correlated. Latency is a sequential offline",
        "measurement on a shared, loaded laptop. Cost is notional (USD 0.10 / 0.40 per million input / output tokens).",
        "",
        "## Gate reports",
        "",
        *gate_lines,
    ]
    (out / "RESULTS.md").write_text("\n".join(md) + "\n")
    print(f"evidence written to {out.relative_to(ROOT)}")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
