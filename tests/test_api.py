"""API contract: typed predict, headers, schema errors, auth/roles, metrics, events, registry verify."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from spectral_canary.api import create_app
from spectral_canary.data import load_split_csv
from spectral_canary.schema import decode_row


@pytest.fixture()
def client(rt):
    app = create_app(rt, start_background=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def tokens(rt) -> dict[str, str]:
    data = json.loads(rt.paths.operators.read_text())
    return {o["id"]: o["token"] for o in data["operators"]}


def _features(rt, i: int = 0) -> dict:
    X, _, _ = load_split_csv(rt.paths.data / "val.csv")
    return decode_row(X[i])


def test_health_and_ready(client) -> None:
    assert client.get("/healthz").json()["status"] == "alive"
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["champion"] == "v1-logreg"


def test_predict_contract(client, rt) -> None:
    r = client.post(
        "/v1/predict",
        json={
            "request_id": "api-1",
            "request_key": "card-1",
            "features": _features(rt),
            "context": {"simulated": True},
        },
        headers={"X-Trace-Id": "trace-abc"},
    )
    assert r.status_code == 200
    b = r.json()
    assert set(b) >= {
        "request_id",
        "model_version",
        "score",
        "decision",
        "threshold",
        "model_sha256",
        "latency_ms",
        "arm",
        "trace_id",
    }
    assert r.headers["X-Model-Version"] == b["model_version"] == "v1-logreg"
    assert r.headers["X-Trace-Id"] == "trace-abc" and b["simulated"] is True


def test_schema_error_is_422_and_counted(client) -> None:
    r = client.post("/v1/predict", json={"features": {"amount": "lots"}})
    assert r.status_code == 422 and r.json()["error"] == "schema_validation_failed"
    m = client.get("/metrics").text
    assert "scw_schema_errors_total" in m


def test_release_endpoints_require_auth_and_role(client, tokens) -> None:
    assert client.post("/v1/releases", json={"candidate_version": "v2-hgb"}).status_code == 401
    viewer = {"Authorization": f"Bearer {tokens['dashboard-viewer']}"}
    assert client.post("/v1/releases", json={"candidate_version": "v2-hgb"}, headers=viewer).status_code == 403
    op = {"Authorization": f"Bearer {tokens['oncall-operator']}"}
    r = client.post("/v1/releases", json={"candidate_version": "v2-hgb"}, headers=op)
    assert r.status_code == 200 and r.json()["state"] == "validated"
    rid = r.json()["release_id"]
    assert client.post(f"/v1/releases/{rid}/approve", headers=op).status_code in (403, 409)


def test_chaos_endpoints_disabled_by_default(client, tokens, monkeypatch) -> None:
    monkeypatch.delenv("SCW_ENABLE_CHAOS", raising=False)
    op = {"Authorization": f"Bearer {tokens['oncall-operator']}"}
    assert client.post("/v1/chaos/faults", json={"version": "v1-logreg"}, headers=op).status_code == 403


def test_metrics_have_bounded_labels(client, rt) -> None:
    for i in range(5):
        client.post("/v1/predict", json={"request_id": f"m-{i}", "features": _features(rt, i)})
    text = client.get("/metrics").text
    assert 'scw_requests_total{arm="champion",model_version="v1-logreg",status="ok"}' in text
    assert "scw_process_resident_memory_bytes" in text
    assert "request_id" not in text and "card-" not in text


def test_registry_verify_and_events(client) -> None:
    v = client.get("/v1/registry/verify").json()
    assert v["event_chain"]["ok"] and all(m["ok"] for m in v["models"])
    kinds = [e["kind"] for e in client.get("/v1/events").json()]
    assert "model.registered" in kinds and "deployment.bootstrapped" in kinds
    models = client.get("/v1/registry/models").json()
    assert {m["version"] for m in models} == {"v1-logreg", "v2-hgb", "v3-hgb-fresh", "v4-hgb-segfilter"}


def test_feedback_is_idempotent(client) -> None:
    assert client.post("/v1/feedback", json={"request_id": "x-1", "label": 1}).json()["accepted"] is True
    assert client.post("/v1/feedback", json={"request_id": "x-1", "label": 1}).json()["duplicate"] is True


def test_traffic_series_reflects_requests(client, rt) -> None:
    for i in range(30):
        client.post(
            "/v1/predict",
            json={
                "request_id": f"s-{i}",
                "features": _features(rt, i),
                "context": {"simulated": True, "scenario": "baseline"},
            },
        )
    s = client.get("/v1/traffic/series", params={"bucket": 10}).json()
    assert s["total"] >= 30
    assert s["series"][-1]["simulated_share"] == 1.0


def test_tampered_champion_fails_closed_without_crashing(scw_home, tmp_path) -> None:
    """Champion bytes change on disk -> gateway still starts, /readyz 503, predictions 503 (never a score)."""
    import os
    import shutil

    from spectral_canary.fixtures import fixture_runtime
    from spectral_canary.paths import state_paths

    home = tmp_path / "home"
    src = state_paths(scw_home)
    shutil.copytree(src.data, home / "data")
    shutil.copytree(src.artifacts, home / "artifacts")
    shutil.copytree(src.home / "secrets", home / "secrets")
    rt = fixture_runtime(tmp_path / "run", home=home)
    rt.close()
    f = home / "artifacts" / "v1-logreg" / "model.skops"
    os.chmod(f, 0o644)
    data = bytearray(f.read_bytes())
    data[len(data) // 2] ^= 0xFF
    f.write_bytes(bytes(data))
    rt2 = fixture_runtime(tmp_path / "run", home=home)  # "restart" against the same registry
    with TestClient(create_app(rt2, start_background=False)) as c:
        r = c.get("/readyz")
        assert r.status_code == 503 and "artifact_hash_mismatch" in " ".join(r.json()["problems"])
        p = c.post("/v1/predict", json={"features": _features(rt2)})
        assert p.status_code == 503 and p.json()["error"] == "champion_unavailable"
    rt2.close()


def test_project_contract_health_capabilities_runs(client, rt) -> None:
    """The read-only contract dashboards read: /health, /capabilities, /runs (from recorded run reports)."""
    import json as _json
    import time as _time

    run = rt.paths.runs / "chaos-20990101-000000"
    run.mkdir(parents=True, exist_ok=True)
    (run / "report.json").write_text(
        _json.dumps(
            {
                "run_id": run.name,
                "kind": "chaos",
                "started_at": _time.time(),
                "duration_s": 12.5,
                "passed": True,
                "scenarios": [{"scenario": "a", "passed": True}, {"scenario": "b", "passed": True}],
                "commit": "abc",
            }
        )
    )
    try:
        h = client.get("/health").json()
        assert h["ok"] is True and h["project_id"] == "spectral-canary" and h["mode"] == "simulated"
        assert h["last_run"] == {"run_id": run.name, "status": "completed"} and h["problems"] == []
        c = client.get("/capabilities").json()
        assert c["project_id"] == "spectral-canary" and c["dataset"]["synthetic"] is True
        assert c["implemented"] and c["not_implemented"] and c["disabled_by_design"]
        r = client.get("/runs", params={"limit": 5}).json()
        assert r["project_id"] == "spectral-canary" and r["total"] >= 1
        first = r["runs"][0]
        assert first["run_id"] == run.name and first["kind"] == "chaos" and first["mode"] == "simulated"
        assert first["headline"] == "2 of 2 failure scenarios held" and first["provenance"]["synthetic"] is True
        assert client.get(f"/runs/{run.name}").json()["status"] == "completed"
        assert client.get("/runs/nope").status_code == 404
    finally:
        (run / "report.json").unlink()
        run.rmdir()
