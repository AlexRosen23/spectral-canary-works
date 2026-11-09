# System architecture (runtime, scale, deployment)

## Status legend

| tag | meaning |
|---|---|
| **LIVE-LOCAL** | runs on this Mac, verified by tests or a recorded run |
| **LOCAL-DOCKER** | runs in the docker compose stack on this Mac, verified |
| **IaC-VALIDATED** | OpenTofu `fmt` + `validate` pass; local Docker resources can be applied at $0 |
| **NOT PROVISIONED** | code exists for a cloud path; nothing has been created in any cloud account |

## Topology

```
 ┌──────────────── this Mac (127.0.0.1 only) ───────────────────────────────────────────┐
 │                                                                                       │
 │  browser ──▶ web (Vite/React; nginx-unprivileged in docker)  :5121                    │
 │      │  REST + SSE                                                                     │
 │      ▼                                                                                 │
 │  gateway (uvicorn, 1 process)  :5120 ──▶ SQLite WAL (registry, events, telemetry)     │
 │      │   │                                 var/runs/<run>/registry.sqlite              │
 │      │   └─▶ artifacts (read-only, sha256-verified) var/artifacts/<version>/          │
 │      └─ /metrics ◀── Prometheus :5122 ◀── Grafana :5123 (anonymous viewer)            │
 │  traffic generator (SIMULATED, labeled) ──▶ gateway                                   │
 └───────────────────────────────────────────────────────────────────────────────────────┘
 Optional, NOT PROVISIONED: infra/aws → ECR + 1× t4g.micro (Docker, SSM, IMDSv2) + budget alert
```

| component | status | how to run |
|---|---|---|
| gateway + registry + controller | LIVE-LOCAL | `make serve` |
| control-room UI | LIVE-LOCAL | `make web` |
| demo / chaos / load test | LIVE-LOCAL (recorded runs in `var/runs/`) | `make demo`, `make chaos-replay`, `make loadtest` |
| compose stack (gateway, web, traffic, Prometheus, Grafana) | LOCAL-DOCKER | `make up` / `make down` |
| `infra/local` (Docker provider) | IaC-VALIDATED | `cd infra/local && tofu apply` (local Docker only) |
| `infra/aws` | IaC-VALIDATED, **NOT PROVISIONED** | `scripts/aws_deploy.sh` (refuses without `SCW_CONFIRM_BILLABLE=yes`) |
| GitHub Actions CI | defined, **not enabled** (owner decision) | `make ci` runs the same checks locally |

## Request path and its costs

1. Pydantic parse → `validate_record` → encode (microseconds).
2. Snapshot read: one `SELECT` of the pointer row; the cached snapshot is reused unless the generation changed.
3. Model call in a bounded executor with a 500 ms timeout (the lane depends on the arm).
4. On canary-arm requests, a second (paired) champion call for evidence.
5. Telemetry row → bounded in-memory queue → background batch insert every 200 ms or 256 rows.
6. Every 1,500 routed requests: flush + guardrail evaluation (synchronous on that request).

Measured costs and bottlenecks are in `LOAD_TEST_RESULTS.md`.

## Scaling path (decided, not built)

- **More replicas:** the snapshot already re-reads the pointer generation from the shared store on every
  request, so N stateless gateway replicas behind a load balancer would serve consistently. Two things must
  move first: (a) the guardrail window counter (now in-process) into the store, as an atomic counter or a
  single controller leader; (b) SQLite to Postgres for multi-writer telemetry, or ship telemetry to a queue
  (Kafka/SQS) and a columnar store.
- **Bigger models:** move inference to a model server (Triton, Ray Serve or KServe). The gateway keeps routing,
  guardrails and audit. The contract (`model_artifact_v1`) already separates the artifact from the gateway.
- **Kubernetes** becomes worthwhile when there are multiple services, autoscaling needs or a platform team.
  For one CPU model behind one gateway it adds cost and moving parts. See `ARCHITECTURE_DECISIONS.md` ADR-2.
