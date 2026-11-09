"""End-to-end failure-to-rollback scenarios. Marked e2e (slower); run by `make test` and `make ci`."""

from __future__ import annotations

import json
import os

import pytest

from spectral_canary.chaos import run_chaos, scenario_names
from spectral_canary.drivers import HttpDriver, InProcessDriver
from spectral_canary.fixtures import fixture_runtime
from spectral_canary.paths import state_paths
from spectral_canary.scenarios import release_story

pytestmark = pytest.mark.e2e


def test_release_story_in_process(scw_home, tmp_path) -> None:
    rt = fixture_runtime(tmp_path / "story", home=scw_home)
    try:
        res = release_story(InProcessDriver(rt), "pytest-story")
        assert res["passed"], json.dumps({"expected": res["expected"], "actual": res["actual"]}, indent=1)
        assert rt.store.verify_event_chain()["ok"]
        pm = (tmp_path / "story" / "reports" / f"postmortem-{res['release_ids']['C']}.md").read_text()
        assert "guardrail_breach" in pm and "simulated" in pm.lower()
    finally:
        rt.close()


def test_replay_is_deterministic(scw_home, tmp_path) -> None:
    """Same seeds -> same decisions and same evidence (wall-clock latency excluded)."""

    def projection(run: str) -> list:
        rt = fixture_runtime(tmp_path / run, home=scw_home)
        try:
            release_story(InProcessDriver(rt), "replay", scale=1.0)
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
            return out
        finally:
            rt.close()

    a, b = projection("r1"), projection("r2")
    assert a == b


@pytest.mark.skipif(os.environ.get("SCW_SKIP_HTTP_E2E") == "1", reason="explicitly skipped")
def test_rollback_over_real_http(scw_home, tmp_path, monkeypatch) -> None:
    """Same story through a real uvicorn process and real HTTP requests (reduced traffic volume)."""
    from spectral_canary.auth import ensure_operators_file
    from spectral_canary.cli import start_server, stop_server

    run = tmp_path / "http"
    rt = fixture_runtime(run, home=scw_home)
    rt.close()
    monkeypatch.setenv("SCW_HOME", str(scw_home))
    tokens = ensure_operators_file(state_paths(scw_home).operators)
    port = _free_port()  # a fixed port made the test fail whenever an orphaned server still held it
    proc = start_server(run / "registry.sqlite", port, {"SCW_HOME": str(scw_home)}, log_path=run / "server.log")
    try:
        d = HttpDriver(f"http://127.0.0.1:{port}", tokens)
        res = release_story(d, "pytest-http")
        assert res["passed"], json.dumps({"expected": res["expected"], "actual": res["actual"]}, indent=1)
        r = d.http.post("/v1/predict", json={"request_key": "card-1", "features": _row(scw_home)})
        assert r.headers["X-Model-Version"] == "v2-hgb"
        assert "scw_rollbacks_total" in d.http.get("/metrics").text
    finally:
        stop_server(proc)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _row(home):
    from spectral_canary.data import load_split_csv
    from spectral_canary.schema import decode_row

    X, _, _ = load_split_csv(state_paths(home).data / "val.csv")
    return decode_row(X[0])


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
