"""Seeded release and failure scenarios with expected outcomes.

Every scenario: starts from a known fixture state (fresh registry with v1..v4 registered and v1 bootstrapped
as champion), sends labeled SIMULATED traffic through real handlers, and checks its expected outcome.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .drivers import DriverError
from .traffic import Phase, TrafficPlan, drive

BASE = "baseline"
SHIFT = "high_value_online_surge"


def _sender(driver: Any) -> tuple[Callable, Callable]:
    def send(p: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, body, _ = driver.predict(p)
        return status, body

    return send, driver.feedback


def run_traffic(driver: Any, run_id: str, phases: list[Phase], seed: int = 1234, **kw: Any) -> dict[str, Any]:
    send, fb = _sender(driver)
    stats = drive(TrafficPlan(run_id, phases, seed=seed, **kw), send, fb)
    driver.flush()
    return stats


def step(log: list[dict[str, Any]], name: str, **data: Any) -> None:
    log.append({"t": round(time.time(), 3), "step": name, **data})


def _require_validated(rel: dict[str, Any]) -> None:
    if rel["state"] != "validated":
        raise RuntimeError(f"{rel['candidate_version']} unexpectedly {rel['state']}: {rel.get('reason')}")


# -- the 90-second story -----------------------------------------------------------------------------------
def release_story(driver: Any, run_id: str, scale: float = 1.0) -> dict[str, Any]:
    """v2 promoted through a healthy canary; v4 blocked offline; v3 canary fails under shift and rolls back."""
    n = lambda k: max(1, int(k * scale))  # noqa: E731
    steps: list[dict[str, Any]] = []
    expected: dict[str, Any] = {}
    actual: dict[str, Any] = {}

    # Release A: v1 -> v2 (healthy, approved)
    a = driver.propose("v2-hgb")
    step(steps, "propose v2-hgb", release_id=a["release_id"], state=a["state"])
    _require_validated(a)
    driver.stage(a["release_id"])
    driver.shadow(a["release_id"])
    s = run_traffic(driver, f"{run_id}-A-shadow", [Phase(BASE, n(600), 101, "A shadow (baseline)")])
    step(steps, "shadow traffic", release_id=a["release_id"], traffic=s)
    driver.canary(a["release_id"])
    s = run_traffic(driver, f"{run_id}-A-canary", [Phase(BASE, n(6000), 102, "A canary (baseline)")])
    step(steps, "canary traffic", release_id=a["release_id"], traffic=s)
    try:
        driver.approve(a["release_id"], actor="oncall-operator", role="operator")
        actual["self_approval_refused"] = False
    except DriverError as exc:
        actual["self_approval_refused"] = exc.status == 403
    rel = driver.approve(a["release_id"])
    step(steps, "approve v2-hgb", release_id=a["release_id"], state=rel["state"])
    expected["A"] = "promoted"
    actual["A"] = rel["state"]
    expected["self_approval_refused"] = True

    # Release B: v4 must be blocked by the offline gate.
    b = driver.propose("v4-hgb-segfilter")
    step(
        steps,
        "propose v4-hgb-segfilter",
        release_id=b["release_id"],
        state=b["state"],
        blocking_reasons=(b.get("gate_report") or {}).get("blocking_reasons"),
    )
    expected["B"] = "blocked"
    actual["B"] = b["state"]

    # Release C: v2 -> v3 (degraded); healthy on baseline, fails under SIMULATED shift.
    c = driver.propose("v3-hgb-fresh")
    step(
        steps,
        "propose v3-hgb-fresh",
        release_id=c["release_id"],
        state=c["state"],
        gate_checks=[(x["name"], x["passed"]) for x in (c.get("gate_report") or {}).get("checks", [])],
    )
    _require_validated(c)
    driver.stage(c["release_id"])
    driver.shadow(c["release_id"])
    s = run_traffic(driver, f"{run_id}-C-shadow", [Phase(BASE, n(600), 201, "C shadow (baseline)")], seed=77)
    step(steps, "shadow traffic", release_id=c["release_id"], traffic=s)
    driver.canary(c["release_id"])
    s = run_traffic(
        driver,
        f"{run_id}-C-canary",
        [
            Phase(BASE, n(3000), 202, "C canary (baseline)"),
            Phase(SHIFT, n(6000), 203, "C canary (SIMULATED shift)"),
        ],
        seed=78,
    )
    step(steps, "canary traffic with injected shift", release_id=c["release_id"], traffic=s)
    rel = driver.release(c["release_id"])
    dep = driver.deployment()
    expected["C"] = "rolled_back"
    actual["C"] = rel["state"]
    expected["champion_after"] = "v2-hgb"
    actual["champion_after"] = dep["champion_version"]
    expected["mode_after"] = "none"
    actual["mode_after"] = dep["mode"]
    # Every response after the rollback must come from the champion.
    post = run_traffic(driver, f"{run_id}-post", [Phase(SHIFT, n(200), 204, "post-rollback check")], seed=79)
    versions = {v for ph in post["by_phase"].values() for v in ph["versions"]}
    expected["post_rollback_versions"] = ["v2-hgb"]
    actual["post_rollback_versions"] = sorted(versions)
    step(steps, "post-rollback traffic", traffic=post)
    passed = all(actual.get(k) == v for k, v in expected.items())
    return {
        "scenario": "release_story",
        "passed": passed,
        "expected": expected,
        "actual": actual,
        "release_ids": {"A": a["release_id"], "B": b["release_id"], "C": c["release_id"]},
        "steps": steps,
    }


# -- chaos scenarios (in-process; each gets a fresh fixture runtime) ---------------------------------------
def _canary(driver: Any, version: str, run_id: str, shadow_n: int = 450) -> str:
    r = driver.propose(version)
    _require_validated(r)
    driver.stage(r["release_id"])
    driver.shadow(r["release_id"])
    run_traffic(driver, f"{run_id}-shadow", [Phase(BASE, shadow_n, 301, "shadow")], seed=5)
    driver.canary(r["release_id"])
    return r["release_id"]


def chaos_schema_break(driver: Any, run_id: str) -> dict[str, Any]:
    bad = [
        {
            "request_id": f"sim-{run_id}-bad-missing",
            "features": {"amount": 10.0},
            "context": {"simulated": True},
        },
        {
            "request_id": f"sim-{run_id}-bad-cat",
            "context": {"simulated": True},
            "features": {
                "amount": 12.0,
                "merchant_category": "crypto_atm",
                "hour_of_day": 3,
                "account_age_days": 5,
                "txn_count_24h": 1,
                "distance_from_home_km": 1.0,
                "device_trust_score": 0.5,
                "card_present": True,
                "country_mismatch": False,
            },
        },
        {
            "request_id": f"sim-{run_id}-bad-type",
            "context": {"simulated": True},
            "features": {
                "amount": "12.00",
                "merchant_category": "grocery",
                "hour_of_day": 3,
                "account_age_days": 5,
                "txn_count_24h": 1,
                "distance_from_home_km": 1.0,
                "device_trust_score": 0.5,
                "card_present": "yes",
                "country_mismatch": False,
            },
        },
        {
            "request_id": f"sim-{run_id}-bad-range",
            "context": {"simulated": True},
            "features": {
                "amount": -5.0,
                "merchant_category": "grocery",
                "hour_of_day": 31,
                "account_age_days": 5,
                "txn_count_24h": 1,
                "distance_from_home_km": 1.0,
                "device_trust_score": 1.7,
                "card_present": True,
                "country_mismatch": False,
                "extra_field": 1,
            },
        },
    ]
    results = [driver.predict(p) for p in bad]
    good = run_traffic(driver, f"{run_id}-good", [Phase(BASE, 100, 401, "valid traffic")])
    rejected = [s for s, _, _ in results if s == 422]
    ok_versions = {v for ph in good["by_phase"].values() for v in ph["versions"]}
    passed = len(rejected) == len(bad) and good["by_phase"]["valid traffic"]["status"].get("200") == 100
    return {
        "scenario": "schema_break",
        "passed": passed,
        "expected": "all malformed payloads rejected with 422 before any model call; valid traffic unaffected",
        "observed": {
            "rejected": len(rejected),
            "sent_malformed": len(bad),
            "problems": [b.get("problems") for _, b, _ in results],
            "valid_status": good["by_phase"]["valid traffic"]["status"],
            "valid_versions": sorted(ok_versions),
        },
    }


def chaos_distribution_shift(driver: Any, run_id: str) -> dict[str, Any]:
    """Champion only, no release: drift is detected and reported, nothing is rolled back."""
    run_traffic(driver, f"{run_id}-base", [Phase(BASE, 1000, 501, "baseline")])
    rt = driver.rt
    from .monitor import drift_report

    rows = rt.store.requests_since(0)
    base = drift_report([r for r in rows if r["scenario"] == BASE], rt.paths.data / "train.csv")
    run_traffic(driver, f"{run_id}-shift", [Phase(SHIFT, 1000, 502, "shift")])
    rows = rt.store.requests_since(0)
    shifted = drift_report([r for r in rows if r["scenario"] == SHIFT], rt.paths.data / "train.csv")
    dep = driver.deployment()
    passed = base["status"] == "stable" and shifted["status"] == "major" and dep["champion_version"] == "v1-logreg"
    return {
        "scenario": "distribution_shift",
        "passed": passed,
        "expected": "baseline PSI stable, shifted PSI major; drift alone never moves traffic",
        "observed": {
            "baseline_max_psi": base["max_psi"],
            "shift_max_psi": shifted["max_psi"],
            "shift_top_features": sorted(
                ((k, v["psi"]) for k, v in shifted["features"].items()), key=lambda kv: -kv[1]
            )[:4],
            "champion_after": dep["champion_version"],
        },
    }


def chaos_degraded_predictions(driver: Any, run_id: str) -> dict[str, Any]:
    _promote_v2(driver, run_id)
    rid = _canary(driver, "v3-hgb-fresh", run_id)
    run_traffic(driver, f"{run_id}-c", [Phase(SHIFT, 4500, 601, "SIMULATED shift")], seed=9)
    rel = driver.release(rid)
    ev = rel.get("last_evaluation") or {}
    passed = rel["state"] == "rolled_back" and driver.deployment()["champion_version"] == "v2-hgb"
    return {
        "scenario": "degraded_predictions",
        "passed": passed,
        "expected": "labeled guardrail detects the degraded candidate under shift; automatic rollback to v2-hgb",
        "observed": {
            "state": rel["state"],
            "reason": rel.get("reason"),
            "breaches": ev.get("breaches"),
            "paired": (ev.get("labeled_cumulative") or {}).get("paired"),
            "champion_after": driver.deployment()["champion_version"],
        },
    }


def _promote_v2(driver: Any, run_id: str) -> None:
    """Fast path to a v2 champion for scenarios that need one (uses the real gate + canary + approval)."""
    rid = _canary(driver, "v2-hgb", f"{run_id}-v2")
    run_traffic(driver, f"{run_id}-v2-canary", [Phase(BASE, 6000, 701, "v2 canary")], seed=11)
    driver.approve(rid)


def chaos_latency_spike(driver: Any, run_id: str) -> dict[str, Any]:
    rid = _canary(driver, "v2-hgb", run_id)
    driver.set_fault("v2-hgb", latency_ms=220.0, note="SIMULATED latency spike on candidate")
    run_traffic(driver, f"{run_id}-c", [Phase(BASE, 700, 801, "latency spike")])
    driver.clear_faults()
    rel = driver.release(rid)
    ev = rel.get("last_evaluation") or {}
    passed = rel["state"] == "rolled_back" and any("latency" in b for b in ev.get("breaches", []))
    return {
        "scenario": "latency_spike",
        "passed": passed,
        "expected": "candidate p95 above budget -> rollback; users kept getting answers",
        "observed": {
            "state": rel["state"],
            "breaches": ev.get("breaches"),
            "candidate_p95_ms": ((ev.get("arms") or {}).get("candidate") or {}).get("latency_ms", {}).get("p95"),
        },
    }


def chaos_candidate_timeout(driver: Any, run_id: str) -> dict[str, Any]:
    rid = _canary(driver, "v2-hgb", run_id)
    driver.set_fault("v2-hgb", latency_ms=650.0, note="SIMULATED hang beyond model timeout")
    s = run_traffic(driver, f"{run_id}-c", [Phase(BASE, 700, 901, "timeouts")])
    driver.clear_faults()
    rel = driver.release(rid)
    ev = rel.get("last_evaluation") or {}
    statuses = s["by_phase"]["timeouts"]["status"]
    passed = rel["state"] == "rolled_back" and set(statuses) == {"200"}
    return {
        "scenario": "candidate_timeout",
        "passed": passed,
        "expected": "candidate timeouts degrade gracefully to the champion (HTTP 200), error budget -> rollback",
        "observed": {"state": rel["state"], "http_statuses": statuses, "breaches": ev.get("breaches")},
    }


def chaos_partial_outage(driver: Any, run_id: str) -> dict[str, Any]:
    rid = _canary(driver, "v2-hgb", run_id)
    driver.set_fault("v2-hgb", error_rate=0.3, note="SIMULATED partial outage: 30% candidate errors")
    run_traffic(driver, f"{run_id}-c", [Phase(BASE, 1600, 1001, "partial outage")])
    driver.clear_faults()
    rel = driver.release(rid)
    # Champion-side partial outage (no release active): requests fail closed with 503 + Retry-After.
    driver.set_fault("v1-logreg", error_rate=0.05, note="SIMULATED champion partial outage")
    s = run_traffic(driver, f"{run_id}-champ", [Phase(BASE, 400, 1002, "champion outage")])
    driver.clear_faults()
    st = s["by_phase"]["champion outage"]["status"]
    passed = rel["state"] == "rolled_back" and set(st) <= {"200", "503"} and st.get("503", 0) > 0
    return {
        "scenario": "partial_outage",
        "passed": passed,
        "expected": "candidate error budget breach -> rollback; "
        "champion errors fail closed with 503, never a fake score",
        "observed": {"state": rel["state"], "reason": rel.get("reason"), "champion_outage_statuses": st},
    }


def chaos_corrupted_artifact(driver: Any, run_id: str) -> dict[str, Any]:
    """A registered artifact whose bytes change can never be loaded, gated, promoted or rolled back onto."""
    import os
    import shutil

    from .registry import register_from_dir

    rt = driver.rt
    work = rt.db_path.parent / "tamper"
    src = rt.paths.artifacts / "v2-hgb"
    dst = work / "v2-hgb-tampered"
    shutil.copytree(src, dst)
    import json as _json

    m = _json.loads((dst / "manifest.json").read_text())
    m["version"] = "v2-hgb-tampered"
    os.chmod(dst / "manifest.json", 0o644)
    (dst / "manifest.json").write_text(_json.dumps(m))
    register_from_dir(rt.store, rt.paths, dst, actor="chaos-replay")
    f = dst / "model.skops"
    os.chmod(f, 0o644)
    data = bytearray(f.read_bytes())
    data[len(data) // 2] ^= 0xFF
    f.write_bytes(bytes(data))
    rt.store.append_event(
        "chaos.artifact_tampered",
        {"version": "v2-hgb-tampered", "byte_flipped": len(data) // 2},
        actor="chaos-replay",
        simulated=True,
    )
    r = driver.propose("v2-hgb-tampered")
    # Also try registering an unreadable (truncated) artifact.
    trunc = work / "v2-hgb-truncated"
    shutil.copytree(src, trunc)
    m["version"] = "v2-hgb-truncated"
    os.chmod(trunc / "manifest.json", 0o644)
    (trunc / "manifest.json").write_text(_json.dumps(m))
    os.chmod(trunc / "model.skops", 0o644)
    (trunc / "model.skops").write_bytes((src / "model.skops").read_bytes()[:500])
    try:
        register_from_dir(rt.store, rt.paths, trunc, actor="chaos-replay")
        trunc_refused = False
    except Exception as exc:
        trunc_refused = "artifact" in str(exc)
    dep = driver.deployment()
    passed = r["state"] == "blocked" and trunc_refused and dep["champion_version"] == "v1-logreg"
    return {
        "scenario": "corrupted_artifact",
        "passed": passed,
        "expected": "tampered artifact blocked at the gate (hash mismatch); truncated artifact refused at registration",
        "observed": {
            "tampered_release_state": r["state"],
            "blocking_reasons": (r.get("gate_report") or {}).get("blocking_reasons"),
            "truncated_registration_refused": trunc_refused,
            "champion_after": dep["champion_version"],
        },
    }


def chaos_repeat_rollback(driver: Any, run_id: str) -> dict[str, Any]:
    rid = _canary(driver, "v2-hgb", run_id)
    first = driver.rollback(rid, "operator rollback #1")
    gen = driver.deployment()["generation"]
    second = driver.rollback(rid, "operator rollback #2 (duplicate)")
    gen2 = driver.deployment()["generation"]
    passed = (
        first["rollback_status"] == "rolled_back" and second["rollback_status"] == "already_rolled_back" and gen == gen2
    )
    return {
        "scenario": "repeat_rollback",
        "passed": passed,
        "expected": "second rollback is an idempotent no-op (pointer generation unchanged), and is audited",
        "observed": {
            "first": first["rollback_status"],
            "second": second["rollback_status"],
            "generation_after_first": gen,
            "generation_after_second": gen2,
            "verification": first.get("verification", {}).get("passed"),
        },
    }


CHAOS_SCENARIOS: dict[str, Callable[[Any, str], dict[str, Any]]] = {
    "schema_break": chaos_schema_break,
    "distribution_shift": chaos_distribution_shift,
    "degraded_predictions": chaos_degraded_predictions,
    "latency_spike": chaos_latency_spike,
    "candidate_timeout": chaos_candidate_timeout,
    "partial_outage": chaos_partial_outage,
    "corrupted_artifact": chaos_corrupted_artifact,
    "repeat_rollback": chaos_repeat_rollback,
}
