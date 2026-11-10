"""The LLM lane's store: the same ``Store`` (immutable registry rows, hash-chained events, CAS deployment pointer,
release state machine rows) plus two lane-specific tables for triage telemetry and delayed analyst labels.

The lane uses its own SQLite file, so the ML lane's pointer and the LLM lane's pointer can never collide, while
every guarantee in ``store.py`` (triggers, chain, CAS) applies unchanged. The controller reads request rows only
through ``max_request_seq`` / ``requests_since`` / ``insert_requests``, which this class re-points at
``llm_requests``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..store import Store

LLM_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_requests (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  request_key TEXT NOT NULL,
  release_id TEXT,
  generation INTEGER NOT NULL,
  arm TEXT NOT NULL,
  served_version TEXT,
  status TEXT NOT NULL,
  error TEXT,
  fallback TEXT,
  latency_ms REAL NOT NULL,
  simulated INTEGER NOT NULL DEFAULT 0,
  scenario TEXT,
  alert_json TEXT,
  served_json TEXT,
  champion_json TEXT,
  candidate_json TEXT
);
CREATE INDEX IF NOT EXISTS llm_requests_release ON llm_requests(release_id, seq);
CREATE TABLE IF NOT EXISTS llm_feedback (
  request_id TEXT PRIMARY KEY,
  reason_code TEXT NOT NULL,
  escalate INTEGER NOT NULL,
  ts REAL NOT NULL,
  simulated INTEGER NOT NULL DEFAULT 0
);
"""

_COLS = (
    "ts",
    "request_id",
    "request_key",
    "release_id",
    "generation",
    "arm",
    "served_version",
    "status",
    "error",
    "fallback",
    "latency_ms",
    "simulated",
    "scenario",
    "alert_json",
    "served_json",
    "champion_json",
    "candidate_json",
)
_JSON = ("alert_json", "served_json", "champion_json", "candidate_json")


def _cell(r: dict[str, Any], k: str) -> Any:
    if k not in _JSON:
        return r.get(k)
    v = r.get(k, r.get(k.removesuffix("_json")))
    return None if v is None else json.dumps(v, sort_keys=True)


class LLMStore(Store):
    def __init__(self, path: Path, clock: Any = None):
        if clock is None:
            super().__init__(path)
        else:
            super().__init__(path, clock=clock)
        with self._conn() as c:
            c.executescript(LLM_SCHEMA_SQL)

    def insert_requests(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        q = f"INSERT OR IGNORE INTO llm_requests ({', '.join(_COLS)}) VALUES ({', '.join('?' for _ in _COLS)})"
        vals = []
        for r in rows:
            vals.append([_cell(r, k) for k in _COLS])
        with self.transaction() as c:
            c.executemany(q, vals)

    def insert_llm_feedback(self, request_id: str, reason_code: str, escalate: bool, simulated: bool) -> bool:
        with self.transaction() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO llm_feedback (request_id, reason_code, escalate, ts, simulated)"
                " VALUES (?,?,?,?,?)",
                (request_id, reason_code, int(bool(escalate)), self.clock(), int(simulated)),
            )
            return cur.rowcount == 1

    def max_request_seq(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COALESCE(MAX(seq), 0) FROM llm_requests").fetchone()[0])

    @staticmethod
    def _row(r: Any) -> dict[str, Any]:
        d = dict(r)
        for k in _JSON:
            d[k.removesuffix("_json")] = json.loads(d.pop(k)) if d.get(k) else None
        if d.get("fb_reason") is not None:
            d["label"] = {"reason_code": d.pop("fb_reason"), "escalate": bool(d.pop("fb_escalate"))}
        else:
            d.pop("fb_reason", None)
            d.pop("fb_escalate", None)
            d["label"] = None
        return d

    def requests_since(
        self,
        start_seq: int,
        release_id: str | None = None,
        with_labels: bool = True,
        labels_as_of: float | None = None,
    ) -> list[dict[str, Any]]:
        args: list[Any] = []
        join = "LEFT JOIN llm_feedback f ON f.request_id = r.request_id"
        if labels_as_of is not None:  # same contract as Store.requests_since
            join += " AND f.ts <= ?"
            args.append(labels_as_of)
        q = (
            "SELECT r.*, f.reason_code AS fb_reason, f.escalate AS fb_escalate FROM llm_requests r"
            f" {join} WHERE r.seq > ?"
        )
        args.append(start_seq)
        if release_id is not None:
            q += " AND r.release_id = ?"
            args.append(release_id)
        q += " ORDER BY r.seq"
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q, args).fetchall()]

    def recent_requests(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT r.*, f.reason_code AS fb_reason, f.escalate AS fb_escalate FROM llm_requests r"
                " LEFT JOIN llm_feedback f ON f.request_id = r.request_id ORDER BY r.seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(r) for r in rows]
