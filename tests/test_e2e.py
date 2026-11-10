"""End-to-end failure-to-rollback scenarios. Marked e2e (slower); run by `make test` and `make ci`."""

from __future__ import annotations

import json
import os

import pytest

from spectral_canary.chaos import run_chaos, scenario_names
from spectral_canary.drivers import InProcessDriver
from spectral_canary.fixtures import fixture_runtime
from spectral_canary.paths import state_paths
from spectral_canary.scenarios import release_story

pytestmark = pytest.mark.e2e


def _story_projection(home: str, run_dir: str, run_id: str) -> dict:
    """Run the full release story in-process and return its result plus a projection of the event log.

    Module-level so a spawned worker process can run it (the determinism test runs two stories side by side)."""
    from pathlib import Path

    rt = fixture_runtime(Path(run_dir), home=Path(home))
    try:
        res = release_story(InProcessDriver(rt), run_id, scale=1.0)
        out = []
        for e in rt.store.events(limit=100_000):
            p = e["payload"]
            if e["kind"] == "guardrail.evaluated":
                p = {
                    "verdict": p["verdict"],
                    "window": p["window_index"],
                    "labeled": p["labeled"]["paired"],
                    "flags": [p["arms"][a]["flagged"] for a in ("candidate", "champion")],
                }
            elif e["kind"] in ("model.registered",):
                p = {"version": p["version"]}
            elif "verified" in e["kind"] or e["kind"] in (
                "postmortem.written",
                "release.staged",
                "release.gate_evaluated",
                "release.shadow_evaluated",
            ):
                p = {}
            out.append((e["kind"], e["release_id"], json.dumps(p, sort_keys=True, default=str)))
        return {"result": res, "chain": rt.store.verify_event_chain(), "projection": out}
    finally:
        rt.close()


def test_release_story_passes_and_replays_deterministically(scw_home, tmp_path) -> None:
    """The release story (v2 promoted, v4 blocked, v3 rolled back under the shift) passes in-process, and the same
    seeds give the same decisions and evidence twice (wall-clock latency excluded). The two runs execute in parallel
    processes: same assertions as running them one after the other, half the wall time."""
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(2, mp_context=mp.get_context("spawn")) as pool:
        fa = pool.submit(_story_projection, str(scw_home), str(tmp_path / "r1"), "replay")
        fb = pool.submit(_story_projection, str(scw_home), str(tmp_path / "r2"), "replay")
        a, b = fa.result(), fb.result()
    res = a["result"]
    assert res["passed"], json.dumps({"expected": res["expected"], "actual": res["actual"]}, indent=1)
    assert a["chain"]["ok"] and b["chain"]["ok"]
    pm = (tmp_path / "r1" / "reports" / f"postmortem-{res['release_ids']['C']}.md").read_text()
    assert "guardrail_breach" in pm and "simulated" in pm.lower()
    assert a["projection"] == b["projection"]


@pytest.mark.skipif(os.environ.get("SCW_SKIP_HTTP_E2E") == "1", reason="explicitly skipped")
def test_demo_cli_rolls_back_over_real_http(scw_home, monkeypatch) -> None:
    """`scw demo` end to end: a real uvicorn gateway, real HTTP requests, the full story, then the CLI's own checks
    (event chain, registry re-verification, postmortem on disk) and the metrics it scraped from the live server."""
    from spectral_canary.cli import main

    monkeypatch.setenv("SCW_HOME", str(scw_home))
    assert main(["demo", "--port", str(_free_port())]) == 0
    runs = sorted(state_paths(scw_home).runs.glob("demo-*"))
    report = json.loads((runs[-1] / "report.json").read_text())
    assert report["passed"] and report["driver"] == "http"
    assert report["result"]["actual"]["post_rollback_versions"] == ["v2-hgb"]  # X-Model-Version after rollback
    assert "scw_rollbacks_total" in (runs[-1] / "metrics.prom").read_text()


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.parametrize("scenario", scenario_names())
def test_chaos_scenario_passes(scw_home, scenario) -> None:
    """One test per chaos scenario (the same code and pass criteria as `scw chaos-replay`), so pytest-xdist can
    spread the suite over the runner's cores."""
    report = run_chaos(state_paths(scw_home), only=[scenario])
    assert [s["scenario"] for s in report["scenarios"]] == [scenario]
    failed = [s for s in report["scenarios"] if not s["passed"]]
    assert not failed, json.dumps(failed, indent=1, default=str)


def test_chaos_rejects_unknown_scenario(scw_home) -> None:
    with pytest.raises(ValueError, match="unknown chaos scenario"):
        run_chaos(state_paths(scw_home), only=["schema_brake"])
