"""`make chaos-replay`: every adversarial scenario from a fresh fixture state, with expected outcomes.

All injected faults are SIMULATED test doubles (``serving.FaultInjector``) acting on the real request
path; the degraded-prediction case uses a genuinely degraded trained model, not an injected fault.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .drivers import InProcessDriver
from .fixtures import fixture_runtime, new_run_dir
from .paths import StatePaths
from .scenarios import BASE, CHAOS_SCENARIOS, Phase, _canary, run_traffic


def _rel(rt: Any, rid: str) -> dict[str, Any]:
    rel = rt.store.get_release(rid)
    assert rel is not None, f"release {rid} missing"
    return rel


class FakeClock:
    def __init__(self, start: float | None = None):
        self.t = start or time.time()

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def chaos_restart_recovery(paths: StatePaths, run_dir: Path) -> dict[str, Any]:
    """Process dies mid-canary. Restart within the stuck window resumes; after it, traffic is reverted."""
    clock = FakeClock()
    rt = fixture_runtime(run_dir, clock=clock)
    d = InProcessDriver(rt)
    rid = _canary(d, "v2-hgb", run_dir.name)
    run_traffic(d, f"{run_dir.name}-c", [Phase(BASE, 700, 1101, "canary before crash")])
    windows_before = _rel(rt, rid)["windows_evaluated"]
    rt.service.writer.flush()
    rt.close()  # simulated crash: no rollback, no cleanup of the pointer

    clock.advance(60)
    rt2 = fixture_runtime(run_dir, clock=clock)
    actions_soon = rt2.controller.recover()
    resumed_state = _rel(rt2, rid)["state"]
    pending = rt2.controller._pending.get(rid)
    rt2.close()

    clock.advance(rt2.policy.canary.stuck_after_seconds + 1)
    rt3 = fixture_runtime(run_dir, clock=clock)
    actions_late = rt3.controller.recover()
    rel = _rel(rt3, rid)
    dep = rt3.store.deployment()
    st, body = InProcessDriver(rt3).predict(
        {
            "request_id": "sim-after-restart",
            "request_key": "card-00001",
            "features": _one_row(rt3),
            "context": {"simulated": True},
        }
    )[:2]
    rt3.close()
    passed = (
        resumed_state == "canary"
        and not actions_soon
        and pending == 700
        and rel["state"] == "reverted_stuck"
        and dep.mode == "none"
        and body.get("model_version") == "v1-logreg"
    )
    return {
        "scenario": "restart_recovery",
        "passed": passed,
        "expected": "restart within stuck window resumes canary with persisted counters; a stuck release "
        "is reverted to the champion on the next start",
        "observed": {
            "windows_before_crash": windows_before,
            "state_after_quick_restart": resumed_state,
            "pending_requests_restored": pending,
            "actions_after_long_outage": actions_late,
            "state_after_long_outage": rel["state"],
            "mode_after": dep.mode,
            "served_after_restart": body.get("model_version"),
            "http_status": st,
        },
    }


def _one_row(rt: Any) -> dict[str, Any]:
    from .data import load_split_csv
    from .schema import decode_row

    X, _, _ = load_split_csv(rt.paths.data / "val.csv")
    return decode_row(X[0])


def chaos_promotion_race(paths: StatePaths, run_dir: Path) -> dict[str, Any]:
    """Two operators act at once (approve vs rollback): exactly one pointer change wins."""
    import threading

    rt = fixture_runtime(run_dir)
    d = InProcessDriver(rt)
    rid = _canary(d, "v2-hgb", run_dir.name)
    run_traffic(d, f"{run_dir.name}-c", [Phase(BASE, 6000, 1201, "canary")], seed=11)
    gen0 = rt.store.deployment().generation
    results: dict[str, Any] = {}

    def approve() -> None:
        try:
            results["approve"] = d.approve(rid)["state"]
        except Exception as exc:
            results["approve"] = f"refused: {exc}"

    def rollback() -> None:
        try:
            results["rollback"] = d.rollback(rid, "race")["rollback_status"]
        except Exception as exc:
            results["rollback"] = f"refused: {exc}"

    ts = [threading.Thread(target=approve), threading.Thread(target=rollback)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    rel = _rel(rt, rid)
    dep = rt.store.deployment()
    events = [e["kind"] for e in rt.store.events(release_id=rid, limit=10_000)]
    winners = events.count("release.promoted") + events.count("rollback.executed")
    rt.close()
    consistent = (rel["state"] == "promoted" and dep.champion_version == "v2-hgb") or (
        rel["state"] == "rolled_back" and dep.champion_version == "v1-logreg"
    )
    # Either order is legal (approve-then-rollback rolls back the promoted release). What must hold: every
    # successful operation made exactly one pointer change (no lost or double-applied update), and the
    # pointer agrees with the release state.
    return {
        "scenario": "promotion_race",
        "passed": consistent and winners == dep.generation - gen0 and winners >= 1,
        "expected": "concurrent approve + rollback serialize: one pointer change per successful operation, "
        "no lost update, pointer and release state agree",
        "observed": {
            "results": results,
            "final_state": rel["state"],
            "champion": dep.champion_version,
            "pointer_changes": dep.generation - gen0,
            "winning_events": winners,
        },
    }


# Latency scenarios make every candidate call slow on purpose; a smaller (still policy-driven) window keeps
# the replay short. The override is recorded in the report.
POLICY_OVERRIDES: dict[str, dict[str, Any]] = {
    "latency_spike": {"window_requests": 600, "min_candidate_requests": 50},
    "candidate_timeout": {"window_requests": 600, "min_candidate_requests": 50},
    # 300-request window; the injected champion delay (60 ms) is over a 40 ms saturation line so the replay stays short
    "gateway_saturation": {"window_requests": 300, "min_candidate_requests": 20, "saturation_champion_p95_ms": 40.0},
}


def run_chaos(paths: StatePaths, only: list[str] | None = None) -> dict[str, Any]:
    root = new_run_dir(paths, "chaos")
    started = time.time()
    out = []
    names = [*CHAOS_SCENARIOS, "restart_recovery", "promotion_race"]
    for name in names:
        if only and name not in only:
            continue
        run_dir = root / name
        t0 = time.time()
        try:
            if name == "restart_recovery":
                res = chaos_restart_recovery(paths, run_dir)
            elif name == "promotion_race":
                res = chaos_promotion_race(paths, run_dir)
            else:
                from .policy import ReleasePolicy

                pol = ReleasePolicy.load()
                if name in POLICY_OVERRIDES:
                    pol.canary = pol.canary.model_copy(update=POLICY_OVERRIDES[name])
                rt = fixture_runtime(run_dir, policy=pol)
                rt.controller.simulated_context = True
                try:
                    res = CHAOS_SCENARIOS[name](InProcessDriver(rt), f"{root.name}-{name}")
                    res["event_chain"] = rt.store.verify_event_chain()
                    res["passed"] = res["passed"] and res["event_chain"]["ok"]
                finally:
                    rt.close()
        except Exception as exc:  # a crashing scenario is a failed scenario, with the error recorded
            res = {"scenario": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
        res["duration_s"] = round(time.time() - t0, 2)
        if name in POLICY_OVERRIDES:
            res["policy_override"] = POLICY_OVERRIDES[name]
        res["simulated"] = True
        out.append(res)
    from .cli import git_commit, machine_info

    report = {
        "run_id": root.name,
        "kind": "chaos",
        "started_at": started,
        "duration_s": round(time.time() - started, 1),
        "passed": all(s["passed"] for s in out),
        "summary": f"{sum(s['passed'] for s in out)}/{len(out)} passed",
        "scenarios": out,
        "machine": machine_info(),
        "commit": git_commit(),
        "note": "All faults are SIMULATED (labeled test doubles on the real request path).",
    }
    p = root / "report.json"
    p.write_text(json.dumps(report, indent=2, default=str))
    report["report_path"] = str(p)
    return report
