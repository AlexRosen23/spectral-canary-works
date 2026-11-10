"""Before/after numbers for the compiled single-row scorer (src/spectral_canary/fastscore.py).

    uv run --frozen python scripts/bench_scoring.py            # per-model single-row latency, both paths
    uv run --frozen python scripts/bench_scoring.py --story    # + one in-process release story per path

Needs `make train-fixtures`. Each story runs in its own process with SCW_FAST_SCORING=0 (sklearn, the old path)
or 1 (compiled), on a fresh fixture registry, so the two runs differ only in the scoring path. Prints JSON.
Timings on a shared laptop are noisy: the script records the load average next to every number.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np

from spectral_canary import artifacts as art
from spectral_canary import fastscore
from spectral_canary.data import load_split_csv
from spectral_canary.paths import state_paths
from spectral_canary.serving import limit_native_threads

VERSIONS = ("v1-logreg", "v2-hgb", "v3-hgb-fresh", "v4-hgb-segfilter")


def pct(xs: list[float]) -> dict[str, float]:
    a = np.asarray(xs)
    return {k: round(float(np.percentile(a, q)), 1) for k, q in (("p50", 50), ("p95", 95), ("p99", 99))}


def model_latency(n: int) -> dict:
    limit_native_threads()
    paths = state_paths()
    X, _, _ = load_split_csv(paths.data / "test.csv")
    out = {}
    for v in VERSIONS:
        f = paths.artifacts / v / "model.skops"
        est = art.load_verified(f, art.sha256_file(f))
        scorer, why = fastscore.build(est)
        assert scorer is not None, why
        rows = X[:n]
        lists = rows.tolist()
        t_sk: list[float] = []
        t_fast: list[float] = []
        for i in range(n):  # interleaved, so machine load hits both paths alike
            t0 = time.perf_counter()
            a = est.predict_proba(rows[i : i + 1])[0, 1]
            t_sk.append((time.perf_counter() - t0) * 1e6)
            t0 = time.perf_counter()
            b = scorer(lists[i])
            t_fast.append((time.perf_counter() - t0) * 1e6)
            assert np.float64(a).view(np.uint64) == np.float64(b).view(np.uint64), (v, i)
        out[v] = {
            "unit": "us",
            "sklearn": pct(t_sk),
            "compiled": pct(t_fast),
            "speedup_p50": round(float(np.median(t_sk) / np.median(t_fast)), 1),
        }
    return out


def story_child(home: str, fast: str) -> None:
    os.environ["SCW_FAST_SCORING"] = fast
    from spectral_canary.drivers import InProcessDriver
    from spectral_canary.fixtures import fixture_runtime
    from spectral_canary.scenarios import release_story

    with tempfile.TemporaryDirectory() as tmp:
        rt = fixture_runtime(Path(tmp) / "run", home=Path(home))
        try:
            paths = sorted({m.scoring_path for m in (rt.service.pool.get(v) for v in VERSIONS)})
            t0 = time.perf_counter()
            res = release_story(InProcessDriver(rt), "bench", scale=1.0)
            dt = time.perf_counter() - t0
            rt.service.writer.flush()
            with rt.store._conn() as c:
                n = c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        finally:
            rt.close()
    summary = {"seconds": round(dt, 2), "passed": res["passed"], "requests": n, "scoring_paths": paths}
    print(json.dumps({**summary, "actual": res["actual"]}))


def story(fast: str) -> dict:
    cmd = [sys.executable, __file__, "--story-child", fast]
    la0 = os.getloadavg()[0]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True, env={**os.environ})
    out = json.loads(r.stdout.strip().splitlines()[-1])
    out["load_avg_1m"] = [round(la0, 1), round(os.getloadavg()[0], 1)]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000, help="single-row calls per model and path")
    ap.add_argument("--story", action="store_true", help="also time one in-process release story per path")
    ap.add_argument("--story-child", choices=("0", "1"), help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.story_child:
        story_child(str(state_paths().home), a.story_child)
        return
    report: dict = {
        "machine": f"{platform.machine()} {platform.platform()}",
        "python": platform.python_version(),
        "omp_num_threads": os.environ.get("OMP_NUM_THREADS"),
        "load_avg_1m_start": round(os.getloadavg()[0], 1),
        "single_row": model_latency(a.n),
    }
    if a.story:
        report["release_story"] = {"sklearn": story("0"), "compiled": story("1")}
    report["load_avg_1m_end"] = round(os.getloadavg()[0], 1)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
