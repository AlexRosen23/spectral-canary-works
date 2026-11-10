# Load test results (genuine measurements only)

## Latest: A/B on the merged code, quiet machine (2026-11-10 08:09–08:16)

Commit `f1ca1b2` (`main` at `661003f` with #4 and #6–#10 merged, plus the compiled scorer; the scorer code is
unchanged in `a979cc0` after the later rebase onto `cde1920`, whose only code change is the postmortem event's
path). Same `make loadtest` as below, back to back: `SCW_FAST_SCORING=0 make loadtest` (the previous sklearn path),
then `make loadtest`. Apple M5 Pro, 18 cores, 48 GB, macOS 27.0.1, Python 3.13.16, one uvicorn process,
`OMP_NUM_THREADS=1`, run under the shared heavy-test lock, 1-minute load average **4.2–7.8** (the quietest this
machine has been for any run here). Reports: [`docs/evidence/perf/`](evidence/perf/) (`loadtest-20261110-080958-sklearn`,
`loadtest-20261110-081321-compiled`).

| mode | conc. | rps before | rps after | p50 ms before → after | p95 ms before → after | p99 ms before → after | availability before → after | server CPU % before → after | load avg (1m) before / after |
|---|---|---|---|---|---|---|---|---|---|
| champion | 1 | 609.1 | **1032.5** | 1.51 → 0.89 | 2.4 → 1.5 | 3.59 → 2.22 | 100% → 100% | 62.5 → 40.5 | 6.51 / 5.87 |
| champion | 4 | 1051.9 | **1867.4** | 3.69 → 1.97 | 5.55 → 3.39 | 7.16 → 4.23 | 100% → 100% | 111.2 → 78.6 | 5.47 / 5.69 |
| champion | 16 | 913.5 | **1910.5** | 9.69 → 7.91 | 62.29 → 11.63 | 144.58 → 14.36 | 99.989% → 100% | 119.0 → 82.2 | 4.98 / 5.46 |
| champion | 32 | 787.9 | **2195.7** | 31.26 → 13.47 | 94.78 → 19.91 | 187.66 → 26.65 | 100% → 100% | 118.0 → 85.0 | 4.54 / 5.09 |
| canary | 1 | 476.9 | **974.8** | 1.76 → 0.91 | 4.07 → 1.4 | 5.24 → 1.97 | 100% → 100% | 67.3 → 44.7 | 4.44 / 5.41 |
| canary | 4 | 639.7 | **1392.4** | 5.02 → 2.35 | 15.13 → 4.21 | 20.88 → 5.88 | 100% → 100% | 114.3 → 86.5 | 4.18 / 4.34 |
| canary | 16 | 640.8 | **849.9** | 9.71 → 17.1 | 95.82 → 24.08 | 156.95 → 69.51 | 100% → 100% | 127.5 → 100.7 | 5.2 / 5.03 |
| canary | 32 | 642.5 | **1154.6** | 19.32 → 22.2 | 238.6 → 49.59 | 362.66 → 167.8 | 100% → 100% | 128.8 → 121.4 | 5.79 / 7.82 |

Telemetry rows dropped: 0 in every cell of both runs. SLOs (concurrency ≤ 16): **6/6 cells met on both paths**.

- **Peak throughput 1,052 → 2,196 rps (2.1×)** champion only; canary mode 640 → 1,392 rps at c=4 (2.2×). On a quiet
  machine the old path is about 3× faster than in the loaded runs below, so absolute numbers on this laptop mostly
  measure the other work on it; the before/after ratio within one run pair is the result.
- Tail latency drops most where the old path queued: champion c=32 p95 94.8 → 19.9 ms, canary c=32 238.6 → 49.6 ms.
- Not every cell improved on every column: canary c=16 has a higher p50 after (17.1 vs 9.7 ms) and lower throughput
  than canary c=4 on the same path, with lower p95/p99. One 20 s cell on a shared laptop; reported as measured.
- **Finding 5 and #8 on merged code:** no canary was rolled back or paused on either path (old path 7 and 9 guardrail
  windows at c=16/c=32, compiled path 11 and 15, all `healthy` except one `insufficient_evidence`). The old path's
  champion model-call p95 in those windows was 53–102 ms (one c=32 window above #8's 100 ms
  `saturation_champion_p95_ms`), the compiled path's 0.1–3.4 ms. With no candidate timeouts, a slow champion alone
  does not pause anything, which is #8's design. So on this machine the load test does not reach the saturation
  that #8 handles; that path is exercised by chaos `gateway_saturation` and by the loaded A/B below, whose two
  old-path rollbacks met #8's pause condition.

In process, same session (`scripts/bench_scoring.py --story`,
[`bench-scoring-20261110-0817.json`](evidence/perf/bench-scoring-20261110-0817.json), load 6.7–7.5): one release
story (16,500 requests) **31.4 s → 3.3 s**, same outcomes on both paths. Single-row p50 / p95 (µs):

| model | sklearn `predict_proba` | compiled | p50 speed-up |
|---|---|---|---|
| v1-logreg | 418 / 482 | 6.9 / 8.1 | 60× |
| v2-hgb | 1,674 / 2,027 | 111 / 161 | 15× |
| v3-hgb-fresh | 1,639 / 1,964 | 106 / 153 | 15× |
| v4-hgb-segfilter | 1,620 / 1,865 | 102 / 136 | 16× |

## A/B under load: compiled single-row scorer (2026-11-09 23:47–23:55)

The gateway now scores one row with [`fastscore.py`](../src/spectral_canary/fastscore.py): a scorer built at model
load from the verified estimator that reproduces sklearn's `predict_proba` **bit for bit** (no retraining, no new
artifact; see "Why the scores cannot change" below). Both runs below use the same commit (`4a930db`: the scorer on
top of PR #7, before #8–#10 merged; the same scorer code is `6a463de` after the rebase onto main), the same
machine and the same `make loadtest` (8 cells × 20 s, closed loop, 4,000 seeded **SIMULATED** payloads). The only
difference is `SCW_FAST_SCORING=0` (the previous sklearn path) for the first run. Apple M5 Pro, 18 cores, 48 GB,
macOS 27.0.1, Python 3.13.16, one uvicorn process, `OMP_NUM_THREADS=1`, 1-minute load average **6.3–9.0** from other
work during every cell. Reports: [`docs/evidence/perf/`](evidence/perf/) (`loadtest-20261109-234757-sklearn`,
`loadtest-20261109-235207-compiled`).

| mode | conc. | rps before | rps after | p50 ms before → after | p95 ms before → after | p99 ms before → after | availability before → after | server CPU % before → after | load avg (1m) before / after |
|---|---|---|---|---|---|---|---|---|---|
| champion | 1 | 119.8 | **384.7** | 7.71 → 2.28 | 12.63 → 4.7 | 20.25 → 6.61 | 100% → 100% | 64.1 → 45.4 | 8.97 / 7.87 |
| champion | 4 | 328.1 | **784.5** | 10.9 → 4.58 | 21.37 → 7.51 | 36.52 → 11.68 | 100% → 100% | 101.8 → 81.5 | 8.16 / 7.96 |
| champion | 16 | 249.2 | **865.5** | 32.27 → 17.18 | 213.89 → 34.06 | 403.93 → 44.45 | 99.34% → 100% | 95.6 → 80.9 | 7.47 / 8.01 |
| champion | 32 | 143.8 | **764.9** | 182.27 → 39.49 | 379.39 → 58.28 | 510.94 → 78.28 | 96.16% → 100% | 98.2 → 89.1 | 8.48 / 8.06 |
| canary | 1 | 110.3 | **223.8** | 8.01 → 3.96 | 17.62 → 6.41 | 23.66 → 16.95 | 100% → 100% | 68.2 → 47.4 | 8.57 / 7.76 |
| canary | 4 | 191.8 | **598.5** | 17.78 → 6.05 | 44.56 → 9.5 | 60.26 → 12.53 | 100% → 100% | 111.6 → 91.3 | 6.69 / 8.24 |
| canary | 16 | 125.2 | **594.9** | 80.49 → 24.36 | 353.55 → 35.29 | 614.9 → 104.01 | 99.48% → 100% | 97.2 → 99.5 | 6.3 / 7.8 |
| canary | 32 | 171.6 | **768.7** | 143.11 → 36.53 | 417.96 → 54.17 | 575.51 → 198.07 | 99.22% → 100% | 114.2 → 108.8 | 8.44 / 6.85 |

Telemetry rows dropped: 0 in every cell of both runs. SLOs (concurrency ≤ 16): **6/6 cells met after, 4/6
before** (champion c=16 and canary c=16 missed availability; canary c=16 also missed p95).

What changed, read from the reports and the per-cell registries:

- **Peak throughput 328 → 866 rps (2.6×)** on one process, champion only; canary mode 192 → 599 rps at c=4 (3.1×).
  The "before" peak is below Run 1's 391 rps further down because this machine was busier (and the venv is
  Python 3.13); compare the two columns of this table with each other, not with Run 1.
- **Server CPU is no longer pinned at one core** in champion mode (81–89% at c ≥ 4). The closed-loop client runs
  on the same machine, so part of the remaining ceiling is the client, HTTP and the GIL rather than inference.
- **Finding 5 did not fire, and the two fixes work together.** On the old path the healthy v2 canary was rolled
  back in canary c=16 and c=32 ("candidate error rate 0.054 / 0.073 > budget 0.02": model calls queued past the
  500 ms timeout). On the compiled path the same cells ran 8 and 11 guardrail windows, all `healthy`, no rollback.
  The compiled scorer moves the saturation point up; the saturation-aware guardrail (PR #8, finding 5 below)
  decides what happens once the gateway does saturate. This A/B ran before #8 merged, so its "before" column shows
  the old verdict. Re-reading those two breach windows from the cells' registries against #8's rule: every
  candidate failure was a timeout (8 of 8 and 10 of 10, no other errors) and the champion's own model-call p95 in
  the same window was 220.5 ms and 312.9 ms, above the 100 ms `saturation_champion_p95_ms`, so on merged code both
  windows would be `inconclusive_saturated` and the release would **pause** instead of rolling back. That is
  derived from the recorded windows, not a new load-test run.

Same commit, in process (no HTTP), `scripts/bench_scoring.py --story`
([`bench-scoring-20261110.json`](evidence/perf/bench-scoring-20261110.json), load average 4.8–5.8): one full
release story (16,500 requests) took **96.3 s → 8.2 s**, with the same outcomes (A promoted, B blocked, C rolled
back). An earlier run of the same script at load ~7–8 gave 72.1 s → 13.6 s, and the event-log projection that the
determinism test compares was byte-identical between the two paths. Single-row model call, p50 / p95 (µs,
interleaved, 2,000 rows of the test split):

| model | sklearn `predict_proba` | compiled | p50 speed-up |
|---|---|---|---|
| v1-logreg | 2,390 / 4,513 | 37 / 67 | 65× |
| v2-hgb | 5,604 / 8,433 | 333 / 487 | 17× |
| v3-hgb-fresh | 5,919 / 8,222 | 333 / 468 | 18× |
| v4-hgb-segfilter | 4,686 / 8,494 | 281 / 453 | 17× |

### Why the scores cannot change

The old cost was wrappers, not math: for one row, `CalibratedClassifierCV.predict_proba` ran input validation, the
HGB-internal `ColumnTransformer` (joblib dispatch included), `make_known_categories_bitsets` and an OpenMP thread
query three times, then 40–60 tiny Cython tree calls per fold. v1 paid ~1 ms for its own `ColumnTransformer`.
`fastscore.py` keeps the arithmetic and drops the wrappers:

- HGB: the fitted `OrdinalEncoder` mapping, the tree walk of sklearn's `_predict_one_from_raw_data` (NaN → missing
  branch, categorical bitsets, `value <= threshold`), leaf values summed sequentially from `0.0 + baseline` in fit
  order, the calibrator's own `np.clip` + `interp1d`, and the fold mean in fold order.
- v1: the same numpy operations on arrays of the same `(1, k)` shapes, the same `(1, 16) @ coef_.T` call (so the
  same BLAS path) and `scipy.special.expit(out=...)`.
- At load, `build()` refuses any estimator of another shape and any scorer that differs from sklearn's single-row
  `predict_proba` by one bit on a probe set; that model then scores through sklearn as before. `/readyz` shows the
  path per version (`"scoring": {"v2-hgb": "compiled", ...}`) and `SCW_FAST_SCORING=0` turns it off.
- `tests/test_fastscore.py` serves all 8,000 test rows one at a time through the gateway's model pool. v2/v3/v4
  equal sklearn `predict_proba` bit for bit (batch and single-row agree for these models). v1 equals sklearn's
  single-row call bit for bit, which is what the gateway ran before. sklearn's own v1 batch output differs from its
  single-row output in the last bit on about a quarter of rows, because BLAS sums the two shapes in different
  orders, so the batch comparison for v1 goes through the committed 6-decimal hash. All four committed
  `test_prediction_sha256` values reproduce from the served scores, and `make train-fixtures` in an empty
  `SCW_HOME` reproduces them too.

## Run 1: baseline before the compiled scorer

### Run

| field | value |
|---|---|
| run | `var/runs/loadtest-20261109-001009/` (report.json + one raw JSONL of every request per cell, ~45 MB, gitignored) |
| date | 2026-11-09 00:10–00:22 local |
| command | `make loadtest` (= `scw loadtest --port 5130 --concurrency 1 4 16 32 --duration 20 --mode champion canary`) |
| gateway commit | `f01561016adf` (card-level guardrails) |
| machine | Apple M5 Pro, 18 cores, 48 GB RAM, macOS 27.0.1 (arm64), Python 3.12.15 |
| **machine load** | 1-minute load average **12.7–27.5** during the cells (other projects' builds and Unreal crash reporters were running). Treat the absolute numbers as a loaded laptop, not a quiet benchmark host |
| server | 1 uvicorn process, `OMP_NUM_THREADS=1`, fresh fixture registry per cell, SQLite WAL telemetry |
| client | closed loop: N asyncio clients × 1 keep-alive connection each, 20 s per cell after a 2 s warm-up, 4,000 seeded **SIMULATED** baseline payloads |
| modes | `champion`: v1 serves everything. `canary`: v2 at 10% (sticky by card) **plus a paired champion call** on every canary request |

### Results

| mode | conc. | requests | throughput (rps) | p50 ms | p95 ms | p99 ms | max ms | availability | server CPU % (mean) | RSS max MB |
|---|---|---|---|---|---|---|---|---|---|---|
| champion | 1 | 5,082 | 254.1 | 3.83 | 5.44 | 6.15 | 13.2 | 100% | 63.5 | 170.1 |
| champion | 4 | 7,821 | **390.9** | 10.08 | 14.87 | 17.44 | 55.0 | 100% | 106.5 | 170.4 |
| champion | 16 | 7,488 | 374.0 | 27.08 | 120.78 | 195.95 | 363.2 | 100% | 109.4 | 173.6 |
| champion | 32 | 7,337 | 365.9 | 72.73 | 166.52 | 237.80 | 473.2 | 100% | 109.0 | 177.8 |
| canary | 1 | 2,890 | 144.4 | 5.86 | 12.86 | 17.15 | 183.3 | 100% | 62.3 | 185.7 |
| canary | 4 | 3,742 | 186.9 | 18.46 | 43.57 | 58.15 | 202.6 | 100% | 92.3 | 181.4 |
| canary | 16 | 5,411 | 270.1 | 34.63 | 174.48 | 346.16 | 680.7 | 100% | 100.0 | 182.0 |
| canary | 32 | 5,792 | 288.5 | 81.97 | 258.66 | 483.58 | 763.0 | 99.98% (one 503) | 110.2 | 204.4 |

Telemetry rows dropped by the bounded writer: **0** in every cell.

### SLOs and measured compliance

| SLO (scope: concurrency ≤ 16, this machine) | objective | result |
|---|---|---|
| availability (HTTP 200 share) | ≥ 99.5% | **met in 6/6 cells** (100%) |
| client-observed p95 latency | ≤ 250 ms | **met in 6/6 cells** (worst 174.5 ms, canary c=16) |
| telemetry completeness | 0 dropped rows | **met in 6/6 cells** |

At c=32 (outside the SLO scope) canary p95 reached 258.7 ms and one champion call exceeded the 500 ms model
timeout, which returned a 503 with fail-closed semantics rather than a fabricated score.

### Interpretation: bottlenecks and trade-offs

1. **One Python process is CPU-bound at about one core.** Server CPU saturates at about 100–110% (one core
   plus I/O threads) from c=4 upward. Champion-only throughput plateaus at about 365–390 rps, and beyond that
   concurrency only adds queueing: at c=32, 366 rps × 72.7 ms (p50 as a proxy for the mean) ≈ 27 requests in flight (Little's law),
   which matches the offered load. The fix is horizontal (more workers/replicas, see `SYSTEM_ARCHITECTURE.md`),
   not a bigger model server. *(Update: most of that core was sklearn wrapper overhead per single-row call; the
   compiled scorer above raised the same-machine peak from 328 to 866 rps.)*
2. **The canary costs about 2× per request on the candidate arm.** Canary-mode throughput at c=1 is 57% of
   champion-only (144 vs 254 rps). The 10% candidate requests run the calibrated gradient-boosting ensemble
   (3 boosted models + isotonic) **and** the paired champion call that gives the guardrail its statistical
   power (ADR-5). That's a deliberate cost for evidence, and the number to weigh when choosing the canary
   fraction.
3. **Memory is flat:** about 170–205 MB RSS with 2 models loaded; it doesn't grow with load.
4. **SQLite telemetry is not the bottleneck at this rate.** The batched writer (256 rows / 200 ms) dropped
   nothing at about 390 rps.
5. **Finding: the guardrail cannot yet tell "candidate broken" from "gateway saturated".** In canary c=16 and
   c=32, candidate model-call p95 rose to 339–429 ms (queueing behind the CPU-bound process; the champion's
   own calls were at 161–196 ms). Some candidate calls crossed the 500 ms timeout, the candidate error rate
   reached 2.9–6.4% (> the 2% budget), and the controller **rolled back a healthy v2**. The behavior is
   fail-safe (removing the canary also removes its extra paired-call load), but the postmortem would blame the
   model for a capacity problem. **Status: fixed in code on 2026-11-09 (PR #8), verified by fault injection (chaos
   `gateway_saturation` and four tests). The load test on merged code (2026-11-10, quiet machine, top of this file)
   did not saturate the gateway on either scoring path, so it neither confirms nor contradicts the pause.** The compiled-scorer
   A/B above shows both halves: the faster path stays healthy at c=16/c=32, and the old path's two rollbacks there
   meet exactly #8's pause condition (all timeouts, champion p95 220–313 ms > 100 ms). The fix as recommended: a saturation-aware guardrail that, when
   the champion lane is also over its latency budget in the same window, records `inconclusive: gateway
   saturated` and **pauses** instead of rolling back.
6. **Finding (fixed): request-level statistics on clustered traffic.** The first load test run
   (`loadtest-20261109-000327`, kept as evidence) produced a false *flag-rate* rollback at canary c=32. The
   candidate's 146 window requests came from only 38 cards (the load generator reuses payloads per card), and
   the CI treated them as independent. Guardrails now count one observation per card; regression test
   `test_guardrail_counts_cards_not_requests`. In the run above, no flag-rate breach occurred.

## Reproduce

```bash
make train-fixtures
make loadtest           # ~3.5 min (8 cells x 20 s plus startup); dedicated gateway on :5130 per cell
python -c "import json;r=json.load(open(sorted(__import__('glob').glob('var/runs/loadtest-*/report.json'))[-1]));print(r['slo_compliance'])"
SCW_FAST_SCORING=0 make loadtest   # the previous sklearn scoring path, for an A/B on the same machine
uv run --frozen python scripts/bench_scoring.py --story   # single-row and in-process story, both paths
```

Comparing runs is only meaningful with similar machine load; every report records the load average before
and after each cell.
