"""Offline regression suite: every gold case x k tries through the real backend, graded by code.

pass@1 here = the mean per-try success rate; pass^k = the share of cases that succeed on *all* k tries
(the consistency measure from Anthropic's "Demystifying evals for AI agents", 2026-01-09). Each try uses its
own deterministic seed (``call_seed(case_id, try)``), so a re-run of an immutable bundle samples identically.

Raw per-try results are written as JSONL next to the summary, so every number can be recomputed.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from ..metrics import percentiles, wilson_interval
from .alerts import SEGMENTS
from .bundles import LoadedBundle
from .client import Backend, call_seed
from .graders import grade


def cost_units(input_tokens: float, output_tokens: float, price: dict[str, float]) -> float:
    """Notional cost in US dollars per million calls at the policy's reference token prices (an assumption:
    local Ollama inference has no per-token bill; the prices make cost comparable across bundles)."""
    return input_tokens * price["input_usd_per_mtok"] + output_tokens * price["output_usd_per_mtok"]


def run_suite(
    lb: LoadedBundle,
    cases: list[dict[str, Any]],
    backend: Backend,
    k: int,
    out_dir: Path,
    gold_sha: str,
    concurrency: int = 1,
    timeout_s: float = 60.0,
    log: Any = None,
) -> tuple[list[dict[str, Any]], Path, bool]:
    """Returns (rows, jsonl path, reused). Reuses a finished run of the same immutable bundle + gold set + k."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{lb.version}-{lb.sha256[:12]}-gold{gold_sha[:12]}-k{k}.jsonl"
    if path.exists():
        prev = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        if len(prev) == len(cases) * k:
            return prev, path, True

    def one(job: tuple[dict[str, Any], int]) -> dict[str, Any]:
        c, t = job
        r = backend.chat(
            lb.bundle, {"summary": c["summary"], "fields": c["fields"]}, call_seed(c["case_id"], t), timeout_s
        )
        g = grade(r.text, c["expected"])
        return {
            "case_id": c["case_id"],
            "try": t,
            "bundle": lb.version,
            "bundle_sha256": lb.sha256,
            "expected": c["expected"],
            "segments": c["segments"],
            "smoke": c.get("smoke", False),
            "ok": r.ok,
            "error": r.error,
            "error_type": r.error_type,
            "latency_ms": round(r.latency_ms, 2),
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "response_model": r.model,
            "text": r.text,
            "grade": g,
        }

    t0 = time.time()
    jobs = [(c, t) for c in cases for t in range(k)]
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max(1, concurrency)) as ex:
        for i, row in enumerate(ex.map(one, jobs), 1):
            rows.append(row)
            if log and i % 50 == 0:
                log(f"  {lb.version}: {i}/{len(jobs)} calls ({time.time() - t0:.0f}s)")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    tmp.replace(path)
    return rows, path, False


def _rate(xs: list[bool]) -> dict[str, Any]:
    n = len(xs)
    s = int(sum(xs))
    lo, hi = wilson_interval(s, n)
    return {"rate": round(s / n, 4) if n else None, "n": n, "ci95": [round(lo, 4), round(hi, 4)]}


def summarize(rows: list[dict[str, Any]], k: int, price: dict[str, float]) -> dict[str, Any]:
    by_case: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_case.setdefault(r["case_id"], []).append(r)
    g = [r["grade"] for r in rows]
    pos = [r for r in rows if r["expected"]["escalate"]]
    neg = [r for r in rows if not r["expected"]["escalate"]]
    seg = {}
    for s in SEGMENTS:
        sr = [r for r in pos if s in r["segments"]]
        seg[s] = {**_rate([r["grade"]["escalate"] is True for r in sr]), "cases": len({r["case_id"] for r in sr})}
    smoke_cases = {cid: rs for cid, rs in by_case.items() if rs[0]["smoke"]}
    smoke_first = [min(rs, key=lambda r: r["try"]) for rs in smoke_cases.values()]
    ok_rows = [r for r in rows if r["ok"]]
    in_tok = float(np.mean([r["input_tokens"] for r in ok_rows])) if ok_rows else 0.0
    out_tok = float(np.mean([r["output_tokens"] for r in ok_rows])) if ok_rows else 0.0
    return {
        "cases": len(by_case),
        "k": k,
        "calls": len(rows),
        "call_errors": sum(1 for r in rows if not r["ok"]),
        "schema_valid": _rate([x["valid"] for x in g]),
        "reason_accuracy": _rate([bool(x.get("reason_ok")) for x in g]),
        "escalate_accuracy": _rate([bool(x.get("escalate_ok")) for x in g]),
        "correct_pass1": _rate([bool(x.get("correct")) for x in g]),
        "correct_pass_k": _rate([all(r["grade"].get("correct") for r in rs) for rs in by_case.values()]),
        "escalate_pass_k": _rate([all(r["grade"].get("escalate_ok") for r in rs) for rs in by_case.values()]),
        "escalation_recall": _rate([r["grade"]["escalate"] is True for r in pos]),
        "false_escalation_rate": _rate([r["grade"]["escalate"] is True for r in neg]),
        "segments": seg,
        "one_sentence_rationale": _rate([x["one_sentence"] for x in g]),
        "smoke": {
            "cases": len(smoke_first),
            "rule": "try 0 of each smoke case: valid JSON and the escalate decision correct",
            "passed_cases": sum(1 for r in smoke_first if r["grade"]["valid"] and r["grade"].get("escalate_ok")),
            "reason_correct_cases": sum(1 for r in smoke_first if r["grade"].get("reason_ok")),
        },
        "latency_ms": percentiles([r["latency_ms"] for r in ok_rows]),
        "tokens": {"input_mean": round(in_tok, 1), "output_mean": round(out_tok, 1)},
        "cost_usd_per_1k_calls": round(cost_units(in_tok, out_tok, price) / 1000, 5),
    }
