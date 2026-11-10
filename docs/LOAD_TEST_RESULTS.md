# Load test results (genuine measurements only)

## Run

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

## Results

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

## SLOs and measured compliance

| SLO (scope: concurrency ≤ 16, this machine) | objective | result |
|---|---|---|
| availability (HTTP 200 share) | ≥ 99.5% | **met in 6/6 cells** (100%) |
| client-observed p95 latency | ≤ 250 ms | **met in 6/6 cells** (worst 174.5 ms, canary c=16) |
| telemetry completeness | 0 dropped rows | **met in 6/6 cells** |

At c=32 (outside the SLO scope) canary p95 reached 258.7 ms and one champion call exceeded the 500 ms model
timeout, which returned a 503 with fail-closed semantics rather than a fabricated score.

## Interpretation: bottlenecks and trade-offs

1. **One Python process is CPU-bound at about one core.** Server CPU saturates at about 100–110% (one core
   plus I/O threads) from c=4 upward. Champion-only throughput plateaus at about 365–390 rps, and beyond that
   concurrency only adds queueing: at c=32, 366 rps × 72.7 ms (p50 as a proxy for the mean) ≈ 27 requests in flight (Little's law),
   which matches the offered load. The fix is horizontal (more workers/replicas, see `SYSTEM_ARCHITECTURE.md`),
   not a bigger model server.
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
   model for a capacity problem. **Status: fixed in code on 2026-11-09, verified by fault injection (chaos
   `gateway_saturation` and four tests), load test not rerun yet.** The fix as recommended: a saturation-aware guardrail that, when
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
make loadtest           # ~12 min on this machine; dedicated gateway on :5130 per cell
python -c "import json;r=json.load(open(sorted(__import__('glob').glob('var/runs/loadtest-*/report.json'))[-1]));print(r['slo_compliance'])"
```

Comparing runs is only meaningful with similar machine load; every report records the load average before
and after each cell.
