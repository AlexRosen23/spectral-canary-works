#!/usr/bin/env python3
"""Relative promptfoo gate for the LLM triage lane: compare a PR head run with its base run.

Both runs must come from the same machine and Ollama server (CI runs them back to back on one runner). An absolute
pass-rate threshold does not transfer across compute backends: the accepted champion scores 92.7% on Apple Metal and
84.7% on a GitHub CPU runner with identical weights, Ollama, prompt, params and seed. A same-runner difference does.

The margin is the release policy's non-inferiority bound, converted to pass-rate points the same way the Makefile
derives LLM_GATE_THRESHOLD: a case fails on an invalid reply or a missed must-escalate alert, so losing
`max_escalation_recall_drop` of the must-escalate cases costs

    margin = 100 * max_escalation_recall_drop * must_escalate_cases / cases     (0.05 * 66 / 150 -> 2.2 points)

The gate fails when head_pass_rate < base_pass_rate - margin, and also (fail closed) when either run has provider
errors, is empty, or the two runs cover a different number of cases. The policy is read from the base checkout, so a
PR cannot loosen its own gate by editing configs/llm/release_policy.toml.

Standard library only (Python >= 3.9): the CI job has no uv environment.

    python3 scripts/llm_gate_compare.py --base base.json --head head.json \
        --policy <base checkout>/configs/llm/release_policy.toml
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLICY_SECTION = "offline_gate"
POLICY_KEY = "max_escalation_recall_drop"


@dataclass(frozen=True)
class SuiteRun:
    label: str
    provider: str
    cases: int
    passed: int
    errors: int
    must_escalate: int
    failed_by_metric: dict[str, int]

    @property
    def pass_rate(self) -> float:
        return 100.0 * self.passed / self.cases if self.cases else 0.0


@dataclass(frozen=True)
class Verdict:
    ok: bool
    margin: float
    drop: float
    reasons: list[str]


def load_results(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def summarize(label: str, data: dict[str, Any]) -> SuiteRun:
    """Pass/fail counts from one promptfoo JSON output (`-o x.json`)."""
    results = data["results"]["results"]
    failed_by_metric: dict[str, int] = {}
    providers: set[str] = set()
    errors = 0
    passed = 0
    must_escalate = 0
    for r in results:
        providers.add(str((r.get("provider") or {}).get("label") or (r.get("provider") or {}).get("id", "?")))
        if r.get("failureReason") == 2 or (r.get("error") and not r.get("gradingResult")):
            errors += 1  # promptfoo ResultFailureReason.ERROR: provider/transport error, no reply was graded
        if r.get("success"):
            passed += 1
        else:
            for comp in (r.get("gradingResult") or {}).get("componentResults") or []:
                if not comp.get("pass"):
                    metric = (comp.get("assertion") or {}).get("metric") or "unnamed"
                    failed_by_metric[metric] = failed_by_metric.get(metric, 0) + 1
        expected = (r.get("vars") or (r.get("testCase") or {}).get("vars") or {}).get("expected_escalate")
        if expected is True or str(expected).lower() == "true":
            must_escalate += 1
    stats_errors = int(data["results"].get("stats", {}).get("errors", 0))
    return SuiteRun(
        label=label,
        provider=",".join(sorted(providers)),
        cases=len(results),
        passed=passed,
        errors=max(errors, stats_errors),
        must_escalate=must_escalate,
        failed_by_metric=dict(sorted(failed_by_metric.items())),
    )


def read_recall_margin(policy: Path) -> float:
    """`[offline_gate] max_escalation_recall_drop` from the release policy (tomllib when available)."""
    text = policy.read_text(encoding="utf-8")
    try:
        import tomllib  # Python >= 3.11

        return float(tomllib.loads(text)[POLICY_SECTION][POLICY_KEY])
    except ImportError:
        section = re.search(rf"^\[{POLICY_SECTION}\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
        value = section and re.search(rf"^{POLICY_KEY}\s*=\s*([0-9.]+)", section.group(1), re.M)
        if not value:
            raise SystemExit(f"{policy}: [{POLICY_SECTION}] {POLICY_KEY} not found") from None
        return float(value.group(1))


def pass_rate_margin(recall_margin: float, base: SuiteRun) -> float:
    return 100.0 * recall_margin * base.must_escalate / base.cases if base.cases else 0.0


def compare(base: SuiteRun, head: SuiteRun, recall_margin: float) -> Verdict:
    margin = pass_rate_margin(recall_margin, base)
    drop = base.pass_rate - head.pass_rate
    reasons: list[str] = []
    for run in (base, head):
        if run.cases == 0:
            reasons.append(f"{run.label}: no test results")
        if run.errors:
            reasons.append(f"{run.label}: {run.errors} provider error(s); an errored run is not evidence")
    if base.cases != head.cases:
        reasons.append(f"case count differs: base {base.cases}, head {head.cases}")
    # round to avoid float noise deciding a tie at exactly the margin
    if round(drop, 9) > round(margin, 9):
        reasons.append(f"head pass rate dropped {drop:.2f} points, more than the {margin:.2f}-point margin")
    return Verdict(ok=not reasons, margin=margin, drop=drop, reasons=reasons)


def report(base: SuiteRun, head: SuiteRun, recall_margin: float, verdict: Verdict) -> str:
    def row(r: SuiteRun) -> str:
        failed = ", ".join(f"{k} {v}" for k, v in r.failed_by_metric.items()) or "none"
        return (
            f"| {r.label} | {r.provider} | {r.passed}/{r.cases} | {r.pass_rate:.2f}% | {r.errors} | "
            f"{r.must_escalate} | {failed} |"
        )

    lines = [
        "### LLM triage gate: base vs head (same runner, same Ollama)",
        "",
        "| run | provider | passed | pass rate | errors | must-escalate | failed by metric |",
        "|---|---|---|---|---|---|---|",
        row(base),
        row(head),
        "",
        f"- margin: 100 x {recall_margin} (max_escalation_recall_drop) x {base.must_escalate}/{base.cases} "
        f"(must-escalate/cases in base) = **{verdict.margin:.2f} points**",
        f"- change: head - base = **{head.pass_rate - base.pass_rate:+.2f} points** "
        f"(head fails below {base.pass_rate - verdict.margin:.2f}%)",
        f"- verdict: **{'PASS' if verdict.ok else 'FAIL'}**",
    ]
    lines += [f"  - {reason}" for reason in verdict.reasons]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path, required=True, help="promptfoo JSON of the base run (.json/.json.gz)")
    ap.add_argument("--head", type=Path, required=True, help="promptfoo JSON output of the head run")
    ap.add_argument("--policy", type=Path, required=True, help="release_policy.toml to read the margin from (base's)")
    ap.add_argument("--base-name", default="base", help="label for the base run in the report (e.g. commit, prompt)")
    ap.add_argument("--head-name", default="head", help="label for the head run in the report")
    ap.add_argument("--json-out", type=Path, help="also write the verdict as JSON")
    args = ap.parse_args(argv)

    for p in (args.base, args.head, args.policy):
        if not p.is_file():
            print(f"llm-gate: missing {p}", file=sys.stderr)
            return 2
    recall_margin = read_recall_margin(args.policy)
    base = summarize(args.base_name, load_results(args.base))
    head = summarize(args.head_name, load_results(args.head))
    verdict = compare(base, head, recall_margin)
    text = report(base, head, recall_margin, verdict)
    print(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(text)
    if args.json_out:
        args.json_out.write_text(
            json.dumps(
                {
                    "ok": verdict.ok,
                    "margin_points": round(verdict.margin, 4),
                    "drop_points": round(verdict.drop, 4),
                    "recall_margin": recall_margin,
                    "reasons": verdict.reasons,
                    "base": {**base.__dict__, "pass_rate": round(base.pass_rate, 4)},
                    "head": {**head.__dict__, "pass_rate": round(head.pass_rate, 4)},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
