"""Seeded SIMULATED traffic: realistic JSON payloads plus delayed, partially missing ground-truth labels.

Every payload carries ``context.simulated = true`` and the scenario name, every label is sent with
``simulated = true``, and request ids start with ``sim-``. The model predictions these requests receive
are real; the traffic is not from real users.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .data import generate
from .schema import decode_row


@dataclass(frozen=True)
class Phase:
    scenario: str
    requests: int
    seed: int
    label: str = ""


@dataclass
class TrafficPlan:
    run_id: str
    phases: list[Phase]
    card_pool: int = 6000
    label_delay: int = 120  # label for request i is delivered after request i + delay
    label_missing_rate: float = 0.2  # share of labels that never arrive
    seed: int = 1234
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SimRequest:
    index: int
    phase: str
    payload: dict[str, Any]
    label: int
    label_arrives: bool


def iter_requests(plan: TrafficPlan) -> Iterator[SimRequest]:
    i = 0
    key_rng = np.random.default_rng(plan.seed)
    for ph in plan.phases:
        X, y = generate(ph.requests, ph.seed, ph.scenario)
        keys = key_rng.integers(0, plan.card_pool, ph.requests)
        arrive = key_rng.random(ph.requests) >= plan.label_missing_rate
        for j in range(ph.requests):
            yield SimRequest(
                index=i,
                phase=ph.label or ph.scenario,
                payload={
                    "request_id": f"sim-{plan.run_id}-{i:06d}",
                    "request_key": f"card-{int(keys[j]):05d}",
                    "features": decode_row(X[j]),
                    "context": {"simulated": True, "scenario": ph.scenario, "phase": ph.label or ph.scenario},
                },
                label=int(y[j]),
                label_arrives=bool(arrive[j]),
            )
            i += 1


def drive(
    plan: TrafficPlan, send_predict: Any, send_feedback: Any, on_phase: Any = None, after_request: Any = None
) -> dict[str, Any]:
    """Run the plan through caller-supplied senders (in-process service or HTTP client)."""
    pending: list[tuple[int, str, int]] = []
    stats: dict[str, Any] = {"requests": 0, "labels_sent": 0, "labels_withheld": 0, "by_phase": {}}
    current = None
    for req in iter_requests(plan):
        if req.phase != current:
            current = req.phase
            if on_phase:
                on_phase(current, req.index)
        status, body = send_predict(req.payload)
        ph = stats["by_phase"].setdefault(req.phase, {"requests": 0, "status": {}, "versions": {}})
        ph["requests"] += 1
        ph["status"][str(status)] = ph["status"].get(str(status), 0) + 1
        if status == 200:
            v = body["model_version"]
            ph["versions"][v] = ph["versions"].get(v, 0) + 1
        stats["requests"] += 1
        if req.label_arrives and status == 200:
            pending.append((req.index + plan.label_delay, req.payload["request_id"], req.label))
        elif status == 200:
            stats["labels_withheld"] += 1
        while pending and pending[0][0] <= req.index:
            _, rid, lab = pending.pop(0)
            send_feedback({"request_id": rid, "label": lab, "simulated": True})
            stats["labels_sent"] += 1
        if after_request:
            after_request(req, status, body)
    for _, rid, lab in pending:
        send_feedback({"request_id": rid, "label": lab, "simulated": True})
        stats["labels_sent"] += 1
    return stats
