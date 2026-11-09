"""Seeded synthetic fraud alerts for the LLM triage lane, with deterministic rule-derived labels.

An alert is a small set of fields (amount, channel, device age, password reset, ...) rendered into a short
English summary with seeded phrasing variants. The *label* (reason code + escalate) is computed from the
fields by ``label_alert``, a fixed decision table that is also written, in plain words, into the triage
prompt. Nobody hand-labels anything and no model labels anything: the gold set measures whether a prompt +
model follows a known policy, not whether it has good fraud judgement. That is the honest scope.

Decision table (``label_alert``), first match wins for the reason code:

1. ``CARD_TESTING``      3 or more authorizations under $2 in the past hour
2. ``ACCOUNT_TAKEOVER``  password reset < 24 h before the purchase AND device first seen < 24 h before it
3. ``STOLEN_CARD_CNP``   card not present AND (device first seen < 24 h OR ship-to address added < 24 h)
4. ``FRIENDLY_FRAUD``    2 or more prior chargebacks AND a known device (first seen >= 24 h)
5. ``BENIGN_ANOMALY``    otherwise

Escalate when any rule fires (the rule names double as evaluation *segments*):

- ``high_value_cnp``      card not present AND amount >= $1,000
- ``account_takeover``    reason code is ACCOUNT_TAKEOVER
- ``card_testing_burst``  reason code is CARD_TESTING AND 3 or more declines in the past hour
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REASON_CODES = ("CARD_TESTING", "ACCOUNT_TAKEOVER", "STOLEN_CARD_CNP", "FRIENDLY_FRAUD", "BENIGN_ANOMALY")
SEGMENTS = ("high_value_cnp", "account_takeover", "card_testing_burst")
MERCHANTS = ("electronics", "grocery", "travel", "gift_cards", "digital_goods", "restaurants", "fuel", "apparel")
ARCHETYPES = ("card_testing", "account_takeover", "stolen_card_cnp", "friendly_fraud", "benign_anomaly")
GENERATOR_VERSION = "triage-alerts-v1"
HIGH_VALUE = 1000.0


@dataclass(frozen=True)
class Alert:
    alert_id: str
    amount: float
    channel: str  # "card_present" | "card_not_present"
    merchant: str
    device_age_hours: float
    password_reset_hours_ago: float | None
    ship_to_added_hours_ago: float | None
    small_auths_1h: int
    declines_1h: int
    prior_chargebacks: int
    tenure_months: int
    local_hour: int

    def fields(self) -> dict[str, Any]:
        return asdict(self)


def label_alert(a: Alert) -> dict[str, Any]:
    """The decision table from the module docstring. Pure function of the fields."""
    new_device = a.device_age_hours < 24
    recent_reset = a.password_reset_hours_ago is not None and a.password_reset_hours_ago < 24
    new_ship_to = a.ship_to_added_hours_ago is not None and a.ship_to_added_hours_ago < 24
    cnp = a.channel == "card_not_present"
    if a.small_auths_1h >= 3:
        reason = "CARD_TESTING"
    elif recent_reset and new_device:
        reason = "ACCOUNT_TAKEOVER"
    elif cnp and (new_device or new_ship_to):
        reason = "STOLEN_CARD_CNP"
    elif a.prior_chargebacks >= 2 and not new_device:
        reason = "FRIENDLY_FRAUD"
    else:
        reason = "BENIGN_ANOMALY"
    segments = []
    if cnp and a.amount >= HIGH_VALUE:
        segments.append("high_value_cnp")
    if reason == "ACCOUNT_TAKEOVER":
        segments.append("account_takeover")
    if reason == "CARD_TESTING" and a.declines_1h >= 3:
        segments.append("card_testing_burst")
    return {"reason_code": reason, "escalate": bool(segments), "segments": segments}


# -- sampling -----------------------------------------------------------------------------------------------
def _amount(rng: random.Random, lo: float, hi: float) -> float:
    # log-uniform, rounded to cents; a share lands near the $1,000 boundary on purpose
    if rng.random() < 0.15:
        return round(rng.uniform(900, 1100), 2)
    import math

    return round(math.exp(rng.uniform(math.log(lo), math.log(hi))), 2)


def _known_device(rng: random.Random) -> float:
    return float(rng.choice([rng.uniform(24 * 3, 24 * 900), rng.uniform(24, 48)]))


def sample_alert(rng: random.Random, archetype: str, alert_id: str) -> Alert:
    """Draw fields that *lean* toward an archetype. The label still comes from ``label_alert``."""
    merchant = rng.choice(MERCHANTS)
    tenure = rng.randint(1, 180)
    hour = rng.randint(0, 23)
    reset: float | None = None
    ship: float | None = None
    small, declines, chargebacks = 0, rng.choice([0, 0, 0, 1, 2]), rng.choice([0, 0, 0, 1])
    if archetype == "card_testing":
        channel = "card_not_present" if rng.random() < 0.85 else "card_present"
        amount = round(rng.uniform(1, 60), 2) if rng.random() < 0.8 else _amount(rng, 60, 2500)
        small = rng.randint(3, 12) if rng.random() < 0.9 else 2
        declines = rng.randint(0, 8)
        device = rng.uniform(0.05, 20) if rng.random() < 0.6 else _known_device(rng)
        merchant = rng.choice(["digital_goods", "gift_cards", "apparel", merchant])
    elif archetype == "account_takeover":
        channel = "card_not_present" if rng.random() < 0.8 else "card_present"
        amount = _amount(rng, 40, 3000)
        reset = rng.uniform(0.2, 23.5) if rng.random() < 0.88 else rng.uniform(25, 72)
        device = rng.uniform(0.05, 23.5) if rng.random() < 0.9 else rng.uniform(25, 60)
        ship = rng.uniform(0.5, 20) if rng.random() < 0.4 else None
    elif archetype == "stolen_card_cnp":
        channel = "card_not_present" if rng.random() < 0.92 else "card_present"
        amount = _amount(rng, 25, 3500)
        if rng.random() < 0.6:
            device = rng.uniform(0.05, 23.5)
        else:
            device = _known_device(rng)
            ship = rng.uniform(0.5, 23.5) if rng.random() < 0.85 else rng.uniform(30, 300)
        if rng.random() < 0.15:
            reset = rng.uniform(26, 24 * 20)  # an old reset: should NOT read as account takeover
    elif archetype == "friendly_fraud":
        channel = "card_not_present" if rng.random() < 0.6 else "card_present"
        amount = _amount(rng, 20, 2500)
        chargebacks = rng.randint(2, 6) if rng.random() < 0.9 else 1
        device = _known_device(rng)
    else:  # benign_anomaly: unusual but explainable (travel, late hour, big in-store purchase)
        channel = "card_not_present" if rng.random() < 0.45 else "card_present"
        amount = _amount(rng, 5, 3000)
        device = _known_device(rng)
        hour = rng.choice([1, 2, 3, 4, hour])
        merchant = rng.choice(["travel", "restaurants", "fuel", merchant])
        if rng.random() < 0.2:
            reset = rng.uniform(30, 24 * 25)
    return Alert(
        alert_id=alert_id,
        amount=amount,
        channel=channel,
        merchant=merchant,
        device_age_hours=round(device, 2),
        password_reset_hours_ago=None if reset is None else round(reset, 2),
        ship_to_added_hours_ago=None if ship is None else round(ship, 2),
        small_auths_1h=small,
        declines_1h=declines,
        prior_chargebacks=chargebacks,
        tenure_months=tenure,
        local_hour=hour,
    )


# -- rendering ----------------------------------------------------------------------------------------------
def _ago(hours: float) -> str:
    if hours < 1:
        return f"{max(1, round(hours * 60))} minutes"
    if hours < 48:
        return f"{hours:.1f} hours".replace(".0 hours", " hours")
    return f"{round(hours / 24)} days"


def render_summary(a: Alert, rng: random.Random) -> str:
    """Seeded English summary of the alert. Every decision-relevant fact is stated explicitly."""
    chan = (
        rng.choice(["card-not-present (online) purchase", "online purchase, card not present"])
        if a.channel == "card_not_present"
        else rng.choice(["in-store purchase, card present", "card-present purchase at a terminal"])
    )
    head = f"Alert {a.alert_id}: ${a.amount:,.2f} {chan} at a {a.merchant.replace('_', ' ')} merchant."
    facts = []
    if a.device_age_hours < 24:
        facts.append(
            rng.choice(
                [
                    f"New device, first seen {_ago(a.device_age_hours)} before the purchase.",
                    f"Device first seen {_ago(a.device_age_hours)} before the purchase.",
                ]
            )
        )
    else:
        facts.append(
            rng.choice(
                [
                    f"Known device, first seen {_ago(a.device_age_hours)} before the purchase.",
                    f"Device in use for {_ago(a.device_age_hours)}.",
                ]
            )
        )
    if a.password_reset_hours_ago is None:
        facts.append(rng.choice(["No password reset in the last 30 days.", "Password unchanged for 30+ days."]))
    else:
        facts.append(f"Password reset {_ago(a.password_reset_hours_ago)} before the purchase.")
    if a.channel == "card_not_present":
        if a.ship_to_added_hours_ago is None:
            facts.append("Ship-to address unchanged.")
        else:
            facts.append(f"Ship-to address added {_ago(a.ship_to_added_hours_ago)} before the purchase.")
    facts.append(
        f"{a.small_auths_1h} authorizations under $2 in the past hour."
        if a.small_auths_1h
        else rng.choice(["No micro-authorizations in the past hour.", "0 authorizations under $2 in the past hour."])
    )
    facts.append(f"{a.declines_1h} declined attempt{'s' if a.declines_1h != 1 else ''} in the past hour.")
    facts.append(
        f"Cardholder has filed {a.prior_chargebacks} chargeback{'s' if a.prior_chargebacks != 1 else ''} "
        "in the past year."
        if a.prior_chargebacks
        else "No prior chargebacks."
    )
    rng.shuffle(facts)
    tail = f"Customer tenure {a.tenure_months} months; local time {a.local_hour:02d}:00."
    return " ".join([head, *facts, tail])


# -- gold set -----------------------------------------------------------------------------------------------
GOLD_COMPOSITION = {  # cases per archetype; fixed so every segment has support
    "dev": {
        "card_testing": 12,
        "account_takeover": 14,
        "stolen_card_cnp": 14,
        "friendly_fraud": 10,
        "benign_anomaly": 10,
    },
    "test": {
        "card_testing": 28,
        "account_takeover": 40,
        "stolen_card_cnp": 36,
        "friendly_fraud": 22,
        "benign_anomaly": 24,
    },
}
GOLD_SEED = 20261109
SMOKE_SIZE = 12


def _case(a: Alert, rng: random.Random, split: str, archetype: str) -> dict[str, Any]:
    lab = label_alert(a)
    return {
        "case_id": a.alert_id,
        "split": split,
        "archetype": archetype,
        "summary": render_summary(a, rng),
        "fields": a.fields(),
        "expected": {"reason_code": lab["reason_code"], "escalate": lab["escalate"]},
        "segments": lab["segments"],
        "smoke": False,
        "labeler": "rule:label_alert (deterministic; not human, not AI)",
    }


def build_gold(seed: int = GOLD_SEED) -> list[dict[str, Any]]:
    """Dev split (prompt development) + test split (release gate). Same seed -> byte-identical output."""
    rng = random.Random(seed)
    cases: list[dict[str, Any]] = []
    for split, comp in GOLD_COMPOSITION.items():
        n = 0
        for archetype, count in comp.items():
            for _ in range(count):
                n += 1
                aid = f"G{'D' if split == 'dev' else 'T'}-{n:04d}"
                cases.append(_case(sample_alert(rng, archetype, aid), rng, split, archetype))
    _mark_smoke(cases)
    return cases


def _mark_smoke(cases: list[dict[str, Any]]) -> None:
    """The smoke set is deliberately *textbook*: per reason code, the first test cases that look like the
    canonical example. Its account-takeover examples are high-value online purchases, which escalate under
    the dollar rule anyway, so a prompt that forgets the account-takeover escalation rule still passes it.
    That is the point: it reproduces how a small hand-picked smoke check misses a segment."""
    test = [c for c in cases if c["split"] == "test"]
    picks: list[dict[str, Any]] = []
    canonical = {
        "CARD_TESTING": lambda c: c["fields"]["small_auths_1h"] >= 5,
        "ACCOUNT_TAKEOVER": lambda c: "high_value_cnp" in c["segments"],
        "STOLEN_CARD_CNP": lambda c: c["fields"]["device_age_hours"] < 2,
        "FRIENDLY_FRAUD": lambda c: c["fields"]["prior_chargebacks"] >= 3,
        "BENIGN_ANOMALY": lambda c: c["fields"]["channel"] == "card_present",
    }
    per = {"CARD_TESTING": 2, "ACCOUNT_TAKEOVER": 3, "STOLEN_CARD_CNP": 3, "FRIENDLY_FRAUD": 2, "BENIGN_ANOMALY": 2}
    for code, k in per.items():
        picks += [c for c in test if c["expected"]["reason_code"] == code and canonical[code](c)][:k]
    for c in picks[:SMOKE_SIZE]:
        c["smoke"] = True


def gold_sha256(cases: list[dict[str, Any]]) -> str:
    return hashlib.sha256("\n".join(json.dumps(c, sort_keys=True) for c in cases).encode()).hexdigest()


def write_gold(path: Path, cases: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(c, sort_keys=True) + "\n" for c in cases))
    return gold_sha256(cases)


def load_gold(path: Path, split: str | None = None) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    return [c for c in cases if split is None or c["split"] == split]


# -- simulated traffic --------------------------------------------------------------------------------------
TRAFFIC_MIX = {  # baseline alert mix in the simulated queue
    "baseline": {
        "benign_anomaly": 0.34,
        "stolen_card_cnp": 0.22,
        "card_testing": 0.14,
        "friendly_fraud": 0.15,
        "account_takeover": 0.15,
    },
    # SIMULATED account-takeover campaign: many low-value ATO alerts (drift scenario)
    "ato_campaign": {
        "benign_anomaly": 0.15,
        "stolen_card_cnp": 0.15,
        "card_testing": 0.10,
        "friendly_fraud": 0.10,
        "account_takeover": 0.50,
    },
}


@dataclass
class TriageRequest:
    index: int
    payload: dict[str, Any]
    expected: dict[str, Any]
    segments: list[str]
    label_arrives: bool


def iter_traffic(
    run_id: str,
    n: int,
    seed: int,
    scenario: str = "baseline",
    customers: int = 4000,
    label_missing_rate: float = 0.1,
    start_index: int = 0,
) -> Iterator[TriageRequest]:
    """SIMULATED alert queue: payloads carry ``context.simulated = true``; ids start with ``sim-``."""
    rng = random.Random(f"{seed}:{scenario}:{start_index}")
    mix = TRAFFIC_MIX[scenario]
    names, weights = list(mix), list(mix.values())
    for i in range(start_index, start_index + n):
        arch = rng.choices(names, weights)[0]
        a = sample_alert(rng, arch, f"ALR-{seed % 1000:03d}{i:05d}")
        lab = label_alert(a)
        yield TriageRequest(
            index=i,
            payload={
                "request_id": f"sim-{run_id}-{i:06d}",
                "request_key": f"cust-{rng.randrange(customers):05d}",
                "alert": {"summary": render_summary(a, rng), "fields": a.fields()},
                "context": {"simulated": True, "scenario": scenario},
            },
            expected={"reason_code": lab["reason_code"], "escalate": lab["escalate"]},
            segments=lab["segments"],
            label_arrives=rng.random() >= label_missing_rate,
        )
