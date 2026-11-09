# Architecture (code level)

```
                 configs/models/*.toml          configs/release_policy.toml
                          │                                │
  data.py ──▶ train.py ──▶ var/artifacts/<v>/{model.skops, manifest.json, MODEL_CARD.md}
  (seeded)    (fit/val/test discipline)        │  model_artifact_v1 contract
                                               ▼
                    registry.py ──▶ store.py (SQLite, WAL)
                    validate + sign      ├─ model_versions   (immutable: triggers)
                                         ├─ events           (append-only, hash chain)
                                         ├─ deployment       (pointer, CAS on generation)
                                         ├─ releases         (state machine row)
                                         └─ requests/feedback (telemetry, delayed labels)
                                               ▲
   HTTP ──▶ api.py ──▶ serving.InferenceService ──┤  snapshot per request (in-flight safe)
           FastAPI     ├ schema.validate_record   │  router: sha256(key) bucket < fraction
                       ├ ModelPool (verified load)│  shadow (after response decided)
                       ├ bulkhead executors + timeout  canary + champion fallback
                       └ TelemetryWriter (bounded queue → batched inserts)
                                               │ every N routed requests (logical clock)
                                               ▼
                    controller.Controller ──▶ gates.py (offline), monitor.py (shadow, guardrails, drift)
                    transitions, approve,       │ breach → rollback/pause per policy
                    rollback, recover, tick     ▼
                                       postmortem.py (from events) → var/runs/<run>/reports/
   scenarios.py / chaos.py / traffic.py ── drive the same code paths (in-process or HTTP drivers)
   web/ (React + TS) ── reads /v1/* + SSE /v1/events/stream; renders recorded data only
```

## Modules

| module | responsibility |
|---|---|
| `schema.py` | the feature contract: validation (types, ranges, categories, no extras) and encoding |
| `data.py` | seeded generator, label mechanism, shift scenario, deterministic split, dataset manifest |
| `train.py` | model families (logistic regression; isotonic-calibrated HGB), config-declared data changes, threshold from val only, metrics on frozen test, manifest + model card |
| `artifacts.py` | skops save (atomic, read-only), type allowlist, hash-verified load, HMAC manifest signing |
| `store.py` | SQLite schema with triggers; event hash chain; CAS pointer swap that writes its audit event in the same transaction |
| `registry.py` | register = validate contract + schema hash + real load + sign; `verify_registry` re-hashes everything |
| `serving.py` | model pool, deterministic router, shadow/canary execution, timeouts, bulkhead, fault injector (simulated), telemetry writer |
| `gates.py` | offline gate checks with observed/limit/reason |
| `monitor.py` | shadow summary, canary window evaluation (flag rate CI, paired McNemar, latency ratio, error budget), PSI drift |
| `controller.py` | the state machine: propose, stage, shadow, canary, evaluate, approve, pause, rollback, recover, tick |
| `postmortem.py` | builds the postmortem from events + telemetry (no hand-written numbers) |
| `api.py` | FastAPI routes, auth/roles, SSE with bounded backpressure, Prometheus `/metrics` |
| `telemetry.py` | bounded-label Prometheus metrics, process CPU/RSS, redacted JSON logs |
| `traffic.py`, `scenarios.py`, `chaos.py`, `drivers.py` | seeded simulated traffic with delayed labels; the demo story and failure scenarios with expected outcomes |
| `loadtest.py` | closed-loop HTTP load generator with raw per-request output |

### LLM triage lane (`src/spectral_canary/llm/`, see `LLM_RELEASE_LANE.md`)

| module | responsibility |
|---|---|
| `llm/alerts.py` | seeded alert generator, rule-derived labels (decision table), gold set + smoke subset, simulated alert traffic |
| `llm/bundles.py` | prompt bundles (prompt + model + weights digest + params) compiled, hashed, signed, registered in `model_versions` |
| `llm/client.py` | Ollama `/api/chat` backend with JSON-schema structured output; `ScriptedBackend` test double |
| `llm/graders.py`, `llm/evals.py` | code graders; k-try regression suite with pass@1 / pass^k, segment recall, cost, latency |
| `llm/gates.py` | offline gate for bundles (same report shape as `gates.py`) |
| `llm/service.py` | triage gateway core: snapshot, sticky routing, shadow, canary with champion fallback and paired champion call, fail closed |
| `llm/monitor.py` | LLM shadow summary, canary window (online grading, latency/cost SLOs, McNemar), input drift (PSI) |
| `llm/controller.py` | `LLMController(Controller)`: only the evidence hooks; transitions, CAS, approval and rollback are inherited |
| `llm/store.py` | `LLMStore(Store)`: same registry/events/pointer tables + `llm_requests`, `llm_feedback`, in a separate SQLite file |
| `llm/otel.py` | OpenTelemetry GenAI client metrics into the gateway's Prometheus registry |
| `llm/api.py`, `llm/scenarios.py`, `llm/commands.py` | `/v1/llm/*` routes; HTTP/in-process drivers and the recorded story; `scw llm-*` commands |

## Key invariants and where they are enforced

- *Only verified bytes serve:* `ModelPool.get` → `artifacts.load_verified` (hash + allowlist) and an HMAC check.
- *The pointer and the audit log never disagree:* `Store.swap_deployment` updates both in one transaction.
- *Shadow cannot change a response:* the response fields are fixed before the shadow call; the shadow result
  is written only to telemetry.
- *In-flight requests survive swaps:* each request holds one immutable `Snapshot`; models are never unloaded
  while referenced (`test_in_flight_requests_survive_pointer_swaps`).
- *Rollback is idempotent and verified:* a duplicate request records `rollback.duplicate_ignored`; recovery is
  confirmed by real predictions through `service.predict` plus readiness.

See `SYSTEM_ARCHITECTURE.md` for runtime topology, scaling and deployment.
