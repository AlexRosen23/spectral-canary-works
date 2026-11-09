"""Real HTTP load test (closed-loop, asyncio + httpx) against a dedicated gateway process.

For each (mode, workers, concurrency) cell: start a fresh gateway on its own port and registry, warm it,
run N concurrent clients for a fixed duration sending SIMULATED baseline payloads, record every
request's latency/status/served version to ``raw-*.jsonl``, and sample the server's CPU and RSS.
Nothing is extrapolated: the report contains only what was measured, plus the machine's load average.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import psutil

from .data import generate
from .schema import decode_row

SLOS = {
    "availability": {"objective": 0.995, "definition": "share of requests answered with HTTP 200"},
    "latency_p95_ms": {"objective": 250.0, "definition": "client-observed p95 at concurrency <= 16"},
    "telemetry_drop_rate": {"objective": 0.0, "definition": "request rows dropped by the bounded writer"},
}


def _payloads(n: int = 4000) -> list[dict[str, Any]]:
    X, _ = generate(n, 515, "baseline")
    return [
        {
            "request_key": f"card-{i % 3000:05d}",
            "features": decode_row(X[i]),
            "context": {"simulated": True, "scenario": "loadtest"},
        }
        for i in range(n)
    ]


async def _client(
    base: str, payloads: list[dict[str, Any]], stop_at: float, out: list[dict[str, Any]], wid: int
) -> None:
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    async with httpx.AsyncClient(base_url=base, timeout=10.0, limits=limits) as c:
        i = wid
        while time.perf_counter() < stop_at:
            p = {**payloads[i % len(payloads)], "request_id": f"lt-{wid}-{i}"}
            t0 = time.perf_counter()
            try:
                r = await c.post("/v1/predict", json=p)
                status = r.status_code
                version = r.headers.get("x-model-version")
                arm = r.headers.get("x-release-arm")
            except httpx.HTTPError as exc:
                status, version, arm = 0, None, f"client_error:{type(exc).__name__}"
            out.append(
                {
                    "t": time.time(),
                    "ms": (time.perf_counter() - t0) * 1000,
                    "status": status,
                    "version": version,
                    "arm": arm,
                    "worker": wid,
                }
            )
            i += 7


def _sample(pid: int, stop: list[bool], samples: list[dict[str, Any]]) -> None:
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return
    procs = [root, *root.children(recursive=True)]
    for p in procs:
        with contextlib.suppress(psutil.Error):
            p.cpu_percent(None)
    while not stop[0]:
        time.sleep(0.5)
        cpu, rss = 0.0, 0
        for p in procs:
            try:
                cpu += p.cpu_percent(None)
                rss += p.memory_info().rss
            except psutil.Error:
                pass
        samples.append({"t": time.time(), "cpu_percent": round(cpu, 1), "rss_mb": round(rss / 2**20, 1)})


def run_cell(
    base: str,
    pid: int | None,
    concurrency: int,
    duration: float,
    payloads: list[dict[str, Any]],
    raw_path: Path,
) -> dict[str, Any]:
    import threading

    out: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    stop = [False]
    th = threading.Thread(target=_sample, args=(pid, stop, samples), daemon=True) if pid else None
    if th:
        th.start()
    started = time.time()

    async def main() -> None:
        stop_at = time.perf_counter() + duration
        await asyncio.gather(*[_client(base, payloads, stop_at, out, w) for w in range(concurrency)])

    asyncio.run(main())
    stop[0] = True
    if th:
        th.join(timeout=2)
    with raw_path.open("w") as fh:
        for r in out:
            fh.write(json.dumps(r) + "\n")
    lat = np.array([r["ms"] for r in out if r["status"] == 200])
    ok = sum(1 for r in out if r["status"] == 200)
    versions: dict[str, int] = {}
    for r in out:
        if r["version"]:
            versions[r["version"]] = versions.get(r["version"], 0) + 1
    statuses: dict[str, int] = {}
    for r in out:
        statuses[str(r["status"])] = statuses.get(str(r["status"]), 0) + 1
    elapsed = (max(r["t"] for r in out) - started) if out else duration
    return {
        "concurrency": concurrency,
        "duration_s": round(elapsed, 2),
        "requests": len(out),
        "throughput_rps": round(len(out) / elapsed, 1) if elapsed else None,
        "availability": round(ok / len(out), 5) if out else None,
        "statuses": statuses,
        "latency_ms": {k: round(float(np.percentile(lat, q)), 2) for k, q in (("p50", 50), ("p95", 95), ("p99", 99))}
        | {"max": round(float(lat.max()), 2), "mean": round(float(lat.mean()), 2)}
        if len(lat)
        else None,
        "served_versions": versions,
        "server_cpu_percent_mean": round(float(np.mean([s["cpu_percent"] for s in samples])), 1) if samples else None,
        "server_cpu_percent_max": max((s["cpu_percent"] for s in samples), default=None),
        "server_rss_mb_max": max((s["rss_mb"] for s in samples), default=None),
        "raw": raw_path.name,
        "started_at": started,
    }


def run_loadtests(paths: Any, args: argparse.Namespace) -> int:
    from .cli import git_commit, machine_info, start_server, stop_server
    from .drivers import InProcessDriver
    from .fixtures import fixture_runtime, new_run_dir
    from .scenarios import BASE, Phase, run_traffic

    root = new_run_dir(paths, "loadtest")
    payloads = _payloads()
    cells = []
    plan = [(m, c) for m in args.mode for c in args.concurrency]
    print(f"[loadtest] {len(plan)} cells x {args.duration}s; raw results -> {root}")
    for mode, conc in plan:
        cell_dir = root / f"{mode}-c{conc}"
        rt = fixture_runtime(cell_dir)
        if mode == "canary":
            d = InProcessDriver(rt)
            r = d.propose("v2-hgb")
            d.stage(r["release_id"])
            d.shadow(r["release_id"])
            run_traffic(d, f"{root.name}-shadow", [Phase(BASE, 450, 301, "shadow")], seed=5)
            d.canary(r["release_id"])
        rt.close()
        base = args.base_url
        proc = None
        if base is None:
            proc = start_server(
                cell_dir / "registry.sqlite",
                args.port,
                {"SCW_RUN_ID": cell_dir.name},
                log_path=cell_dir / "server.log",
            )
            base = f"http://127.0.0.1:{args.port}"
        try:
            run_cell(base, None, 2, 2.0, payloads, cell_dir / "warmup.jsonl")
            load_before = machine_info()["load_avg_1m"]
            res = run_cell(
                base,
                proc.pid if proc else None,
                conc,
                args.duration,
                payloads,
                cell_dir / f"raw-{mode}-c{conc}.jsonl",
            )
            metrics = httpx.get(f"{base}/metrics", timeout=5).text
            dropped = next(
                (
                    float(line.split()[-1])
                    for line in metrics.splitlines()
                    if line.startswith("scw_telemetry_dropped_total")
                ),
                0.0,
            )
            (cell_dir / "metrics.prom").write_text(metrics)
        finally:
            if proc:
                stop_server(proc)
        res.update(
            {
                "mode": mode,
                "workers": 1,
                "telemetry_dropped": dropped,
                "load_avg_1m_before": load_before,
                "load_avg_1m_after": machine_info()["load_avg_1m"],
            }
        )
        lat = res["latency_ms"] or {}
        print(
            f"[loadtest] {mode:<8} c={conc:<3} {res['requests']:>6} req  {res['throughput_rps']:>7} rps  "
            f"p50 {lat.get('p50')} p95 {lat.get('p95')} p99 {lat.get('p99')} ms  avail {res['availability']}  "
            f"cpu~{res['server_cpu_percent_mean']}%  rss {res['server_rss_mb_max']} MB  "
            f"versions {res['served_versions']}"
        )
        cells.append(res)
    slo = []
    for c in cells:
        if c["concurrency"] <= 16 and c["latency_ms"]:
            slo.append(
                {
                    "cell": f"{c['mode']}-c{c['concurrency']}",
                    "availability_ok": c["availability"] >= SLOS["availability"]["objective"],
                    "latency_p95_ok": c["latency_ms"]["p95"] <= SLOS["latency_p95_ms"]["objective"],
                    "telemetry_ok": c["telemetry_dropped"] == 0,
                }
            )
    n_pass = sum(all(v for k, v in s.items() if k.endswith("_ok")) for s in slo)
    report = {
        "run_id": root.name,
        "kind": "loadtest",
        "started_at": cells[0]["started_at"] if cells else time.time(),
        "passed": all(all(v for k, v in s.items() if k.endswith("_ok")) for s in slo),
        "summary": f"{len(cells)} cells; SLO cells passing {n_pass}/{len(slo)}",
        "cells": cells,
        "slos": SLOS,
        "slo_compliance": slo,
        "machine": machine_info(),
        "commit": git_commit(),
        "command": "scw " + " ".join(sys.argv[1:]),
        "traffic": "SIMULATED baseline payloads (seeded); real HTTP, real model inference, real SQLite telemetry",
    }
    (root / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"[loadtest] report: {root / 'report.json'}")
    return 0
