"""Registry immutability, event-chain tamper evidence, CAS pointer swaps, deterministic canary routing."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from spectral_canary.serving import bucket, routes_to_candidate
from spectral_canary.store import ConflictError, Store


def test_model_rows_and_events_are_immutable(rt) -> None:
    conn = sqlite3.connect(rt.db_path)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE model_versions SET artifact_sha256 = 'x' WHERE version = 'v1-logreg'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM model_versions")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET kind = 'forged' WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")
    conn.close()


def test_event_chain_detects_tampering(tmp_path: Path) -> None:
    s = Store(tmp_path / "r.sqlite")
    for i in range(5):
        s.append_event("test.event", {"i": i})
    assert s.verify_event_chain() == {"ok": True, "checked": 5, "head": s.events()[-1]["hash"]}
    conn = sqlite3.connect(tmp_path / "r.sqlite")
    conn.execute("DROP TRIGGER events_no_update")  # an attacker with file access can drop triggers…
    conn.execute("UPDATE events SET payload_json = '{\"i\":99}' WHERE seq = 3")
    conn.commit()
    conn.close()
    res = s.verify_event_chain()  # …but cannot forge the hash chain without detection
    assert res["ok"] is False and res["broken_at_seq"] == 3


def test_pointer_swap_is_compare_and_swap(tmp_path: Path) -> None:
    s = Store(tmp_path / "r.sqlite")
    g = s.deployment().generation
    s.swap_deployment(g, event_kind="t", event_payload={}, actor="a", champion_version="v1")
    with pytest.raises(ConflictError):
        s.swap_deployment(g, event_kind="t", event_payload={}, actor="b", champion_version="v2")
    assert s.deployment().champion_version == "v1"


def test_concurrent_swaps_exactly_one_wins(tmp_path: Path) -> None:
    s = Store(tmp_path / "r.sqlite")
    g = s.deployment().generation
    wins, losses = [], []

    def go(v: str) -> None:
        try:
            s.swap_deployment(g, event_kind="t", event_payload={"v": v}, actor=v, champion_version=v)
            wins.append(v)
        except ConflictError:
            losses.append(v)

    ts = [threading.Thread(target=go, args=(f"v{i}",)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(wins) == 1 and len(losses) == 7
    assert s.deployment().champion_version == wins[0]
    assert s.deployment().generation == g + 1


def test_routing_fraction_on_fixed_seeded_keys() -> None:
    keys = [f"card-{i:05d}" for i in range(20_000)]
    share = sum(routes_to_candidate(k, 0.10) for k in keys) / len(keys)
    assert abs(share - 0.10) < 0.01, share
    assert sum(routes_to_candidate(k, 0.0) for k in keys) == 0
    assert all(routes_to_candidate(k, 1.0) for k in keys)


def test_routing_is_sticky_per_key() -> None:
    assert all(bucket("card-00042") == bucket("card-00042") for _ in range(100))
    # a key in the 10% canary stays in it when the fraction grows (monotone ramp)
    k = next(f"card-{i}" for i in range(1000) if routes_to_candidate(f"card-{i}", 0.10))
    assert routes_to_candidate(k, 0.25) and routes_to_candidate(k, 0.5)
