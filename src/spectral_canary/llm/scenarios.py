"""Drivers and the recorded release story for the LLM triage lane.

``LLMHttpDriver`` talks to a running gateway over real HTTP (``make llm-demo``); ``LLMInProcessDriver`` calls the
runtime directly (tests, with the scripted test-double backend). The same story code runs on both.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from ..controller import ReleaseError
from ..drivers import DriverError
from .alerts import iter_traffic

CHAMPION0 = "triage-p1"
GOOD = "triage-p2"
REGRESSED = "triage-p3"
SWAP = "triage-p2-qwen"
CTX = "triage-p2-ctx4k"


class LLMInProcessDriver:
    kind = "in_process"

    def __init__(self, rt: Any):
        self.rt = rt
        self.c = rt.controller

    def triage(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        out = self.rt.service.predict(payload)
        return out.http_status, out.body

    def feedback(self, fb: dict[str, Any]) -> None:
        self.rt.store.insert_llm_feedback(fb["request_id"], fb["reason_code"], fb["escalate"], fb["simulated"])

    def _w(self, fn: Any, *a: Any) -> Any:
        try:
            return fn(*a)
        except ReleaseError as exc:
            raise DriverError(exc.status, str(exc)) from exc

    def propose(self, v: str) -> dict[str, Any]:
        return self._w(self.c.propose, v, "oncall-operator")

    def stage(self, rid: str) -> dict[str, Any]:
        return self._w(self.c.to_staging, rid, "oncall-operator")

    def shadow(self, rid: str) -> dict[str, Any]:
        return self._w(self.c.to_shadow, rid, "oncall-operator")

    def canary(self, rid: str) -> dict[str, Any]:
        return self._w(self.c.to_canary, rid, "oncall-operator")

    def approve(self, rid: str, actor: str = "release-manager", role: str = "release_manager") -> dict[str, Any]:
        return self._w(self.c.approve, rid, actor, role)

    def rollback(self, rid: str, reason: str) -> dict[str, Any]:
        return self._w(self.c.rollback, rid, "oncall-operator", "manual", reason)

    def release(self, rid: str) -> dict[str, Any]:
        return self.rt.store.get_release(rid) or {}

    def deployment(self) -> dict[str, Any]:
        return self.rt.store.deployment().as_dict()

    def flush(self) -> None:
        self.rt.service.writer.flush()


class LLMHttpDriver:
    kind = "http"

    def __init__(self, base_url: str, tokens: dict[str, str], timeout: float = 7200.0):
        self.http = httpx.Client(base_url=base_url, timeout=timeout)
        self.tokens = tokens

    def _post(self, path: str, actor: str = "oncall-operator", json: Any = None) -> dict[str, Any]:
        r = self.http.post(path, headers={"Authorization": f"Bearer {self.tokens[actor]}"}, json=json or {})
        if r.status_code >= 400:
            raise DriverError(r.status_code, r.json().get("detail", r.text))
        return r.json()

    def triage(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        r = self.http.post("/v1/llm/triage", json=payload)
        return r.status_code, r.json()

    def feedback(self, fb: dict[str, Any]) -> None:
        self.http.post("/v1/llm/feedback", json=fb).raise_for_status()

    def propose(self, v: str) -> dict[str, Any]:
        return self._post("/v1/llm/releases", json={"candidate_version": v})

    def stage(self, rid: str) -> dict[str, Any]:
        return self._post(f"/v1/llm/releases/{rid}/stage")

    def shadow(self, rid: str) -> dict[str, Any]:
        return self._post(f"/v1/llm/releases/{rid}/shadow")

    def canary(self, rid: str) -> dict[str, Any]:
        return self._post(f"/v1/llm/releases/{rid}/canary")

    def approve(self, rid: str, actor: str = "release-manager", role: str = "release_manager") -> dict[str, Any]:
        return self._post(f"/v1/llm/releases/{rid}/approve", actor)

    def rollback(self, rid: str, reason: str) -> dict[str, Any]:
        return self._post(f"/v1/llm/releases/{rid}/rollback", json={"reason": reason})

    def release(self, rid: str) -> dict[str, Any]:
        return self.http.get(f"/v1/llm/releases/{rid}").json()

    def deployment(self) -> dict[str, Any]:
        return self.http.get("/v1/llm/overview").json()["deployment"]

    def flush(self) -> None:
        pass


def drive(
    driver: Any, run_id: str, n: int, seed: int, scenario: str = "baseline", label_delay: int = 20, start_index: int = 0
) -> dict[str, Any]:
    """Send n SIMULATED alerts; deliver each analyst label ``label_delay`` requests later (10% never arrive)."""
    pending: list[tuple[int, dict[str, Any]]] = []
    st: dict[str, Any] = {"requests": n, "status": {}, "versions": {}, "arms": {}, "fallbacks": 0, "labels_sent": 0}
    for t in iter_traffic(run_id, n, seed, scenario, start_index=start_index):
        status, body = driver.triage(t.payload)
        st["status"][str(status)] = st["status"].get(str(status), 0) + 1
        if status == 200:
            v = body["bundle_version"]
            st["versions"][v] = st["versions"].get(v, 0) + 1
            st["arms"][body["arm"]] = st["arms"].get(body["arm"], 0) + 1
            st["fallbacks"] += 1 if body.get("fallback") else 0
            if t.label_arrives:
                pending.append(
                    (t.index + label_delay, {"request_id": t.payload["request_id"], **t.expected, "simulated": True})
                )
        while pending and pending[0][0] <= t.index:
            driver.feedback(pending.pop(0)[1])
            st["labels_sent"] += 1
    for _, fb in pending:
        driver.feedback(fb)
        st["labels_sent"] += 1
    driver.flush()
    return st


def _step(log: list[dict[str, Any]], name: str, echo: Callable[[str], None] | None, **data: Any) -> None:
    log.append({"t": round(time.time(), 3), "step": name, **data})
    if echo:
        echo(f"[llm-demo] {name}: " + ", ".join(f"{k}={v}" for k, v in data.items() if k not in ("traffic", "gate")))


def _gate_view(rel: dict[str, Any]) -> dict[str, Any]:
    g = rel.get("gate_report") or {}
    return {
        "passed": g.get("passed"),
        "blocking_reasons": g.get("blocking_reasons"),
        "checks": [
            {k: c.get(k) for k in ("name", "passed", "observed", "limit", "blocking")} for c in g.get("checks", [])
        ],
    }


def canary_until(
    driver: Any,
    rid: str,
    run_id: str,
    seed: int,
    chunk: int,
    max_requests: int,
    stop: Callable,
    scenario: str = "baseline",
) -> dict[str, Any]:
    sent = 0
    total: dict[str, Any] = {"requests": 0, "versions": {}, "status": {}, "fallbacks": 0}
    while sent < max_requests:
        s = drive(driver, run_id, chunk, seed, scenario, start_index=sent)
        sent += chunk
        total["requests"] += s["requests"]
        total["fallbacks"] += s["fallbacks"]
        for key in ("versions", "status"):
            for k, v in s[key].items():
                total[key][k] = total[key].get(k, 0) + v
        if stop(driver.release(rid)):
            break
    return total


def llm_release_story(
    driver: Any,
    run_id: str,
    scale: float = 1.0,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """p2 promoted (shadow + canary + approval); p3 blocked offline on a segment its smoke check misses; the model
    swap blocked offline; a config-only change rolled back automatically by a canary guardrail."""
    n = lambda k: max(1, int(k * scale))  # noqa: E731
    steps: list[dict[str, Any]] = []
    expected: dict[str, Any] = {}
    actual: dict[str, Any] = {}
    observations: dict[str, Any] = {}

    # A: p1 -> p2 (better-or-equal): gate, shadow, canary, approval
    a = driver.propose(GOOD)
    _step(steps, f"propose {GOOD}", echo, release_id=a["release_id"], state=a["state"], gate=_gate_view(a))
    expected["A_gate"], actual["A_gate"] = "validated", a["state"]
    if a["state"] == "validated":
        driver.stage(a["release_id"])
        driver.shadow(a["release_id"])
        s = drive(driver, f"{run_id}-A-shadow", n(70), 101)
        _step(steps, "A shadow traffic", echo, release_id=a["release_id"], traffic=s)
        driver.canary(a["release_id"])
        s = canary_until(
            driver,
            a["release_id"],
            f"{run_id}-A-canary",
            102,
            n(100),
            n(1200),
            lambda r: r["state"] != "canary" or (r.get("last_evaluation") or {}).get("promotion_eligible"),
        )
        rel = driver.release(a["release_id"])
        _step(
            steps,
            "A canary traffic",
            echo,
            release_id=a["release_id"],
            state=rel["state"],
            traffic=s,
            windows=rel.get("windows_evaluated"),
            healthy=rel.get("healthy_windows"),
        )
        try:
            driver.approve(a["release_id"], actor="oncall-operator", role="operator")
            actual["self_approval_refused"] = False
        except DriverError as exc:
            actual["self_approval_refused"] = exc.status == 403
        try:
            rel = driver.approve(a["release_id"])
        except DriverError as exc:
            _step(steps, "A approve refused", echo, detail=exc.detail)
            rel = driver.release(a["release_id"])
        _step(steps, f"approve {GOOD}", echo, release_id=a["release_id"], state=rel["state"])
        actual["A"] = rel["state"]
    else:
        actual["A"] = a["state"]
    expected["A"] = "promoted"
    expected["self_approval_refused"] = True

    # B: p3 drops the account-takeover escalation rule -> blocked offline on the segment check
    b = driver.propose(REGRESSED)
    gv = _gate_view(b)
    smoke: dict[str, Any] = next((c for c in gv["checks"] if c["name"] == "smoke_subset"), {})
    _step(
        steps,
        f"propose {REGRESSED}",
        echo,
        release_id=b["release_id"],
        state=b["state"],
        gate=gv,
        blocking_reasons=gv["blocking_reasons"],
        smoke=smoke.get("observed"),
    )
    expected["B"], actual["B"] = "blocked", b["state"]
    expected["B_blocked_on_account_takeover_segment"] = True
    actual["B_blocked_on_account_takeover_segment"] = any(
        "segment_recall:account_takeover" in r for r in (gv["blocking_reasons"] or [])
    )
    # Observation, not a pass/fail expectation: did the 12-case textbook smoke check (relative to the champion) also
    # notice the regression? By design it should not; with a weak 3B model the dropped rule can leak elsewhere.
    observations["B_smoke_subset"] = {
        "candidate": smoke.get("observed"),
        "limit": smoke.get("limit"),
        "would_have_shipped": bool(smoke.get("passed")),
    }

    # C: model swap (same prompt and params on qwen2.5-coder:3b): the gate reports quality AND cost/latency
    c = driver.propose(SWAP)
    gc = _gate_view(c)
    _step(
        steps,
        f"propose {SWAP}",
        echo,
        release_id=c["release_id"],
        state=c["state"],
        gate=gc,
        blocking_reasons=gc["blocking_reasons"],
    )
    expected["C"], actual["C"] = "blocked", c["state"]
    # (a validated release is not active and moves no traffic; it is simply never staged in this story)

    # D: serving-config change (num_ctx 2048 -> 4096), same prompt and decoding as p2. Canary on baseline traffic;
    # if it is still in canary after that, a SIMULATED account-takeover campaign moves the alert mix outside the
    # validated envelope (input-drift trigger). Either guardrail breach rolls it back automatically.
    d = driver.propose(CTX)
    _step(steps, f"propose {CTX}", echo, release_id=d["release_id"], state=d["state"], gate=_gate_view(d))
    expected["D_gate"], actual["D_gate"] = "validated", d["state"]
    if d["state"] == "validated":
        driver.stage(d["release_id"])
        driver.shadow(d["release_id"])
        s = drive(driver, f"{run_id}-D-shadow", n(70), 401)
        _step(steps, "D shadow traffic", echo, release_id=d["release_id"], traffic=s)
        try:
            driver.canary(d["release_id"])
            s = canary_until(
                driver, d["release_id"], f"{run_id}-D-canary", 402, n(200), n(200), lambda r: r["state"] != "canary"
            )
            _step(
                steps,
                "D canary traffic (baseline)",
                echo,
                release_id=d["release_id"],
                traffic=s,
                state=driver.release(d["release_id"])["state"],
            )
            if driver.release(d["release_id"])["state"] == "canary":
                s = canary_until(
                    driver,
                    d["release_id"],
                    f"{run_id}-D-shift",
                    403,
                    n(100),
                    n(600),
                    lambda r: r["state"] != "canary",
                    scenario="ato_campaign",
                )
                _step(
                    steps,
                    "D canary traffic (SIMULATED account-takeover campaign)",
                    echo,
                    release_id=d["release_id"],
                    traffic=s,
                )
        except DriverError as exc:
            _step(steps, "D canary refused", echo, detail=exc.detail)
        rel = driver.release(d["release_id"])
        actual["D"] = rel["state"]
        actual["D_reason"] = rel.get("reason")
    else:
        actual["D"] = d["state"]
    expected["D"] = "rolled_back"
    dep = driver.deployment()
    expected["champion_after"], actual["champion_after"] = GOOD, dep["champion_version"]
    expected["mode_after"], actual["mode_after"] = "none", dep["mode"]
    post = drive(driver, f"{run_id}-post", n(30), 301)
    expected["post_rollback_versions"] = [GOOD]
    actual["post_rollback_versions"] = sorted(post["versions"])
    _step(steps, "post-release traffic", echo, traffic=post, versions=post["versions"])
    keys = [k for k in expected if k not in ("A_gate", "D_gate")]
    return {
        "scenario": "llm_release_story",
        "passed": all(actual.get(k) == expected[k] for k in keys),
        "expected": expected,
        "actual": actual,
        "observations": observations,
        "release_ids": {"A": a["release_id"], "B": b["release_id"], "C": c["release_id"], "D": d["release_id"]},
        "steps": steps,
    }
