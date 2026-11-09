"""SQLite-backed registry, deployment pointer, release state, append-only event log and telemetry.

Guarantees enforced by the database itself (not just by Python code):
- ``model_versions`` rows are immutable (UPDATE/DELETE triggers abort).
- ``events`` rows are append-only and hash-chained (each row hashes its predecessor), so editing or
  deleting history is detectable by ``verify_event_chain``.
- The deployment pointer changes only through ``swap_deployment``, a compare-and-swap on ``generation``
  inside one transaction together with its audit event. Two racing promotions cannot both win.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS model_versions (
  version TEXT PRIMARY KEY,
  model_name TEXT NOT NULL,
  created_at REAL NOT NULL,
  artifact_path TEXT NOT NULL,
  artifact_sha256 TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  signature TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS model_versions_no_update BEFORE UPDATE ON model_versions
BEGIN SELECT RAISE(ABORT, 'model_versions rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_versions_no_delete BEFORE DELETE ON model_versions
BEGIN SELECT RAISE(ABORT, 'model_versions rows are immutable'); END;

CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  run_id TEXT,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  simulated INTEGER NOT NULL DEFAULT 0,
  release_id TEXT,
  payload_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE TABLE IF NOT EXISTS deployment (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  champion_version TEXT,
  candidate_version TEXT,
  mode TEXT NOT NULL DEFAULT 'none',
  canary_fraction REAL NOT NULL DEFAULT 0,
  release_id TEXT,
  last_known_good TEXT,
  generation INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
  release_id TEXT PRIMARY KEY,
  candidate_version TEXT NOT NULL,
  champion_version TEXT,
  state TEXT NOT NULL,
  reason TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  state_entered_at REAL NOT NULL,
  heartbeat_at REAL NOT NULL,
  window_start_seq INTEGER NOT NULL DEFAULT 0,
  canary_start_seq INTEGER,
  last_eval_seq INTEGER,
  windows_evaluated INTEGER NOT NULL DEFAULT 0,
  healthy_windows INTEGER NOT NULL DEFAULT 0,
  last_evaluation_json TEXT,
  shadow_summary_json TEXT,
  created_by TEXT,
  gate_report_json TEXT,
  approvals_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS requests (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  request_key TEXT NOT NULL,
  release_id TEXT,
  generation INTEGER NOT NULL,
  arm TEXT NOT NULL,
  served_version TEXT,
  score REAL,
  decision INTEGER,
  latency_ms REAL NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  shadow_version TEXT,
  shadow_score REAL,
  shadow_decision INTEGER,
  shadow_latency_ms REAL,
  shadow_status TEXT,
  champion_score REAL,
  champion_decision INTEGER,
  champion_latency_ms REAL,
  features_json TEXT,
  simulated INTEGER NOT NULL DEFAULT 0,
  scenario TEXT,
  fallback TEXT
);
CREATE INDEX IF NOT EXISTS requests_release ON requests(release_id, seq);

CREATE TABLE IF NOT EXISTS feedback (
  request_id TEXT PRIMARY KEY,
  label INTEGER NOT NULL,
  ts REAL NOT NULL,
  simulated INTEGER NOT NULL DEFAULT 0
);
"""

GENESIS = "0" * 64


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def event_hash(
    prev_hash: str,
    kind: str,
    actor: str,
    simulated: bool,
    release_id: str | None,
    payload: dict[str, Any],
    run_id: str | None,
) -> str:
    body = _canonical(
        {
            "prev": prev_hash,
            "kind": kind,
            "actor": actor,
            "simulated": bool(simulated),
            "release_id": release_id,
            "payload": payload,
            "run_id": run_id,
        }
    )
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class Deployment:
    champion_version: str | None
    candidate_version: str | None
    mode: str
    canary_fraction: float
    release_id: str | None
    last_known_good: str | None
    generation: int
    updated_at: float

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class ConflictError(RuntimeError):
    """Compare-and-swap lost: someone else changed the deployment first."""


class Store:
    """Thread-safe facade. One connection per thread; writes are serialized by a process lock."""

    def __init__(self, path: Path, clock: Any = time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self.clock = clock
        self.run_id: str | None = None
        with self._conn() as c:
            c.executescript(SCHEMA_SQL)
            if c.execute("SELECT COUNT(*) FROM deployment").fetchone()[0] == 0:
                c.execute("INSERT INTO deployment (id, mode, updated_at) VALUES (1, 'none', ?)", (self.clock(),))

    # -- connections ----------------------------------------------------------------------------------------
    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        yield self._connection()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            c = self._connection()
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- events ---------------------------------------------------------------------------------------------
    def append_event(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        actor: str = "system",
        simulated: bool = False,
        release_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        if conn is None:
            with self.transaction() as c:
                return self.append_event(kind, payload, actor=actor, simulated=simulated, release_id=release_id, conn=c)
        row = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS
        payload = json.loads(_canonical(payload))
        h = event_hash(prev, kind, actor, simulated, release_id, payload, self.run_id)
        cur = conn.execute(
            "INSERT INTO events (ts, run_id, kind, actor, simulated, release_id, payload_json, prev_hash, hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self.clock(),
                self.run_id,
                kind,
                actor,
                int(simulated),
                release_id,
                _canonical(payload),
                prev,
                h,
            ),
        )
        return int(cur.lastrowid or 0)

    def events(self, after: int = 0, limit: int = 1000, release_id: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM events WHERE seq > ?"
        args: list[Any] = [after]
        if release_id:
            q += " AND release_id = ?"
            args.append(release_id)
        q += " ORDER BY seq LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        return [self._event_row(r) for r in rows]

    @staticmethod
    def _event_row(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": r["seq"],
            "ts": r["ts"],
            "run_id": r["run_id"],
            "kind": r["kind"],
            "actor": r["actor"],
            "simulated": bool(r["simulated"]),
            "release_id": r["release_id"],
            "payload": json.loads(r["payload_json"]),
            "hash": r["hash"],
            "prev_hash": r["prev_hash"],
        }

    def verify_event_chain(self) -> dict[str, Any]:
        prev = GENESIS
        n = 0
        with self._conn() as c:
            for r in c.execute("SELECT * FROM events ORDER BY seq"):
                expect = event_hash(
                    prev,
                    r["kind"],
                    r["actor"],
                    bool(r["simulated"]),
                    r["release_id"],
                    json.loads(r["payload_json"]),
                    r["run_id"],
                )
                if r["prev_hash"] != prev or r["hash"] != expect:
                    return {"ok": False, "broken_at_seq": r["seq"], "checked": n}
                prev = r["hash"]
                n += 1
        return {"ok": True, "checked": n, "head": prev}

    # -- registry -------------------------------------------------------------------------------------------
    def insert_model(self, rec: dict[str, Any], actor: str) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO model_versions (version, model_name, created_at, artifact_path, artifact_sha256,"
                " manifest_json, manifest_sha256, signature) VALUES (?,?,?,?,?,?,?,?)",
                (
                    rec["version"],
                    rec["model_name"],
                    self.clock(),
                    rec["artifact_path"],
                    rec["artifact_sha256"],
                    _canonical(rec["manifest"]),
                    rec["manifest_sha256"],
                    rec["signature"],
                ),
            )
            self.append_event(
                "model.registered",
                {
                    "version": rec["version"],
                    "model_name": rec["model_name"],
                    "artifact_sha256": rec["artifact_sha256"],
                    "manifest_sha256": rec["manifest_sha256"],
                },
                actor=actor,
                conn=c,
            )

    def get_model(self, version: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM model_versions WHERE version = ?", (version,)).fetchone()
        return self._model_row(r) if r else None

    def list_models(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM model_versions ORDER BY created_at").fetchall()
        return [self._model_row(r) for r in rows]

    @staticmethod
    def _model_row(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "version": r["version"],
            "model_name": r["model_name"],
            "created_at": r["created_at"],
            "artifact_path": r["artifact_path"],
            "artifact_sha256": r["artifact_sha256"],
            "manifest": json.loads(r["manifest_json"]),
            "manifest_sha256": r["manifest_sha256"],
            "signature": r["signature"],
        }

    # -- deployment pointer ---------------------------------------------------------------------------------
    def deployment(self) -> Deployment:
        with self._conn() as c:
            r = c.execute("SELECT * FROM deployment WHERE id = 1").fetchone()
        return Deployment(
            r["champion_version"],
            r["candidate_version"],
            r["mode"],
            float(r["canary_fraction"]),
            r["release_id"],
            r["last_known_good"],
            int(r["generation"]),
            float(r["updated_at"]),
        )

    def swap_deployment(
        self,
        expected_generation: int,
        *,
        event_kind: str,
        event_payload: dict[str, Any],
        actor: str,
        release_id: str | None = None,
        simulated: bool = False,
        release_update: dict[str, Any] | None = None,
        **fields: Any,
    ) -> Deployment:
        """Atomically change the pointer (CAS on generation) and record why, in one transaction."""
        if "dep_release_id" in fields:
            fields["release_id"] = fields.pop("dep_release_id")
        allowed = {
            "champion_version",
            "candidate_version",
            "mode",
            "canary_fraction",
            "release_id",
            "last_known_good",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown deployment fields {bad}")
        with self.transaction() as c:
            cur = c.execute("SELECT generation FROM deployment WHERE id = 1").fetchone()["generation"]
            if cur != expected_generation:
                raise ConflictError(f"deployment generation is {cur}, expected {expected_generation}")
            sets = ", ".join(f"{k} = ?" for k in fields)
            args = [*fields.values(), self.clock()]
            c.execute(
                f"UPDATE deployment SET {sets}{', ' if sets else ''}generation = generation + 1,"
                " updated_at = ? WHERE id = 1",
                args,
            )
            if release_update and release_id:
                self._update_release(c, release_id, **release_update)
            payload = dict(event_payload)
            payload["generation"] = expected_generation + 1
            self.append_event(event_kind, payload, actor=actor, simulated=simulated, release_id=release_id, conn=c)
        return self.deployment()

    # -- releases -------------------------------------------------------------------------------------------
    def create_release(
        self,
        release_id: str,
        candidate: str,
        champion: str | None,
        state: str,
        actor: str,
        reason: str | None = None,
    ) -> None:
        now = self.clock()
        with self.transaction() as c:
            c.execute(
                "INSERT INTO releases (release_id, candidate_version, champion_version, state, reason, created_at,"
                " updated_at, state_entered_at, heartbeat_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (release_id, candidate, champion, state, reason, now, now, now, now, actor),
            )
            self.append_event(
                "release.created",
                {"candidate": candidate, "champion": champion, "state": state},
                actor=actor,
                release_id=release_id,
                conn=c,
            )

    def _update_release(self, c: sqlite3.Connection, release_id: str, **fields: Any) -> None:
        now = self.clock()
        if "state" in fields:
            fields["state_entered_at"] = now
            fields.setdefault("heartbeat_at", now)
        if "gate_report" in fields:
            fields["gate_report_json"] = _canonical(fields.pop("gate_report"))
        if "approvals" in fields:
            fields["approvals_json"] = _canonical(fields.pop("approvals"))
        if "last_evaluation" in fields:
            fields["last_evaluation_json"] = _canonical(fields.pop("last_evaluation"))
        if "shadow_summary" in fields:
            fields["shadow_summary_json"] = _canonical(fields.pop("shadow_summary"))
        fields["updated_at"] = now
        sets = ", ".join(f"{k} = ?" for k in fields)
        c.execute(f"UPDATE releases SET {sets} WHERE release_id = ?", [*fields.values(), release_id])

    def update_release(
        self,
        release_id: str,
        *,
        event_kind: str | None = None,
        event_payload: dict[str, Any] | None = None,
        actor: str = "system",
        simulated: bool = False,
        **fields: Any,
    ) -> None:
        with self.transaction() as c:
            self._update_release(c, release_id, **fields)
            if event_kind:
                self.append_event(
                    event_kind,
                    event_payload or {},
                    actor=actor,
                    simulated=simulated,
                    release_id=release_id,
                    conn=c,
                )

    def heartbeat(self, release_id: str) -> None:
        with self.transaction() as c:
            c.execute("UPDATE releases SET heartbeat_at = ? WHERE release_id = ?", (self.clock(), release_id))

    def get_release(self, release_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM releases WHERE release_id = ?", (release_id,)).fetchone()
        return self._release_row(r) if r else None

    def list_releases(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM releases ORDER BY created_at").fetchall()
        return [self._release_row(r) for r in rows]

    @staticmethod
    def _release_row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["gate_report"] = json.loads(d.pop("gate_report_json") or "null")
        d["approvals"] = json.loads(d.pop("approvals_json") or "[]")
        d["last_evaluation"] = json.loads(d.pop("last_evaluation_json") or "null")
        d["shadow_summary"] = json.loads(d.pop("shadow_summary_json") or "null")
        return d

    # -- request telemetry ----------------------------------------------------------------------------------
    def insert_requests(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cols = [
            "ts",
            "request_id",
            "request_key",
            "release_id",
            "generation",
            "arm",
            "served_version",
            "score",
            "decision",
            "latency_ms",
            "status",
            "error",
            "shadow_version",
            "shadow_score",
            "shadow_decision",
            "shadow_latency_ms",
            "shadow_status",
            "champion_score",
            "champion_decision",
            "champion_latency_ms",
            "features_json",
            "simulated",
            "scenario",
            "fallback",
        ]
        q = f"INSERT OR IGNORE INTO requests ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        with self.transaction() as c:
            c.executemany(q, [[r.get(k) for k in cols] for r in rows])

    def insert_feedback(self, request_id: str, label: int, simulated: bool) -> bool:
        with self.transaction() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO feedback (request_id, label, ts, simulated) VALUES (?,?,?,?)",
                (request_id, int(label), self.clock(), int(simulated)),
            )
            return cur.rowcount == 1

    def max_request_seq(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COALESCE(MAX(seq), 0) FROM requests").fetchone()[0])

    def requests_since(
        self, start_seq: int, release_id: str | None = None, with_labels: bool = True
    ) -> list[dict[str, Any]]:
        q = (
            "SELECT r.*, f.label AS label FROM requests r LEFT JOIN feedback f ON f.request_id = r.request_id"
            " WHERE r.seq > ?"
        )
        args: list[Any] = [start_seq]
        if release_id is not None:
            q += " AND r.release_id = ?"
            args.append(release_id)
        q += " ORDER BY r.seq"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def recent_requests(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT r.seq, r.ts, r.request_id, r.arm, r.served_version, r.score, r.decision, r.latency_ms,"
                " r.status, r.error, r.shadow_version, r.shadow_score, r.simulated, r.scenario, r.fallback,"
                " f.label AS label FROM requests r LEFT JOIN feedback f ON f.request_id = r.request_id"
                " ORDER BY r.seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
