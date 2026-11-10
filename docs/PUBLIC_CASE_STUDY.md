# Case study: catching a model regression that passed every offline check

**Spectral Canary Works**, an ML release and reliability lab, built and measured on one laptop, November 2026.

## Status (what is real, and where)

| part | status |
|---|---|
| 4 trained models, gateway, registry, release controller, guardrails, rollback | **live-local**: runs natively on macOS; 49 automated tests + 10 chaos scenarios pass |
| control-room UI | **live-local**, rendering only recorded data |
| Docker stack (gateway, UI, simulated traffic, Prometheus, Grafana) | **local Docker**, brought up and torn down cleanly |
| OpenTofu `infra/local` | **applied and destroyed** against local Docker |
| OpenTofu `infra/aws` | **implemented, validated, NOT provisioned**: $0 incurred |
| GitHub Actions CI | **enabled** 2026-11-09: 5 jobs on every pull request; the same checks run locally with `make ci` |
| traffic | **simulated** (seeded) and labeled as such everywhere; data is synthetic |

## The problem

A fraud model retrained on fresher data looks fine offline: PR-AUC 0.600 vs the champion's 0.620, within a
0.03 non-inferiority margin, and no slice regresses past tolerance. But its training labels were snapshotted
before slow chargebacks on high-value card-not-present transactions matured, so it learned that those are safe.
They make up about 3% of offline frauds, so the defect was invisible offline.

## What the system did (recorded run `demo-20261109-001407`, real HTTP)

1. **Offline gate:** passed v3 and correctly **blocked** another bad candidate (v4) on 7 explicit checks.
2. **Shadow:** 600 identical requests scored by both models, with no effect on responses. Score JS divergence
   0.011, decision disagreement 0.5%.
3. **10% sticky canary:** two healthy windows on normal traffic.
4. **Simulated surge** of high-value online purchases. Feature drift went "major" (PSI 2.3), but that was
   recorded as an alert only. In window 4, paired labeled evidence showed the candidate catching **65 of 112**
   frauds it served, where the champion, scored on the same requests, would have caught **83** (exact McNemar
   p = 0.0015).
5. **Automatic rollback:** one compare-and-swap of the deployment pointer in the same transaction as its audit
   event. **50/50** verification predictions were served by the restored champion. The postmortem was
   generated from the hash-chained event log.

## What measurement taught me (two bugs my own tests found)

- **Clustered traffic broke my statistics.** My first load test rolled back a *healthy* model: 146 canary
  requests came from 38 repeat cards, and a request-level confidence interval treated them as independent.
  Guardrails now count one observation per card, and there's a regression test.
- **The guardrail used to mistake saturation for a bad model.** Past about 370 requests/s on one Python process,
  candidate calls queue past the 500 ms timeout and trip the error budget. Now, when the champion is slow in the
  same window and the failures are timeouts, the release pauses instead of rolling back (chaos
  `gateway_saturation`); the load test itself has not been rerun.

## Numbers (measured; machine: Apple M5 Pro laptop under a 1-min load average of 13–27 from other work)

- Held-out (synthetic, 8,000 rows / 487 frauds): v2 PR-AUC **0.620** [0.570–0.662], F1 0.598, ECE 0.005.
- Gateway throughput: about **390 req/s** peak (one process, champion only, c=4), p95 14.9 ms. With a canary:
  187 req/s at c=4, p95 43.6 ms. The canary costs a paired champion call on purpose.
- SLOs at concurrency ≤ 16: availability ≥ 99.5% and p95 ≤ 250 ms were **met in 6/6 cells**, with 0 dropped
  telemetry rows.
- Reproducibility: the Linux Docker build retrains every model and reproduces the macOS dataset hash and
  test-prediction hashes exactly.

## Stack

Python 3.12, scikit-learn (skops-safe artifacts), FastAPI, SQLite (WAL, triggers, hash chain), Prometheus
client, React + TypeScript (Vite), Docker, OpenTofu, Grafana.

## Resume bullets (each backed by a command and an artifact in `resume_evidence/metrics.json`)

- Built a local ML release platform (FastAPI, scikit-learn, SQLite) with an offline non-inferiority gate,
  shadow scoring, a 10% sticky canary and paired McNemar guardrails. In a recorded run it automatically rolled
  back a regressed model that had passed offline checks (65 vs 83 of 112 labeled frauds caught, p = 0.0015),
  verified recovery with 50/50 live predictions, and generated the postmortem from a hash-chained audit log.
- Load-tested and fault-injected the inference gateway: 10/10 seeded chaos scenarios (timeouts, partial outages,
  tampered artifacts, crash recovery, concurrent operators), and about 390 req/s on one CPU process with p95
  ≤ 250 ms SLOs met at concurrency ≤ 16. The tests surfaced and fixed a clustered-sample statistics bug that
  caused a false rollback. Packaged with non-root Docker, Prometheus/Grafana and OpenTofu (cloud path
  validated, not deployed).
