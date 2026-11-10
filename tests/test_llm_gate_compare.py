"""Relative promptfoo gate (scripts/llm_gate_compare.py) on the committed promptfoo outputs.

No Ollama here: the inputs are the recorded runs in docs/evidence/llm/promptfoo/ (champion triage-p2 and regressed
triage-p3 on Apple Metal, triage-p2 on a GitHub CPU runner). The live comparison is `make llm-gate-relative` and the
`llm-gate` CI job.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
EVIDENCE = ROOT / "docs/evidence/llm/promptfoo"
POLICY = ROOT / "configs/llm/release_policy.toml"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("llm_gate_compare", ROOT / "scripts/llm_gate_compare.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve the module by name
    spec.loader.exec_module(mod)
    return mod


def _load(gate: ModuleType, name: str) -> dict[str, Any]:
    data: dict[str, Any] = gate.load_results(EVIDENCE / f"{name}.results.json.gz")
    return data


def test_margin_is_the_policy_recall_drop_in_pass_rate_points(gate: ModuleType) -> None:
    recall = gate.read_recall_margin(POLICY)
    base = gate.summarize("base", _load(gate, "triage-p2"))
    assert recall == 0.05
    assert (base.cases, base.must_escalate) == (150, 66)
    assert gate.pass_rate_margin(recall, base) == pytest.approx(2.2)  # 100 * 0.05 * 66/150


def test_regex_fallback_reads_the_same_margin(gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tomllib", None)  # import fails as on Python < 3.11
    assert gate.read_recall_margin(POLICY) == 0.05


def test_same_prompt_passes(gate: ModuleType) -> None:
    run = gate.summarize("x", _load(gate, "triage-p2"))
    assert (run.passed, run.errors) == (139, 0)
    verdict = gate.compare(run, run, 0.05)
    assert verdict.ok and verdict.drop == 0


def test_regressed_prompt_fails(gate: ModuleType) -> None:
    base = gate.summarize("base", _load(gate, "triage-p2"))
    head = gate.summarize("head", _load(gate, "triage-p3"))
    assert head.passed == 111 and head.failed_by_metric == {"escalation_caught": 39}
    verdict = gate.compare(base, head, 0.05)
    assert not verdict.ok and verdict.drop == pytest.approx(18.67, abs=0.01)


def test_cross_backend_runs_are_not_comparable(gate: ModuleType) -> None:
    """Why base and head must share a runner: the same champion loses 8 points from Metal to CPU."""
    metal = gate.summarize("base", _load(gate, "triage-p2"))
    cpu = gate.summarize("head", _load(gate, "triage-p2.github-cpu"))
    assert cpu.passed == 127
    assert not gate.compare(metal, cpu, 0.05).ok
    assert gate.compare(cpu, cpu, 0.05).ok


def test_boundary_three_cases_pass_four_fail(gate: ModuleType) -> None:
    data = _load(gate, "triage-p2")
    base = gate.summarize("base", data)

    def lose(n: int) -> Any:
        d = copy.deepcopy(data)
        flipped = 0
        for r in d["results"]["results"]:
            if r["success"] and flipped < n:
                r["success"] = False
                flipped += 1
        return gate.summarize("head", d)

    assert gate.compare(base, lose(3), 0.05).ok  # 2.0 points <= 2.2
    assert not gate.compare(base, lose(4), 0.05).ok  # 2.67 points > 2.2


def test_errors_and_case_count_mismatch_fail_closed(gate: ModuleType) -> None:
    data = _load(gate, "triage-p2")
    base = gate.summarize("base", data)
    errored = copy.deepcopy(data)
    errored["results"]["results"][0].update(success=False, failureReason=2, error="connection refused")
    errored["results"]["stats"]["errors"] = 1
    verdict = gate.compare(base, gate.summarize("head", errored), 0.05)
    assert not verdict.ok and any("provider error" in r for r in verdict.reasons)

    short = copy.deepcopy(data)
    short["results"]["results"] = short["results"]["results"][:100]
    verdict = gate.compare(base, gate.summarize("head", short), 0.05)
    assert not verdict.ok and any("case count differs" in r for r in verdict.reasons)


def test_cli_exit_codes_and_report(gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p2, p3 = EVIDENCE / "triage-p2.results.json.gz", EVIDENCE / "triage-p3.results.json.gz"
    out = tmp_path / "verdict.json"
    assert gate.main(["--base", str(p2), "--head", str(p2), "--policy", str(POLICY), "--json-out", str(out)]) == 0
    assert '"ok": true' in out.read_text()
    assert gate.main(["--base", str(p2), "--head", str(p3), "--policy", str(POLICY)]) == 1
    text = capsys.readouterr().out
    assert "92.67%" in text and "74.00%" in text and "2.20 points" in text and "FAIL" in text
    assert gate.main(["--base", str(tmp_path / "missing.json"), "--head", str(p2), "--policy", str(POLICY)]) == 2
