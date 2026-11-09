"""Read-only project contract for dashboards (the owner's Constellation reads it): /health, /capabilities, /runs.

Runs are this installation's recorded run reports (var/runs/*/report.json): the release demo, the chaos replay and
the load test. Every run is labeled ``mode: simulated`` because their request traffic is a seeded simulation; the
headline is this project's own summary of its own report, never a number computed by the reader.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from . import __version__

PROJECT_ID = "spectral-canary"
SCHEMA_VERSION = 1
_RUN_ID = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")


def _iso(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(timespec="seconds") if ts else None


def _headline(rep: dict[str, Any]) -> str:
    kind = rep.get("kind")
    if kind == "demo":
        a = rep.get("result", {}).get("actual", {})
        parts = [f"v2 {a.get('A')}", f"v4 {a.get('B')}", f"v3 {str(a.get('C')).replace('_', ' ')}"]
        pm = rep.get("_postmortem") or {}
        pr = ((pm.get("breach_evaluation") or {}).get("payload") or {}).get("labeled", {}).get("paired") or {}
        if pr.get("positives"):
            parts.append(
                f"{pr['candidate_caught']} vs {pr['champion_would_have_caught']} of {pr['positives']} frauds, "
                f"p={pr['mcnemar_p_one_sided']:.2g}"
            )
        v = pm.get("verification") or {}
        if v.get("requests"):
            parts.append(f"{v['served_by_expected']}/{v['requests']} verified")
        return " · ".join(parts)
    if kind == "chaos":
        sc = rep.get("scenarios", [])
        return f"{sum(1 for s in sc if s.get('passed'))} of {len(sc)} failure scenarios held"
    if kind == "loadtest":
        cells = [c for c in rep.get("cells", []) if c.get("throughput_rps")]
        slo = rep.get("slo_compliance", [])
        ok = sum(1 for s in slo if all(v for k, v in s.items() if k.endswith("_ok")))
        if not cells:
            return "no cells measured"
        best = max(cells, key=lambda c: c["throughput_rps"])
        return (
            f"peak {best['throughput_rps']} rps ({best['mode']}, c={best['concurrency']}) · "
            f"SLOs met {ok}/{len(slo)} cells at c≤16"
        )
    return str(rep.get("summary") or kind or "run")


def _load(runs_dir: Path) -> list[dict[str, Any]]:
    out = []
    for p in runs_dir.glob("*/report.json"):
        if p.parent.name == "latest" or p.parent.is_symlink():
            continue
        try:
            rep = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        run_id = str(rep.get("run_id") or p.parent.name)
        if not _RUN_ID.match(run_id) or rep.get("kind") not in ("demo", "chaos", "loadtest"):
            continue
        if rep.get("kind") == "demo":
            rid = (rep.get("result", {}).get("release_ids") or {}).get("C")
            pm = p.parent / "reports" / f"postmortem-{rid}.json"
            if rid and pm.exists():
                with contextlib.suppress(OSError, json.JSONDecodeError):
                    rep["_postmortem"] = json.loads(pm.read_text())
        started = rep.get("started_at")
        dur = rep.get("duration_s")
        out.append(
            {
                "run_id": run_id,
                "project_id": PROJECT_ID,
                "kind": rep["kind"],
                "status": "completed" if rep.get("passed") else "failed",
                "mode": "simulated",
                "started_at": _iso(started),
                "ended_at": _iso(started + dur) if started and dur else None,
                "headline": _headline(rep)[:160],
                "provenance": {
                    "synthetic": True,
                    "traffic": "seeded simulation; predictions, routing and rollbacks are real executions",
                    "commit": rep.get("commit"),
                },
                "_sort": started or 0,
            }
        )
    out.sort(key=lambda r: r["_sort"], reverse=True)
    for r in out:
        r.pop("_sort")
    return out


def runs_answer(runs_dir: Path, limit: int = 20) -> dict[str, Any]:
    runs = _load(runs_dir)
    return {"schema_version": SCHEMA_VERSION, "project_id": PROJECT_ID, "total": len(runs), "runs": runs[:limit]}


def run_answer(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
    for r in _load(runs_dir):
        if r["run_id"] == run_id:
            return r
    return None


def capabilities_answer() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": PROJECT_ID,
        "name": "Spectral Canary Works",
        "version": __version__,
        "active_mode": "simulated",
        "dataset": {"name": "spectral-txn-risk (seeded generator)", "license": "MIT", "synthetic": True},
        "implemented": [
            "immutable model registry: skops artifacts, SHA-256 verified on every load, HMAC-signed manifests",
            "offline non-inferiority gate with slice checks and actionable reasons",
            "shadow scoring that never changes a response",
            "10% sticky canary routing with version headers",
            "paired McNemar and flag-rate guardrails, one observation per card",
            "atomic compare-and-swap rollback, verified by real predictions",
            "hash-chained append-only audit log and generated postmortems",
            "seeded chaos replay: 10 failure scenarios with expected outcomes",
            "Prometheus metrics, SSE event feed, control-room UI",
        ],
        "not_implemented": [
            "saturation-aware guardrail (overload is still read as a candidate failure)",
            "multi-replica controller (guardrail windows live in one process)",
            "sequential testing / alpha spending across windows",
        ],
        "disabled_by_design": [
            "cloud deployment (the AWS path is implemented but not provisioned)",
            "chaos endpoints unless SCW_ENABLE_CHAOS=1",
            "real customer data (synthetic only)",
        ],
        "endpoints": ["/health", "/capabilities", "/runs", "/runs/{run_id}"],
    }


def health_answer(ready: dict[str, Any], runs_dir: Path) -> dict[str, Any]:
    runs = _load(runs_dir)
    last = runs[0] if runs else None
    return {
        "ok": bool(ready.get("ready")),
        "status": "ok" if ready.get("ready") else "degraded",
        "app": PROJECT_ID,
        "project_id": PROJECT_ID,
        "version": __version__,
        "mode": "simulated",
        "champion": ready.get("champion"),
        "runs": len(runs),
        "last_run": {"run_id": last["run_id"], "status": last["status"]} if last else None,
        "problems": list(ready.get("problems") or []),
    }
