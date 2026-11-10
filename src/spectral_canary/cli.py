"""`scw` command line: setup, train-fixtures, eval, serve, demo, chaos-replay, verify, loadtest."""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .paths import PROJECT_ROOT, state_paths


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def machine_info() -> dict[str, Any]:
    import psutil

    info: dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
    }
    try:
        info["cpu"] = (
            subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
            or platform.processor()
        )
    except Exception:
        info["cpu"] = platform.processor()
    info["load_avg_1m"] = round(os.getloadavg()[0], 2)
    return info


def git_commit() -> str | None:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5
            ).stdout.strip()
            or None
        )
    except Exception:
        return None


def cmd_setup(args: argparse.Namespace) -> int:
    from .artifacts import signing_key
    from .auth import ensure_operators_file

    paths = state_paths().ensure()
    signing_key(paths)
    ensure_operators_file(paths.operators)
    print(f"state dir: {paths.home}\nsigning key + operator tokens: {paths.signing_key.parent} (gitignored, 0600)")
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    from .fixtures import train_fixtures

    paths = state_paths().ensure()
    res = train_fixtures(paths, update_manifests=args.update_manifests)
    print(
        f"dataset {res['dataset']['dataset_hash'][:16]} ({res['dataset']['n_rows']} rows); "
        f"{len(res['models'])} model versions ready in {paths.artifacts}"
    )
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    paths = state_paths()
    rows = []
    for d in sorted(paths.artifacts.glob("*/manifest.json")):
        m = json.loads(d.read_text())
        t = m["metrics"]["test"]
        rows.append(
            {
                "version": m["version"],
                "family": m["family"],
                "degraded": m["deliberately_degraded"],
                "pr_auc": t["pr_auc"],
                "pr_auc_ci95": t["pr_auc_ci95"],
                "roc_auc": t["roc_auc"],
                "precision": t["precision"],
                "recall": t["recall"],
                "f1": t["f1"],
                "brier": t["brier"],
                "ece": t["ece"],
                "threshold": t["threshold"],
                "latency_p50_ms": m["latency_single_row"]["p50"],
                "latency_p95_ms": m["latency_single_row"]["p95"],
                "bytes": m["artifact"]["bytes"],
                "cnp_recall": t["slices"]["card_not_present"]["recall"],
                "test_n": t["n"],
                "test_positives": t["positives"],
            }
        )
    if not rows:
        print("no trained models; run `make train-fixtures`", file=sys.stderr)
        return 1
    hdr = [
        "version",
        "pr_auc",
        "pr_auc_ci95",
        "precision",
        "recall",
        "f1",
        "ece",
        "cnp_recall",
        "latency_p95_ms",
    ]
    lines = ["| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    for r in rows:
        lines.append("| " + " | ".join(str(r[h]) for h in hdr) + " |")
    table = "\n".join(lines)
    print(f"Held-out test split: {rows[0]['test_n']} rows, {rows[0]['test_positives']} positives (synthetic)\n")
    print(table)
    paths.reports.mkdir(parents=True, exist_ok=True)
    (paths.reports / "eval.json").write_text(
        json.dumps({"models": rows, "machine": machine_info(), "commit": git_commit()}, indent=2)
    )
    (paths.reports / "EVAL.md").write_text(table + "\n")
    print(f"\nwritten: {paths.reports / 'eval.json'}")
    return 0


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def start_server(
    db: Path, port: int, extra_env: dict[str, str] | None = None, log_path: Path | None = None
) -> subprocess.Popen:
    import httpx

    if not _port_free(port):
        raise SystemExit(f"port {port} is busy (stop `make serve` first, or pass --port)")
    env = {**os.environ, "SCW_DB": str(db), **(extra_env or {})}
    out = open(log_path, "w") if log_path else subprocess.DEVNULL  # noqa: SIM115
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "spectral_canary.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=out,
        stderr=out,
        cwd=PROJECT_ROOT,
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"server exited early (code {proc.returncode}); see {log_path}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=2).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    proc.kill()
    raise SystemExit("server did not become ready within 60 s")


def stop_server(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def cmd_demo(args: argparse.Namespace) -> int:
    from .auth import ensure_operators_file
    from .drivers import HttpDriver, InProcessDriver
    from .fixtures import fixture_runtime, new_run_dir, point_latest
    from .registry import verify_registry
    from .scenarios import release_story

    paths = state_paths().ensure()
    run_dir = new_run_dir(paths, "demo")
    started = time.time()
    print(f"[demo] run {run_dir.name}: fresh registry, v1-logreg bootstrapped as champion (fixture state)")
    rt = fixture_runtime(run_dir)
    tokens = ensure_operators_file(paths.operators)
    if args.in_process:
        driver: Any = InProcessDriver(rt)
        result = release_story(driver, run_dir.name, scale=args.scale)
        proc = None
    else:
        rt.close()
        print(f"[demo] starting gateway on 127.0.0.1:{args.port} (real HTTP; SIMULATED traffic)")
        proc = start_server(
            run_dir / "registry.sqlite",
            args.port,
            {"SCW_RUN_ID": run_dir.name},
            log_path=run_dir / "server.log",
        )
        try:
            driver = HttpDriver(f"http://127.0.0.1:{args.port}", tokens)
            result = release_story(driver, run_dir.name, scale=args.scale)
            metrics_text = driver.http.get("/metrics").text
            (run_dir / "metrics.prom").write_text(metrics_text)
        finally:
            stop_server(proc)
        rt = fixture_runtime(run_dir)
    chain = rt.store.verify_event_chain()
    reg = verify_registry(rt.store, rt.paths)
    rid_c = result["release_ids"]["C"]
    pm = run_dir / "reports" / f"postmortem-{rid_c}.md"
    report = {
        "run_id": run_dir.name,
        "kind": "demo",
        "driver": "in_process" if args.in_process else "http",
        "started_at": started,
        "duration_s": round(time.time() - started, 1),
        "passed": result["passed"] and chain["ok"] and all(r["ok"] for r in reg) and pm.exists(),
        "summary": f"A={result['actual']['A']} B={result['actual']['B']} C={result['actual']['C']} "
        f"champion={result['actual']['champion_after']}",
        "result": result,
        "event_chain": chain,
        "registry_verify": reg,
        "postmortem": str(pm.relative_to(paths.home)) if pm.exists() else None,
        "machine": machine_info(),
        "commit": git_commit(),
        "simulated_traffic": True,
        "db": str((run_dir / "registry.sqlite").relative_to(paths.home)),
    }
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    point_latest(paths, run_dir)
    rt.close()
    print(f"[demo] expected: {result['expected']}")
    print(f"[demo] actual:   {result['actual']}")
    print(
        f"[demo] event chain ok={chain['ok']} ({chain['checked']} events); "
        f"registry verified={all(r['ok'] for r in reg)}"
    )
    print(f"[demo] postmortem: {pm}")
    print(f"[demo] report: {run_dir / 'report.json'}  ({report['duration_s']} s)")
    print(f"[demo] {'PASSED' if report['passed'] else 'FAILED'}. View it: make serve (API) + make web (UI)")
    return 0 if report["passed"] else 1


def cmd_chaos(args: argparse.Namespace) -> int:
    from .chaos import run_chaos

    try:
        report = run_chaos(state_paths().ensure(), only=args.only)
    except ValueError as exc:
        print(f"[chaos] {exc}", file=sys.stderr)
        return 2
    for s in report["scenarios"]:
        # a scenario that crashed has no expected outcome, only the error: print that instead of a blank
        detail = s.get("expected") or f"ERROR {s.get('error', '')}"
        print(f"[chaos] {'PASS' if s['passed'] else 'FAIL'}  {s['scenario']:<22} {detail}")
    print(
        f"[chaos] {sum(s['passed'] for s in report['scenarios'])}/{len(report['scenarios'])} passed; "
        f"report: {report['report_path']}"
    )
    return 0 if report["passed"] else 1


def cmd_verify(args: argparse.Namespace) -> int:
    from .registry import verify_registry
    from .runtime import build_runtime

    rt = build_runtime()
    out: dict[str, Any] = {
        "db": str(rt.db_path),
        "models": verify_registry(rt.store, rt.paths),
        "event_chain": rt.store.verify_event_chain(),
    }
    _print(out)
    return 0 if out["event_chain"]["ok"] and all(m["ok"] for m in out["models"]) else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .telemetry import configure_logging

    configure_logging()
    if args.bootstrap_fixture:
        # Container/stack start: create a fixture registry once (v1..v4 registered, v1 champion) and serve it.
        from .fixtures import fixture_runtime, point_latest

        paths = state_paths().ensure()
        # Key the registry to the exact artifact bytes: a rebuilt image (new artifact hashes) gets a fresh
        # fixture registry instead of failing integrity checks against an old one in a persistent volume.
        import hashlib

        hashes = "".join(
            json.loads(p.read_text())["artifact"]["sha256"] for p in sorted(paths.artifacts.glob("*/manifest.json"))
        )
        run_dir = paths.runs / f"{args.bootstrap_fixture}-{hashlib.sha256(hashes.encode()).hexdigest()[:10]}"
        fixture_runtime(run_dir).close()
        point_latest(paths, run_dir)
        os.environ["SCW_DB"] = str(run_dir / "registry.sqlite")
    uvicorn.run("spectral_canary.api:app", host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_traffic(args: argparse.Namespace) -> int:
    """Steady simulated traffic for the local stack (so dashboards show real request data)."""
    import httpx

    from .traffic import Phase, TrafficPlan, iter_requests

    run = f"stack-{int(time.time())}"
    n = args.requests or 10**9
    plan = TrafficPlan(run, [Phase(args.scenario, min(n, 200_000), int(time.time()) % 10_000, "stack traffic")])
    sent = 0
    with httpx.Client(base_url=args.base_url, timeout=10) as c:
        for _ in range(60):
            try:
                if c.get("/readyz").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        for req in iter_requests(plan):
            try:
                r = c.post("/v1/predict", json=req.payload)
                if r.status_code == 200 and req.label_arrives:
                    c.post(
                        "/v1/feedback",
                        json={"request_id": req.payload["request_id"], "label": req.label, "simulated": True},
                    )
            except httpx.HTTPError as exc:
                print(f"[traffic] {exc}", file=sys.stderr)
            sent += 1
            if sent >= n:
                break
            time.sleep(1 / args.rps)
    print(f"[traffic] sent {sent} SIMULATED requests")
    return 0


def cmd_loadtest(args: argparse.Namespace) -> int:
    from .loadtest import run_loadtests

    return run_loadtests(state_paths().ensure(), args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scw", description="Spectral Canary Works")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(fn=cmd_setup)
    p = sub.add_parser("train-fixtures")
    p.add_argument("--update-manifests", action="store_true", help="accept new hashes into manifests/")
    p.set_defaults(fn=cmd_train)
    sub.add_parser("eval").set_defaults(fn=cmd_eval)
    p = sub.add_parser("demo")
    p.add_argument("--port", type=int, default=int(os.environ.get("SCW_DEMO_PORT", "5120")))
    p.add_argument("--in-process", action="store_true", help="drive the runtime directly instead of HTTP")
    p.add_argument("--scale", type=float, default=1.0)
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("chaos-replay")
    p.add_argument("--only", nargs="*")
    p.set_defaults(fn=cmd_chaos)
    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=int(os.environ.get("SCW_API_PORT", "5120")))
    p.add_argument(
        "--bootstrap-fixture",
        metavar="RUN_NAME",
        default=None,
        help="create/reuse var/runs/RUN_NAME as a fixture registry and serve it",
    )
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("traffic", help="send labeled SIMULATED baseline traffic to a running gateway")
    p.add_argument("--base-url", default="http://127.0.0.1:5120")
    p.add_argument("--rps", type=float, default=5.0)
    p.add_argument("--requests", type=int, default=0, help="0 = run until stopped")
    p.add_argument("--scenario", default="baseline")
    p.set_defaults(fn=cmd_traffic)
    p = sub.add_parser("loadtest")
    p.add_argument("--port", type=int, default=5130)
    p.add_argument("--concurrency", type=int, nargs="*", default=[1, 4, 16, 32])
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--mode", choices=["champion", "canary"], nargs="*", default=["champion", "canary"])
    p.add_argument("--base-url", default=None, help="test an already-running gateway instead (e.g. docker)")
    p.set_defaults(fn=cmd_loadtest)
    from .llm.commands import add_parsers as add_llm_parsers

    add_llm_parsers(sub)
    args = ap.parse_args(argv)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
