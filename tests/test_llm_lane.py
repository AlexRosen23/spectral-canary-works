"""LLM triage lane: gold set, graders, bundle registry, offline gate, service, controller story, API, OTel metrics.

These tests use ``ScriptedBackend``, a deterministic TEST DOUBLE that stands in for the models (no Ollama, no
network). The recorded run with real model calls is ``make llm-demo`` (see docs/LLM_RELEASE_LANE.md).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spectral_canary.artifacts import ArtifactError
from spectral_canary.llm.alerts import build_gold, gold_sha256, iter_traffic, label_alert, load_gold, sample_alert
from spectral_canary.llm.bundles import BUNDLE_DIR, compile_bundle, register_all_bundles
from spectral_canary.llm.client import ScriptedBackend
from spectral_canary.llm.graders import grade, one_sentence
from spectral_canary.llm.monitor import input_drift
from spectral_canary.llm.runtime import GOLD_PATH, build_llm_runtime
from spectral_canary.llm.scenarios import LLMInProcessDriver, drive, llm_release_story
from spectral_canary.paths import state_paths


# -- scripted "models" (test doubles) -----------------------------------------------------------------------
def _alert_from(fields: dict[str, Any]):
    from spectral_canary.llm.alerts import Alert

    return Alert(**fields)


def follows_policy(bundle: dict[str, Any], fields: dict[str, Any], seed: int) -> dict[str, Any]:
    lab = label_alert(_alert_from(fields))
    return {"reason_code": lab["reason_code"], "escalate": lab["escalate"], "rationale": "Policy applied."}


def forgets_ato_rule(bundle: dict[str, Any], fields: dict[str, Any], seed: int) -> dict[str, Any]:
    lab = label_alert(_alert_from(fields))
    esc = "high_value_cnp" in lab["segments"] or "card_testing_burst" in lab["segments"]
    return {"reason_code": lab["reason_code"], "escalate": esc, "rationale": "Policy applied."}


def under_escalates(bundle: dict[str, Any], fields: dict[str, Any], seed: int) -> dict[str, Any]:
    lab = label_alert(_alert_from(fields))
    return {"reason_code": lab["reason_code"], "escalate": lab["escalate"] and seed % 2 == 0, "rationale": "Hm."}


def scripted() -> ScriptedBackend:
    return ScriptedBackend(
        {
            "triage-p1": follows_policy,
            "triage-p2": follows_policy,
            "triage-p3": forgets_ato_rule,
            "triage-p2-qwen": under_escalates,
            "triage-p2-ctx4k": follows_policy,
        },
        latency_ms={"triage-p1": 5, "triage-p2": 5, "triage-p3": 5, "triage-p2-qwen": 4, "triage-p2-ctx4k": 5},
        tokens={},
    )


@pytest.fixture()
def llm_rt(tmp_path: Path):
    home = tmp_path / "home"
    state_paths(home).ensure()
    rt = build_llm_runtime(home=home, db_path=tmp_path / "run" / "llm-registry.sqlite", backend=scripted())
    register_all_bundles(rt.store, rt.paths, rt.backend)
    rt.controller.bootstrap_champion("triage-p1", actor="fixture-bootstrap")
    yield rt
    rt.close()


# -- gold set ------------------------------------------------------------------------------------------------
def test_committed_gold_set_matches_generator() -> None:
    assert gold_sha256(load_gold(GOLD_PATH)) == gold_sha256(build_gold())


def test_labels_are_rule_derived_and_smoke_set_is_textbook() -> None:
    cases = build_gold()
    for c in cases:
        assert label_alert(_alert_from(c["fields"]))["reason_code"] == c["expected"]["reason_code"]
    test = [c for c in cases if c["split"] == "test"]
    low_value_ato = [c for c in test if c["segments"] == ["account_takeover"]]
    assert len(low_value_ato) >= 15, "the p3 regression needs segment support in the test split"
    smoke = [c for c in test if c["smoke"]]
    assert len(smoke) == 12
    # every smoke account takeover is also a high-value online purchase (escalates under the dollar rule too)
    assert all("high_value_cnp" in c["segments"] for c in smoke if "account_takeover" in c["segments"])


def test_decision_table_edges() -> None:
    import random

    a = sample_alert(random.Random(1), "benign_anomaly", "x")
    from dataclasses import replace

    base = replace(
        a,
        channel="card_not_present",
        amount=999.99,
        device_age_hours=500.0,
        password_reset_hours_ago=None,
        ship_to_added_hours_ago=None,
        small_auths_1h=0,
        declines_1h=0,
        prior_chargebacks=0,
    )
    assert label_alert(base) == {"reason_code": "BENIGN_ANOMALY", "escalate": False, "segments": []}
    assert label_alert(replace(base, amount=1000.0))["escalate"] is True
    ato = replace(base, password_reset_hours_ago=23.9, device_age_hours=2.0)
    assert label_alert(ato)["reason_code"] == "ACCOUNT_TAKEOVER" and label_alert(ato)["escalate"]
    assert label_alert(replace(ato, password_reset_hours_ago=24.0))["reason_code"] == "STOLEN_CARD_CNP"


# -- graders -------------------------------------------------------------------------------------------------
def test_code_graders() -> None:
    exp = {"reason_code": "CARD_TESTING", "escalate": True}
    good = json.dumps(
        {"reason_code": "CARD_TESTING", "escalate": True, "rationale": "Four micro-auths and 3 declines."}
    )
    assert grade(good, exp)["correct"]
    assert not grade("not json", exp)["valid"]
    assert grade(json.dumps({"reason_code": "FRAUD", "escalate": True, "rationale": "x"}), exp)["problems"] == [
        "reason_code_not_in_enum"
    ]
    assert (
        "escalate_not_boolean"
        in grade(json.dumps({"reason_code": "CARD_TESTING", "escalate": "yes", "rationale": "x"}), exp)["problems"]
    )
    assert (
        "rationale_too_long"
        in grade(json.dumps({"reason_code": "CARD_TESTING", "escalate": True, "rationale": "x" * 301}), exp)["problems"]
    )
    assert (
        "extra_keys:['score']"
        in grade(json.dumps({"reason_code": "CARD_TESTING", "escalate": True, "rationale": "x", "score": 1}), exp)[
            "problems"
        ]
    )
    assert one_sentence("Known device; $1,200 online, so escalate.")
    assert not one_sentence("Known device. Amount $1,200 online, so escalate.")


# -- bundles -------------------------------------------------------------------------------------------------
def test_bundle_hash_covers_prompt_model_and_params(tmp_path: Path) -> None:
    b = compile_bundle(BUNDLE_DIR / "triage-p2.toml", "sha256:abc")
    q = compile_bundle(BUNDLE_DIR / "triage-p2-qwen.toml", "sha256:def")
    p3 = compile_bundle(BUNDLE_DIR / "triage-p3.toml", "sha256:abc")
    assert b["system_prompt_sha256"] == q["system_prompt_sha256"] and b["model"] != q["model"]
    assert b["system_prompt_sha256"] != p3["system_prompt_sha256"]
    assert b["params"]["temperature"] == 0.0


def test_registered_bundles_are_immutable_and_tamper_evident(llm_rt) -> None:
    import sqlite3

    rec = llm_rt.store.get_model("triage-p2")
    with pytest.raises(sqlite3.DatabaseError), llm_rt.store.transaction() as c:
        c.execute("UPDATE model_versions SET artifact_sha256 = 'x' WHERE version = 'triage-p2'")
    f = Path(rec["artifact_path"])
    os.chmod(f, 0o644)
    f.write_text(f.read_text().replace("25 words", "250 words"))
    llm_rt.service.pool.evict("triage-p2")
    with pytest.raises(ArtifactError):
        llm_rt.service.pool.get("triage-p2")
    r = llm_rt.controller.propose("triage-p2", "oncall-operator")
    assert r["state"] == "blocked" and "bundle_integrity" in r["reason"]


# -- offline gate --------------------------------------------------------------------------------------------
def test_offline_gate_blocks_segment_regression_that_smoke_misses(llm_rt) -> None:
    ok = llm_rt.controller.propose("triage-p2", "oncall-operator")
    assert ok["state"] == "validated", ok["reason"]
    bad = llm_rt.controller.propose("triage-p3", "oncall-operator")
    assert bad["state"] == "blocked"
    checks = {c["name"]: c for c in bad["gate_report"]["checks"]}
    assert not checks["segment_recall:account_takeover"]["passed"]
    assert checks["segment_recall:high_value_cnp"]["passed"]
    assert checks["smoke_subset"]["passed"] and checks["smoke_subset"]["blocking"] is False
    assert bad["gate_report"]["candidate_summary"]["k"] == 3


def test_model_weights_change_blocks_release(llm_rt) -> None:
    llm_rt.backend.model_digest = lambda m: "sha256:different"  # type: ignore[method-assign]
    r = llm_rt.controller.propose("triage-p2", "oncall-operator")
    assert r["state"] == "blocked" and "model_weights_pinned" in r["reason"]


# -- service -------------------------------------------------------------------------------------------------
def _payload(i: int = 0) -> dict[str, Any]:
    t = next(iter_traffic("t", 1, 7, start_index=i))
    return t.payload


def test_invalid_candidate_output_falls_back_and_champion_failure_fails_closed(llm_rt) -> None:
    rel = llm_rt.controller.propose("triage-p2", "oncall-operator")
    llm_rt.controller.to_staging(rel["release_id"], "oncall-operator")
    llm_rt.controller.to_shadow(rel["release_id"], "oncall-operator")
    drive(LLMInProcessDriver(llm_rt), "shadow", 70, 3)
    llm_rt.controller.to_canary(rel["release_id"], "oncall-operator")
    llm_rt.backend.responders["triage-p2"] = lambda b, f, s: "I think this is fraud"
    outs = [llm_rt.service.predict({**_payload(i), "request_key": f"cust-{i:05d}"}) for i in range(300)]
    cand = [o for o in outs if o.body.get("arm") == "candidate"]
    assert cand and all(o.http_status == 200 and o.body["bundle_version"] == "triage-p1" for o in cand)
    assert all(o.body["fallback"] == "candidate_invalid_output" for o in cand)
    llm_rt.backend.fail_versions.add("triage-p1")
    out = llm_rt.service.predict(_payload(999))
    assert out.http_status == 503 and out.body["routed_to_human"] is True
    out = llm_rt.service.predict({"alert": {"summary": "short"}})
    assert out.http_status == 422


def test_shadow_never_changes_the_answer(llm_rt) -> None:
    p = _payload(5)
    before = llm_rt.service.predict(p).body["triage"]
    rel = llm_rt.controller.propose("triage-p2", "oncall-operator")
    llm_rt.controller.to_staging(rel["release_id"], "oncall-operator")
    llm_rt.controller.to_shadow(rel["release_id"], "oncall-operator")
    llm_rt.backend.fail_versions.add("triage-p2")
    after = llm_rt.service.predict({**p, "request_id": "again"})
    assert after.http_status == 200 and after.body["triage"] == before and after.body["arm"] == "champion"


# -- the release story (same code as the recorded run, scripted models) -------------------------------------------
def test_llm_release_story_in_process(llm_rt) -> None:
    res = llm_release_story(LLMInProcessDriver(llm_rt), "pytest-llm")
    assert res["passed"], json.dumps({"expected": res["expected"], "actual": res["actual"]}, indent=1)
    assert llm_rt.store.verify_event_chain()["ok"]
    rid = res["release_ids"]["D"]
    pm = (llm_rt.report_dir / f"postmortem-{rid}.md").read_text()
    assert "input drift" in pm and "simulated" in pm.lower()
    kinds = [e["kind"] for e in llm_rt.store.events(limit=100_000, release_id=rid)]
    assert {"rollback.executed", "rollback.verified", "postmortem.written"} <= set(kinds)


def test_cost_slo_breach_rolls_back(llm_rt) -> None:
    llm_rt.backend.tokens["triage-p2-ctx4k"] = (420, 140)  # scripted: ~3x the champion's notional cost per call
    rel = llm_rt.controller.propose("triage-p2-ctx4k", "oncall-operator")
    assert rel["state"] == "validated"
    d = LLMInProcessDriver(llm_rt)
    d.stage(rel["release_id"])
    d.shadow(rel["release_id"])
    drive(d, "s", 70, 11)
    d.canary(rel["release_id"])
    drive(d, "c", 600, 12)
    r = d.release(rel["release_id"])
    assert r["state"] == "rolled_back" and "token-cost SLO" in r["reason"]
    assert d.deployment()["champion_version"] == "triage-p1"


def test_latency_slo_breach_rolls_back(llm_rt) -> None:
    llm_rt.backend.latency_ms["triage-p2-ctx4k"] = 9000.0  # scripted: model reload on every candidate call
    rel = llm_rt.controller.propose("triage-p2-ctx4k", "oncall-operator")
    d = LLMInProcessDriver(llm_rt)
    d.stage(rel["release_id"])
    d.shadow(rel["release_id"])
    drive(d, "s", 70, 21)
    d.canary(rel["release_id"])
    drive(d, "c", 600, 22)
    r = d.release(rel["release_id"])
    assert r["state"] == "rolled_back" and "latency SLO" in r["reason"]
    ev = [
        e for e in llm_rt.store.events(limit=10_000, release_id=rel["release_id"]) if e["kind"] == "rollback.verified"
    ]
    assert ev and ev[0]["payload"]["served_by_expected"] == ev[0]["payload"]["requests"]


def test_input_drift_detects_ato_campaign() -> None:
    def rows(scenario: str) -> list[dict[str, Any]]:
        return [{"alert": {"fields": t.payload["alert"]["fields"]}} for t in iter_traffic("d", 400, 5, scenario)]

    assert input_drift(rows("baseline"))["status"] == "stable"
    shifted = input_drift(rows("ato_campaign"))
    assert shifted["status"] == "major" and shifted["features"]["recent_password_reset"]["psi"] > 0.25


# -- API + OpenTelemetry GenAI metrics -------------------------------------------------------------------------
def test_llm_api_and_genai_metrics(rt, llm_rt) -> None:
    from spectral_canary.api import create_app
    from spectral_canary.auth import ensure_operators_file

    app = create_app(rt, start_background=False, llm_rt=llm_rt)
    tokens = ensure_operators_file(rt.paths.operators)
    with TestClient(app) as c:
        ov = c.get("/v1/llm/overview").json()
        assert ov["deployment"]["champion_version"] == "triage-p1" and ov["event_chain"]["ok"]
        r = c.post("/v1/llm/triage", json=_payload(1))
        assert r.status_code == 200 and r.headers["X-Model-Version"] == "triage-p1"
        assert set(r.json()["triage"]) == {"reason_code", "escalate", "rationale"}
        assert c.post(
            "/v1/llm/feedback",
            json={
                "request_id": r.json()["request_id"],
                "reason_code": "CARD_TESTING",
                "escalate": True,
                "simulated": True,
            },
        ).json()["accepted"]
        assert c.post("/v1/llm/releases", json={"candidate_version": "triage-p2"}).status_code == 401
        h = {"Authorization": f"Bearer {tokens['dashboard-viewer']}"}
        assert c.post("/v1/llm/releases", json={"candidate_version": "triage-p2"}, headers=h).status_code == 403
        reg = c.get("/v1/llm/registry").json()
        assert {b["version"] for b in reg} >= {"triage-p1", "triage-p2", "triage-p3", "triage-p2-qwen"}
        assert all(b["ok"] for b in c.get("/v1/llm/registry/verify").json()["bundles"])
        m = c.get("/metrics").text
        assert "gen_ai_client_token_usage_bucket{" in m and 'gen_ai_token_type="output"' in m
        assert "gen_ai_client_operation_duration_seconds_bucket{" in m
        assert 'gen_ai_operation_name="chat"' in m and 'gen_ai_request_model="llama3.2:3b"' in m
        # every metric the Grafana LLM panels query is actually exported
        import re

        dash = json.loads((Path(__file__).parents[1] / "deploy/grafana/dashboards/gateway.json").read_text())
        exprs = [t["expr"] for p in dash["panels"] if p["title"].startswith("LLM triage") for t in p["targets"]]
        names = {n for e in exprs for n in re.findall(r"\b(gen_ai_[a-z_]+_(?:bucket|sum|count))\b", e)}
        assert len(names) >= 3 and all(f"\n{n}{{" in m for n in names), names


def test_promptfoo_config_matches_bundle() -> None:
    """The promptfoo CI suite must test exactly the bundle it claims to (model, params, output contract)."""
    import tomllib

    import yaml

    root = Path(__file__).parents[1]
    cfg = yaml.safe_load((root / "evals/triage/promptfooconfig.yaml").read_text())
    prov = cfg["providers"][0]
    spec = tomllib.loads((root / "configs/llm/bundles/triage-p2.toml").read_text())
    assert prov["id"] == f"ollama:chat:{spec['model']}"
    for k, v in spec["params"].items():
        assert prov["config"][k] == v, k
    from spectral_canary.llm.graders import OUTPUT_SCHEMA

    assert prov["config"]["format"] == OUTPUT_SCHEMA
    assert "triage-p2" in (root / "evals/triage/prompt.cjs").read_text()
    assert spec["prompt_file"].endswith("triage-p2.md") and spec["user_template"] == "Fraud alert:\n{summary}"
