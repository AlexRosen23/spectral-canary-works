"""Gate logic, transitions, shadow non-interference, canary guardrails, promotion/rollback atomicity."""

from __future__ import annotations

import threading

import pytest

from spectral_canary.controller import ReleaseError
from spectral_canary.data import load_split_csv
from spectral_canary.drivers import InProcessDriver
from spectral_canary.gates import run_offline_gate
from spectral_canary.monitor import evaluate_canary_window
from spectral_canary.policy import CanaryPolicy
from spectral_canary.scenarios import BASE, SHIFT, _canary, run_traffic
from spectral_canary.schema import decode_row
from spectral_canary.traffic import Phase


def _payloads(rt, n=50, key_prefix="k"):
    X, _, _ = load_split_csv(rt.paths.data / "val.csv")
    return [
        {
            "request_id": f"t-{key_prefix}-{i}",
            "request_key": f"{key_prefix}-{i}",
            "features": decode_row(X[i]),
            "context": {"simulated": True},
        }
        for i in range(n)
    ]


def test_bootstrap_champion_serves_real_predictions(rt) -> None:
    out = rt.service.predict(_payloads(rt, 1)[0])
    assert out.http_status == 200
    assert out.body["model_version"] == "v1-logreg" and out.headers["X-Model-Version"] == "v1-logreg"
    assert 0.0 <= out.body["score"] <= 1.0
    assert out.body["model_sha256"] == rt.store.get_model("v1-logreg")["artifact_sha256"]


def test_gate_passes_v2_v3_and_blocks_v4_with_reasons(rt) -> None:
    g = rt.policy.offline_gate
    assert run_offline_gate("v2-hgb", "v1-logreg", rt.service.pool, rt.paths, g)["passed"]
    assert run_offline_gate("v3-hgb-fresh", "v2-hgb", rt.service.pool, rt.paths, g)["passed"]
    r4 = run_offline_gate("v4-hgb-segfilter", "v2-hgb", rt.service.pool, rt.paths, g)
    assert not r4["passed"]
    assert any("pr_auc_vs_champion" in r for r in r4["blocking_reasons"]), r4["blocking_reasons"]


def test_gate_margins_are_policy_driven(rt) -> None:
    strict = rt.policy.offline_gate.model_copy(update={"max_pr_auc_drop": 0.0, "max_f1_drop": 0.0})
    r = run_offline_gate("v3-hgb-fresh", "v2-hgb", rt.service.pool, rt.paths, strict)
    assert not r["passed"]


def test_illegal_transitions_refused(rt) -> None:
    d = InProcessDriver(rt)
    r = d.propose("v2-hgb")
    with pytest.raises(Exception, match="illegal transition"):
        rt.controller.to_canary(r["release_id"], "x")
    with pytest.raises(ReleaseError, match=r"already active|illegal"):
        rt.controller.approve(r["release_id"], "release-manager", "release_manager")
    rt.controller.to_staging(r["release_id"], "x")
    with pytest.raises(ReleaseError, match="already active"):
        rt.controller.propose("v3-hgb-fresh", "oncall-operator")


def test_shadow_never_changes_responses(rt) -> None:
    payloads = _payloads(rt, 60)
    before = [rt.service.predict(p).body for p in payloads]
    d = InProcessDriver(rt)
    r = d.propose("v2-hgb")
    d.stage(r["release_id"])
    d.shadow(r["release_id"])
    rt.faults.set("v2-hgb", __import__("spectral_canary.serving", fromlist=["Fault"]).Fault(error_rate=0.5))
    after = [rt.service.predict({**p, "request_id": p["request_id"] + "-s"}).body for p in payloads]
    rt.faults.clear()
    keys = ("model_version", "score", "decision", "arm")
    assert [{k: b[k] for k in keys} for b in before] == [{k: a[k] for k in keys} for a in after]
    rt.service.writer.flush()
    rows = rt.store.requests_since(0, r["release_id"])
    assert {row["shadow_status"] for row in rows} <= {"ok", "error"} and any(
        row["shadow_status"] == "error" for row in rows
    )


def test_canary_routes_about_ten_percent_and_tags_version(rt) -> None:
    d = InProcessDriver(rt)
    _canary(d, "v2-hgb", "t")
    s = run_traffic(d, "t-route", [Phase(BASE, 1000, 7, "route")])
    v = s["by_phase"]["route"]["versions"]
    share = v.get("v2-hgb", 0) / 1000
    assert 0.07 <= share <= 0.13, v
    # same key -> same arm
    p = _payloads(rt, 1)[0]
    arms = {rt.service.predict({**p, "request_id": f"sticky-{i}"}).body["arm"] for i in range(10)}
    assert len(arms) == 1


def test_promotion_requires_evidence_role_and_separation_of_duties(rt) -> None:
    d = InProcessDriver(rt)
    rid = _canary(d, "v2-hgb", "t")
    with pytest.raises(ReleaseError, match="not eligible"):
        rt.controller.approve(rid, "release-manager", "release_manager")
    run_traffic(d, "t-c", [Phase(BASE, 6000, 102, "canary")], seed=11)
    with pytest.raises(ReleaseError) as e:
        rt.controller.approve(rid, "oncall-operator", "operator")
    assert e.value.status == 403
    with pytest.raises(ReleaseError, match="separation of duties"):
        rt.controller.approve(rid, "oncall-operator", "release_manager")
    rel = rt.controller.approve(rid, "release-manager", "release_manager")
    assert rel["state"] == "promoted"
    dep = rt.store.deployment()
    assert dep.champion_version == "v2-hgb" and dep.last_known_good == "v1-logreg" and dep.mode == "none"
    assert rt.service.predict(_payloads(rt, 1)[0]).body["model_version"] == "v2-hgb"


def test_rollback_of_promoted_champion_restores_previous(rt) -> None:
    d = InProcessDriver(rt)
    rid = _canary(d, "v2-hgb", "t")
    run_traffic(d, "t-c", [Phase(BASE, 6000, 102, "canary")], seed=11)
    d.approve(rid)
    out = d.rollback(rid, "post-promotion regression reported")
    assert out["rollback_status"] == "rolled_back" and out["verification"]["passed"]
    assert rt.store.deployment().champion_version == "v1-logreg"


def test_breach_policy_pause_vs_rollback(scw_home, tmp_path) -> None:
    from spectral_canary.fixtures import fixture_runtime
    from spectral_canary.policy import ReleasePolicy
    from spectral_canary.serving import Fault

    pol = ReleasePolicy.load()
    pol.canary = pol.canary.model_copy(update={"on_breach": "pause"})
    rt = fixture_runtime(tmp_path / "pause", home=scw_home, policy=pol)
    try:
        d = InProcessDriver(rt)
        rid = _canary(d, "v2-hgb", "p")
        rt.faults.set("v2-hgb", Fault(error_rate=0.5))
        run_traffic(d, "p-c", [Phase(BASE, 1600, 3, "outage")])
        rel = rt.store.get_release(rid)
        assert rel["state"] == "paused"
        assert rt.store.deployment().mode == "none"
        kinds = [e["kind"] for e in rt.store.events(release_id=rid)]
        assert "release.paused" in kinds and "rollback.executed" not in kinds
    finally:
        rt.close()


def test_in_flight_requests_survive_pointer_swaps(rt) -> None:
    d = InProcessDriver(rt)
    rid = _canary(d, "v2-hgb", "t")
    payloads = _payloads(rt, 40, "flight")
    errors: list[str] = []
    seen: set[str] = set()
    stop = threading.Event()

    def hammer(n: int) -> None:
        i = 0
        while not stop.is_set():
            p = payloads[i % len(payloads)]
            out = rt.service.predict({**p, "request_id": f"f{n}-{i}"})
            if out.http_status != 200:
                errors.append(str(out.body))
            seen.add(out.body.get("model_version"))
            i += 1

    ts = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in ts:
        t.start()
    rt.controller.rollback(rid, "oncall-operator", "manual", "swap under load")
    stop.set()
    for t in ts:
        t.join()
    assert errors == []
    assert seen <= {"v1-logreg", "v2-hgb"}


def test_guardrail_ignores_small_samples() -> None:
    rows = [
        {
            "seq": i,
            "request_id": f"r{i}",
            "request_key": f"k{i}",
            "arm": "candidate",
            "status": "ok",
            "shadow_status": "ok",
            "decision": 0,
            "latency_ms": 1.0,
            "label": 1,
            "champion_decision": 1,
            "simulated": 1,
        }
        for i in range(10)
    ]
    ev = evaluate_canary_window(rows, rows, CanaryPolicy(), 1)
    assert ev["verdict"] == "insufficient_evidence" and not ev["breaches"]


def test_degraded_candidate_rolls_back_under_shift_only(rt) -> None:
    """End-to-end (in-process): v3 is healthy on baseline traffic and is rolled back after the shift."""
    d = InProcessDriver(rt)
    rid2 = _canary(d, "v2-hgb", "v2")
    run_traffic(d, "v2-c", [Phase(BASE, 6000, 102, "canary")], seed=11)
    d.approve(rid2)
    rid = _canary(d, "v3-hgb-fresh", "v3")
    run_traffic(d, "v3-base", [Phase(BASE, 3000, 202, "baseline")], seed=78)
    rel = rt.store.get_release(rid)
    assert rel["state"] == "canary", (rel["reason"], rel["last_evaluation"])
    assert rel["last_evaluation"]["verdict"] in ("healthy", "insufficient_evidence")
    run_traffic(d, "v3-shift", [Phase(SHIFT, 6000, 203, "shift")], seed=79)
    rel = rt.store.get_release(rid)
    assert rel["state"] == "rolled_back", rel["last_evaluation"]
    assert rt.store.deployment().champion_version == "v2-hgb"
    kinds = [e["kind"] for e in rt.store.events(release_id=rid)]
    assert kinds.index("rollback.executed") < kinds.index("rollback.verified") < kinds.index("postmortem.written")
    written = next(e for e in rt.store.events(release_id=rid) if e["kind"] == "postmortem.written")
    assert written["payload"]["path"] == f"reports/postmortem-{rid}.md"  # never an absolute local path

    # The postmortem's impact must agree with the evidence that triggered the rollback (it used to count requests,
    # one direction only, with labels that arrived after the rollback, so its numbers looked contradictory).
    from spectral_canary.postmortem import build_postmortem

    pm = build_postmortem(rt.store, rid)
    imp, paired = pm["impact"], pm["breach_evaluation"]["payload"]["labeled"]["paired"]
    assert imp["labeled_fraud_cards_paired"] == paired["positives"]
    assert imp["cards_missed_by_candidate_only"] == paired["missed_by_candidate_only"]
    assert imp["cards_missed_by_champion_only"] == paired["missed_by_champion_only"]
    run_traffic(d, "v3-after", [Phase(BASE, 300, 204, "after rollback")], seed=80)  # more labels arrive later
    assert build_postmortem(rt.store, rid)["impact"] == imp


def test_guardrail_counts_cards_not_requests() -> None:
    """Regression (found in the c=32 load test): 140 candidate requests from 35 cards, none flagged, must not
    breach on a CI that pretends the 140 requests are independent."""

    def row(seq: int, arm: str, key: str, decision: int) -> dict:
        return {
            "seq": seq,
            "request_id": f"r{seq}",
            "request_key": key,
            "arm": arm,
            "status": "ok",
            "shadow_status": "ok" if arm == "candidate" else None,
            "decision": decision,
            "latency_ms": 2.0,
            "shadow_latency_ms": 1.0,
            "champion_latency_ms": 1.0,
            "label": None,
            "simulated": 1,
        }

    rows, seq = [], 0
    for _rep in range(4):  # 35 candidate cards, each repeated 4x, never flagged
        for k in range(35):
            seq += 1
            rows.append(row(seq, "candidate", f"cand-{k}", 0))
    for k in range(1300):  # champion arm: 1300 distinct cards, ~4.3% flagged
        seq += 1
        rows.append(row(seq, "champion", f"champ-{k}", int(k % 23 == 0)))
    ev = evaluate_canary_window(rows, rows, CanaryPolicy(), 1)
    assert ev["arms"]["candidate"]["keys"] == 35
    assert not any("flags" in b for b in ev["breaches"]), ev["breaches"]


def _window(champ_ms: float, cand_failures: list[str], n_cand: int = 150, n_champ: int = 1350) -> list[dict]:
    """A canary window: `cand_failures` lists the candidate-call outcomes that failed ("timeout"/"error")."""
    rows: list[dict] = []
    for i in range(n_cand):
        status = cand_failures[i] if i < len(cand_failures) else "ok"
        rows.append(
            {
                "seq": i,
                "request_id": f"c{i}",
                "request_key": f"c{i}",
                "arm": "candidate",
                "status": "ok",
                "shadow_status": status,
                "decision": int(i % 20 == 0),
                "latency_ms": champ_ms + 5,
                "shadow_latency_ms": 8.0 if status == "ok" else None,
                "champion_latency_ms": champ_ms,
                "label": None,
                "simulated": 1,
            }
        )
    for i in range(n_champ):
        rows.append(
            {
                "seq": n_cand + i,
                "request_id": f"h{i}",
                "request_key": f"h{i}",
                "arm": "champion",
                "status": "ok",
                "shadow_status": None,
                "decision": int(i % 20 == 0),
                "latency_ms": champ_ms + 1,
                "champion_latency_ms": champ_ms,
                "label": None,
                "simulated": 1,
            }
        )
    return rows


def test_timeouts_under_saturation_are_inconclusive_not_a_breach() -> None:
    """Load test finding 5: the champion is slow too, so candidate timeouts are capacity, not the model."""
    rows = _window(champ_ms=180.0, cand_failures=["timeout"] * 9)  # 6% > 2% budget, all timeouts
    ev = evaluate_canary_window(rows, rows, CanaryPolicy(), 1)
    assert ev["verdict"] == "inconclusive_saturated", ev
    assert not ev["breaches"]
    assert ev["saturation"]["saturated"] and "gateway saturated" in ev["saturation"]["reason"]


def test_same_timeouts_with_a_fast_champion_still_breach() -> None:
    rows = _window(champ_ms=4.0, cand_failures=["timeout"] * 9)
    ev = evaluate_canary_window(rows, rows, CanaryPolicy(), 1)
    assert ev["verdict"] == "breach" and any("error rate" in b for b in ev["breaches"])
    assert not ev["saturation"]["saturated"]


def test_real_errors_breach_even_under_saturation() -> None:
    """Saturation explains timeouts, not exceptions: non-timeout errors over budget still roll back."""
    rows = _window(champ_ms=180.0, cand_failures=["error"] * 5 + ["timeout"] * 4)
    ev = evaluate_canary_window(rows, rows, CanaryPolicy(), 1)
    assert ev["verdict"] == "breach", ev


def test_saturated_window_pauses_the_release_without_rollback(scw_home, tmp_path) -> None:
    from spectral_canary.fixtures import fixture_runtime
    from spectral_canary.policy import ReleasePolicy
    from spectral_canary.serving import Fault

    pol = ReleasePolicy.load()
    pol.canary = pol.canary.model_copy(
        update={"window_requests": 300, "min_candidate_requests": 20, "saturation_champion_p95_ms": 40.0}
    )
    rt = fixture_runtime(tmp_path / "sat", home=scw_home, policy=pol)
    try:
        d = InProcessDriver(rt)
        rid = _canary(d, "v2-hgb", "sat")
        rt.faults.set("v1-logreg", Fault(latency_ms=60.0))
        rt.faults.set("v2-hgb", Fault(latency_ms=650.0))
        run_traffic(d, "sat-c", [Phase(BASE, 340, 4, "saturated")])
        rel = rt.store.get_release(rid)
        assert rel["state"] == "paused", rel["last_evaluation"]
        assert rel["reason"].startswith("inconclusive: gateway saturated")
        assert rt.store.deployment().champion_version == "v1-logreg"
        kinds = [e["kind"] for e in rt.store.events(release_id=rid)]
        assert "release.paused" in kinds and "rollback.executed" not in kinds and "postmortem.written" not in kinds
    finally:
        rt.close()
