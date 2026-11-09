"""CLI commands for the LLM lane: ``scw llm-gold``, ``scw llm-eval``, ``scw llm-demo``."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from ..paths import state_paths


def cmd_llm_gold(args: argparse.Namespace) -> int:
    from .alerts import build_gold, gold_sha256, load_gold
    from .runtime import GOLD_PATH

    cases = build_gold()
    sha = gold_sha256(cases)
    if args.write:
        from .alerts import write_gold

        write_gold(GOLD_PATH, cases)
        print(f"wrote {GOLD_PATH} ({len(cases)} cases, sha256 {sha[:16]})")
        return 0
    committed = gold_sha256(load_gold(GOLD_PATH))
    ok = committed == sha
    print(f"gold set: {len(cases)} cases; generator sha256 {sha[:16]}; committed {committed[:16]}; match={ok}")
    return 0 if ok else 1


def cmd_llm_eval(args: argparse.Namespace) -> int:
    """Offline suite for registered-or-not bundles on the gold test split (no registry, no release)."""
    from ..artifacts import canonical_json
    from .alerts import gold_sha256, load_gold
    from .bundles import BUNDLE_DIR, LoadedBundle, compile_bundle
    from .client import OllamaBackend
    from .evals import run_suite, summarize
    from .policy import LLMReleasePolicy
    from .runtime import GOLD_PATH

    paths = state_paths().ensure()
    pol = LLMReleasePolicy.load()
    be = OllamaBackend()
    cases = load_gold(GOLD_PATH, args.split)
    sha = gold_sha256(load_gold(GOLD_PATH))
    out = paths.runs / f"llm-eval-{time.strftime('%Y%m%d-%H%M%S')}"
    rows = []
    for v in args.bundles:
        spec = BUNDLE_DIR / f"{v}.toml"
        b = compile_bundle(spec, be.model_digest(compile_bundle(spec, None)["model"]))
        lb = LoadedBundle(v, b, hashlib.sha256(canonical_json(b)).hexdigest())
        res, path, _ = run_suite(lb, cases, be, args.k, out, sha, pol.offline_gate.concurrency, 60, print)
        s = summarize(res, args.k, pol.price_dict)
        rows.append({"bundle": v, **s, "raw": str(path)})
        print(
            json.dumps(
                {
                    "bundle": v,
                    "correct_pass1": s["correct_pass1"]["rate"],
                    "pass^k": s["correct_pass_k"]["rate"],
                    "escalation_recall": s["escalation_recall"]["rate"],
                    "segments": {k: x["rate"] for k, x in s["segments"].items()},
                }
            )
        )
    (out / "summary.json").write_text(json.dumps(rows, indent=2))
    return 0


def cmd_llm_demo(args: argparse.Namespace) -> int:
    from ..auth import ensure_operators_file
    from ..cli import git_commit, machine_info, start_server, stop_server
    from ..fixtures import fixture_runtime, new_run_dir
    from ..registry import verify_registry
    from .bundles import register_all_bundles
    from .client import OllamaBackend
    from .runtime import DB_NAME, build_llm_runtime
    from .scenarios import CHAMPION0, LLMHttpDriver, llm_release_story

    paths = state_paths().ensure()
    be = OllamaBackend()
    if not be.available():
        print("[llm-demo] Ollama is not reachable at 127.0.0.1:11434; start it and pull llama3.2:3b + qwen2.5-coder:3b")
        return 2
    run_dir = new_run_dir(paths, "llm-demo")
    started = time.time()
    commit0 = git_commit()
    log = lambda m: print(m, flush=True)  # noqa: E731
    log(f"[llm-demo] run {run_dir.name}")
    fixture_runtime(run_dir).close()  # the ML lane's fixture registry, so the same gateway serves both lanes
    reused: list[str] = []
    if args.reuse_evals:
        # Offline suite results are keyed by bundle sha256 + gold sha256 + k, so a finished suite of the same
        # immutable bundle can be reused instead of re-calling the model; gate reports then say reused_previous_run.
        import shutil

        (run_dir / "evals").mkdir(exist_ok=True)
        for p in sorted(Path(args.reuse_evals).glob("evals/*.jsonl")):
            shutil.copy2(p, run_dir / "evals" / p.name)
            reused.append(p.name)
        log(f"[llm-demo] reusing {len(reused)} offline suite result files from {args.reuse_evals}")
    rt = build_llm_runtime(db_path=run_dir / DB_NAME, report_dir=run_dir / "reports", log=log)
    rt.store.run_id = run_dir.name
    reg = register_all_bundles(rt.store, rt.paths, be)
    log(f"[llm-demo] registered bundles: {reg}")
    log(f"[llm-demo] bootstrapping {CHAMPION0} (offline gate on the gold test split, k={rt.policy.offline_gate.k})")
    rt.controller.bootstrap_champion(CHAMPION0, actor="fixture-bootstrap")
    boot = next(e for e in rt.store.events(limit=10_000) if e["kind"] == "deployment.bootstrapped")
    rt.close()
    tokens = ensure_operators_file(paths.operators)
    log(f"[llm-demo] starting gateway on 127.0.0.1:{args.port} (real HTTP, real Ollama calls, SIMULATED alerts)")
    proc = start_server(
        run_dir / "registry.sqlite",
        args.port,
        {"SCW_LLM_DB": str(run_dir / DB_NAME), "SCW_RUN_ID": run_dir.name},
        log_path=run_dir / "server.log",
    )
    try:
        d = LLMHttpDriver(f"http://127.0.0.1:{args.port}", tokens)
        result = llm_release_story(d, run_dir.name, scale=args.scale, echo=log)
        (run_dir / "metrics.prom").write_text(d.http.get("/metrics").text)
    finally:
        stop_server(proc)
    rt = build_llm_runtime(db_path=run_dir / DB_NAME, report_dir=run_dir / "reports")
    chain = rt.store.verify_event_chain()
    regv = verify_registry(rt.store, rt.paths)
    gates = {r["release_id"]: r["gate_report"] for r in rt.store.list_releases()}
    rid_c = result["release_ids"].get("D")
    pm = run_dir / "reports" / f"postmortem-{rid_c}.md"
    report: dict[str, Any] = {
        "run_id": run_dir.name,
        "kind": "llm-demo",
        "lane": "llm",
        "driver": "http",
        "started_at": started,
        "duration_s": round(time.time() - started, 1),
        "passed": result["passed"] and chain["ok"] and all(r["ok"] for r in regv),
        "summary": " ".join(f"{k}={result['actual'].get(k)}" for k in ("A", "B", "C", "D", "champion_after")),
        "result": result,
        "bootstrap_gate": boot["payload"],
        "gate_reports": gates,
        "event_chain": chain,
        "registry_verify": regv,
        "postmortem": str(pm.relative_to(paths.home)) if pm.exists() else None,
        "policy_sha256": rt.policy.sha256,
        "gold_sha256": rt.gold_sha,
        "models": {
            m["version"]: {
                "model": m["manifest"]["model"],
                "digest": m["manifest"]["model_digest"],
                "bundle_sha256": m["artifact_sha256"],
            }
            for m in rt.store.list_models()
        },
        "ollama_url": be.base_url,
        "machine": machine_info(),
        "commit": git_commit(),
        "command": "uv run scw llm-demo --port "
        + str(args.port)
        + (f" --reuse-evals {args.reuse_evals}" if args.reuse_evals else ""),
        "reused_offline_results": reused,
        "commit_at_start": commit0,
        "simulated_traffic": True,
        "db": str((run_dir / DB_NAME).relative_to(paths.home)),
    }
    rt.close()
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    link = paths.runs / "llm-latest"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(run_dir.name)
    log(f"[llm-demo] expected: {result['expected']}")
    log(f"[llm-demo] actual:   {result['actual']}")
    log(
        f"[llm-demo] event chain ok={chain['ok']} ({chain['checked']} events); registry ok={all(r['ok'] for r in regv)}"
    )
    log(
        f"[llm-demo] report: {run_dir / 'report.json'} ({report['duration_s']} s) -> "
        + ("PASSED" if report["passed"] else "FAILED")
    )
    return 0 if report["passed"] else 1


def add_parsers(sub: Any) -> None:
    p = sub.add_parser("llm-gold", help="check (or --write) the committed triage gold set against its generator")
    p.add_argument("--write", action="store_true")
    p.set_defaults(fn=cmd_llm_gold)
    p = sub.add_parser("llm-eval", help="offline regression suite for bundles on the gold set (real Ollama calls)")
    p.add_argument("bundles", nargs="+")
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--split", default="test", choices=["dev", "test"])
    p.set_defaults(fn=cmd_llm_eval)
    p = sub.add_parser("llm-demo", help="the LLM release story over real HTTP with real Ollama calls")
    p.add_argument("--port", type=int, default=int(os.environ.get("SCW_LLM_DEMO_PORT", "5141")))
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument(
        "--reuse-evals",
        metavar="RUN_DIR",
        default=None,
        help="reuse offline suite JSONL files of the same bundles from an earlier run directory",
    )
    p.set_defaults(fn=cmd_llm_demo)
