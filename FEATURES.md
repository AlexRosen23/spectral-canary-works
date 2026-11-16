# Spectral Canary Works: features

Spectral Canary Works is a small model-release system that runs on one laptop. Four scikit-learn fraud-scoring models, trained on a seeded synthetic dataset, move through registration, an offline gate, shadow scoring, a 10% sticky canary and promotion, behind a FastAPI gateway that serves real predictions. When a candidate fails under a **simulated** shift in traffic, the controller detects it from paired, labeled evidence, rolls back with one atomic pointer swap, checks recovery with real predictions and writes a postmortem from its own hash-chained audit log. A second lane puts an LLM component (a fraud-alert triage prompt on a local `llama3.2:3b` through Ollama) through the same registry, controller, audit chain and rollback path. It is for engineers and reviewers who want to see, and rerun, the work between "the model scores well" and "the model is safe to serve".

**This is the source-of-truth features list.** Every PR that adds, changes, removes or switches off something a person can see or use updates this file in the same PR (see "Keeping this file true").

- **Repo:** AlexRosen23/spectral-canary-works (public) · **Folder:** `~/Desktop/Claude Projects/Spectral Canary Works`
- **Status:** working, local-first; nothing is deployed to any cloud · **Version:** `0.1.0` (`pyproject.toml`; no tagged release found) · **`main`:** `9ddbd7d` (2026-11-15)
- **Last updated:** 2026-11-15, written from the code, README, docs and tests.

**Status words** (a feature with no status word is live):
| Word | Means |
| --- | --- |
| *Live* | on `main` and working locally (nothing here is deployed) |
| *Off by default* | built, behind a flag or setting |
| *Waiting on owner* | built, blocked on an owner action (approval to spend money) |
| *Planned* | named as a next step or upgrade in the docs, not built |

## At a glance
| | |
| --- | --- |
| Stack | Python 3.12 (uv), FastAPI, scikit-learn, skops, SQLite (WAL); Ollama, promptfoo, OpenTelemetry; React + Vite + TypeScript UI; Docker, Prometheus, Grafana; OpenTofu; GitHub Actions |
| Ports | gateway + UI `5120` (`make start`), Vite dev UI `5121`, load test `5130`, LLM demo `5141`, ML demo `5150`; Docker stack `5120` to `5123` |
| Code size | about 10,300 lines of Python in `src/spectral_canary/` (ML lane plus `llm/`), about 1,470 lines of TypeScript/CSS in `web/src/` |
| Models | 4 ML versions (`v1-logreg`, `v2-hgb`, `v3-hgb-fresh`, `v4-hgb-segfilter`); 5 LLM prompt bundles |
| Dataset | 40,000 synthetic rows; test split 8,000 rows with 487 frauds |
| API | 37 HTTP routes in the ML gateway plus 16 under `/v1/llm` (counted from decorators) |
| CLI | `scw` with 12 subcommands |
| Tests | 80 Python test functions (99 collected with parametrization per the README), 7 web tests |
| PRs | 12 merged (#1 to #12), 0 open |
| Data | all synthetic; traffic is a labeled simulation |

## Contents
1. What it is and who it's for
2. How to run it
3. Architecture at a glance
4. Features
5. Integrations
6. Data and privacy
7. Tests and evidence
8. Known limits and gaps
9. Change log
- Keeping this file true

## 1. What it is and who it's for

- **Who:** engineers and reviewers (including AI-engineer reviewers) who want a worked, rerunnable answer to three questions: how do you decide a model is safe to ship, how do you know it is degrading once live, and how do you recover and prove you did.
- **What it does best:** catches a regression that passes every offline check, rolls back automatically, and leaves evidence for each step. The recorded run rolled back `v3` on paired evidence: 65 of 112 labeled frauds caught against 83 for the champion on the same requests (exact McNemar p = 0.0015).
- **Honest scope:** the data is synthetic and the defect is built on purpose. "Simulated" means the request traffic is seeded and generated; the predictions, routing, statistics and rollbacks it drives are real executions of this code.
- **Principles the code follows:**
  - Ship on non-inferiority, not on "better by one number".
  - Compare both models on the same requests (paired test).
  - Make no quality claim without enough labels (below 20 labeled frauds per arm the window is `insufficient`).
  - Count one observation per card, not per request.
  - In the ML lane drift raises an alert, never a rollback.
  - Rollback is a pointer swap, not a redeploy.
  - Only verified bytes serve; failure is closed (503, never a made-up score).
  - Every decision leaves evidence.

## 2. How to run it

**Requirements:** macOS or Linux, [uv](https://docs.astral.sh/uv/) (installs Python 3.12), Node 22 and npm, `make`. Docker is optional (`make up`, `make docker-build`, `make iac-validate`, `make workflow-lint`). Ollama with `llama3.2:3b` is needed only for the LLM lane's live commands (`make llm-demo` also needs `qwen2.5-coder:3b`). No cloud account, API key or GPU.

```bash
make setup            # uv sync + npm ci; local operator tokens and a signing key in var/secrets (gitignored)
make train-fixtures   # seeded dataset + v1..v4; fails if any hash differs from manifests/
make eval             # held-out metrics table for every version
make demo             # the whole release story over real HTTP on its own gateway (:5150)
make start            # gateway + built control room on http://127.0.0.1:5120
```

**Expected `make demo` result:** `rel-0001` v1 to v2 promoted, `rel-0002` v4 blocked by the offline gate, `rel-0003` v2 to v3 rolled back automatically in canary window 4, 50/50 verification predictions served by v2, audit chain and registry verified. About 2.5 minutes on a loaded laptop (README, 2026-11-09).

**Make targets**
| command | what it does |
| --- | --- |
| `make setup` | install Python and web dependencies, create local secrets |
| `make train-fixtures` | generate the dataset, train v1 to v4, check hashes against `manifests/` |
| `make eval` | held-out metrics for every trained version |
| `make test` / `make test-fast` | all Python tests with xdist / unit tests only (skips the e2e marker) |
| `make start` | builds what is missing, then gateway and control room on `127.0.0.1:5120` |
| `make serve` / `make web` / `make web-build` | gateway on 5120 / Vite dev UI on 5121 / production UI build |
| `make demo` | the ML release story over real HTTP on its own gateway |
| `make chaos-replay` | 11 seeded failure scenarios with expected outcomes |
| `make verify` | re-hash artifacts, check manifest signatures and the audit hash chain |
| `make loadtest` | real HTTP load test on a dedicated gateway (:5130), results saved under `var/runs/loadtest-*` |
| `make lint`, `make typecheck`, `make contract-check` | ruff, mypy, manifests vs the JSON Schema |
| `make ci` | lint, mypy, fixtures, contract, gold set, tests, chaos, web build, as CI runs them |
| `make up` / `make down` | Docker stack with simulated traffic, Prometheus and Grafana / full teardown |
| `make docker-build` | build the image (retrains and re-verifies hashes inside) |
| `make iac-validate` | OpenTofu `fmt` and `validate` for `infra/local` and `infra/aws` (no plan, no apply) |
| `make workflow-lint` | actionlint on `.github/workflows` (Docker) |
| `make llm-gold` | check the triage gold set against its seeded generator |
| `make llm-eval B=<bundle>` | offline suite for one prompt bundle (150 cases x 3 tries, real local model calls) |
| `make llm-gate` | promptfoo regression suite (pinned 0.125.1), fails under a 90% pass rate |
| `make llm-gate-relative` | same suite for a base ref and this checkout on one machine; fails on a drop over 2.2 points |
| `make llm-demo` | LLM release story over real HTTP on :5141 with real local model calls (about 2 hours when every suite runs live) |
| `make clean-state` | remove generated local state; asks first |

**`scw` CLI** (`src/spectral_canary/cli.py`, `src/spectral_canary/llm/commands.py`)
- `scw setup`, `scw train-fixtures [--update-manifests]`, `scw eval`
- `scw demo [--port] [--in-process] [--scale]` (a scale below 0.67 is refused with a reason: the shadow phase would fall under the 400-request minimum)
- `scw chaos-replay [--only <scenario>...]`
- `scw verify`
- `scw serve [--host] [--port] [--bootstrap-fixture RUN_NAME]`
- `scw traffic [--base-url] [--rps] [--requests] [--scenario]` (labeled simulated traffic to a running gateway)
- `scw loadtest [--port] [--concurrency ...] [--duration] [--mode champion canary] [--base-url]`
- `scw llm-gold [--write]`, `scw llm-eval`, `scw llm-demo`

**Environment variables (names only):** `SCW_HOME`, `SCW_DB`, `SCW_ENABLE_CHAOS`, `SCW_API_PORT`, `SCW_DEMO_PORT`, `OMP_NUM_THREADS`, `VITE_API_URL`, `SCW_CONFIRM_BILLABLE`, `SCW_LLM_DB`, `SCW_OLLAMA_URL`, `SCW_LLM_DEMO_PORT`, `SCW_FAST_SCORING` (set to `0` to use the sklearn path), `LLM_GATE_BASE`, `LLM_GATE_THRESHOLD` (`CONTRIBUTING.md`, `Makefile`, README).

## 3. Architecture at a glance

Data flow: seeded generator, then training (`configs/models/*.toml`), then a skops artifact and signed manifest, then registry verification into a SQLite (WAL) store that holds models, the deployment pointer, events and telemetry. Clients send `POST /v1/predict` to the FastAPI gateway, which routes on one immutable snapshot of the pointer to the champion or the candidate. The controller state machine uses `gates.py` and `monitor.py` to decide, swaps the pointer with compare-and-swap and writes the audit event in the same transaction. The LLM lane adds `llm/service.py` (bundle router, JSON contract, fallback) in front of Ollama and reuses the controller.

| path | what lives there |
| --- | --- |
| `src/spectral_canary/` | data, train, artifacts, registry, store, serving, fastscore, gates, monitor, controller, postmortem, api, auth, telemetry, traffic, chaos, loadtest, scenarios, drivers, cli |
| `src/spectral_canary/llm/` | alerts and gold set, bundles, graders, evals, gates, service, monitor, controller hooks, policy, otel, postmortem, api, commands |
| `web/` | React + TypeScript control room (hand-written SVG charts, SSE feed) |
| `configs/` | `models/*.toml`, `release_policy.toml` (every threshold), `llm/` prompts, bundles, LLM policy |
| `evals/triage/` | triage gold set (150 cases) and the promptfoo suite |
| `manifests/` | committed dataset and model manifests (hashes, metrics); artifacts are regenerated, not committed |
| `contracts/` | `model_artifact_v1.schema.json` |
| `deploy/`, `infra/local`, `infra/aws` | Prometheus and Grafana config; OpenTofu for local Docker and for AWS (not provisioned) |
| `docs/` | 19 design, policy, runbook and evidence documents; `docs/evidence/` and `docs/media/` |
| `resume_evidence/metrics.json` | every claim with its command, artifact, commit and status |

## 4. Features

### 4.1 Data, training and the model registry
- **Seeded synthetic dataset**: 40,000 rows from a documented generator; committed hashes in `manifests/dataset_manifest.json` make a rerun checkable. `src/spectral_canary/data.py`
- **Four model versions** with immutable configs: `v1-logreg` (logistic regression, champion at bootstrap), `v2-hgb` (isotonic-calibrated gradient boosting), `v3-hgb-fresh` (degraded: immature label snapshot, 77 positives relabeled 0), `v4-hgb-segfilter` (degraded: a filter dropped 819 high-value card-not-present rows). `configs/models/`, `src/spectral_canary/train.py`
- **Held-out evaluation** with 95% bootstrap intervals; thresholds chosen on validation only (max F1). `make eval`
- **Signed, hash-verified artifacts**: skops, not pickle, with a reviewed type allowlist; manifests follow the `model_artifact_v1` JSON Schema. `artifacts.py`, `registry.py`, `contracts/`
- **Immutable model rows and append-only events**: SQLite triggers enforce both. `store.py`
- **Reproducibility check**: committed test-prediction hashes; `train-fixtures` refuses to retrain an existing version with a different config. `make train-fixtures`

### 4.2 Release pipeline (ML lane)
- **Offline gate**: integrity, schema, reproducibility (200 rows re-scored), golden ordering, latency, calibration (ECE), non-inferiority on PR-AUC, recall and F1, and per-slice recall (slices under 25 positives are reported as `insufficient_support`). `gates.py`, `configs/release_policy.toml`
- **Shadow scoring**: the champion answers; the candidate scores afterwards and is only recorded. Needs 400 requests, error rate up to 1%, score JS divergence up to 0.20, decision disagreement up to 15%. A test checks shadow never changes a response.
- **10% sticky canary**: routing by `sha256(salt:request_key) mod 10000`, so a card always lands in one arm; `X-Model-Version` response header. `serving.py`
- **Paired scoring**: on candidate requests the champion is also scored, so quality is compared on identical requests.
- **Per-arm bulkheads and a 500 ms model timeout**: a slow candidate cannot delay champion answers; on timeout the champion answers (`X-Fallback`).
- **Guardrail windows every 1,500 routed requests** (a logical clock so replays are deterministic): error budget, latency vs champion, flag-rate ratio (Newcombe interval), paired exact McNemar recall test, all counted per card. `monitor.py`
- **Promotion** after 2 healthy windows plus a release-manager approval; creator cannot approve (self-approval refused with 403). `controller.py`, `auth.py`
- **Pause and resume**: `POST /v1/releases/{id}/pause`; a saturation pause takes the candidate off traffic without blaming the model.
- **Stale and stuck handling**: no new routed traffic for 900 s means evidence is stale and promotion is blocked; a release with no heartbeat for 1,800 s reverts to the champion.
- **Policy fingerprint**: each release records the SHA-256 of the policy file.

### 4.3 Rollback and postmortem
- **Atomic, idempotent rollback**: compare-and-swap on the deployment generation, in the same transaction as the release state change and the `rollback.executed` event; in-flight requests finish on the snapshot they started with.
- **Verified recovery**: 50 real predictions must all be served by the champion and readiness must pass (`rollback.verified`).
- **Generated postmortem**: written from the event log, not by hand; impact counts cards, both directions, with labels known at the rollback. `postmortem.py`, `docs/INCIDENT_POSTMORTEM.md`
- **Saturation-aware guardrail**: when the champion's own model-call p95 is over 100 ms in the same window and every failure over budget is a timeout, the window is `inconclusive_saturated` and the release pauses instead of rolling back. Real errors and quality breaches still roll back.
- **Drift as an alert only**: PSI on features and Jensen-Shannon on scores raise `drift.alert`; no traffic moves. `GET /v1/drift`

### 4.4 Serving and the compiled scorer
- **Typed predict API** with schema validation (422 on a bad request) and 503 when no model can serve. `POST /v1/predict`
- **Delayed labels**: `POST /v1/feedback`.
- **Bit-identical compiled single-row scorer**: no retraining; checked at load against sklearn, with fallback to sklearn; tests serve all 8,000 test rows bit-identically. `fastscore.py`, `tests/test_fastscore.py`
- **Native thread cap** (`OMP_NUM_THREADS=1` plus `threadpoolctl`) to avoid OpenMP spin-waiting. `serving.limit_native_threads`
- **Local operator roles**: viewer, operator (shadow, canary, pause, rollback, chaos), release_manager (approve); random bearer tokens, hashes stored. `auth.py`

### 4.5 ML-lane API (37 routes)
- **Health and ops**: `/healthz`, `/readyz`, `/metrics` (Prometheus).
- **Serving**: `/v1/predict`, `/v1/feedback`, `/v1/schema`.
- **Registry**: `/v1/registry/models`, `/v1/registry/models/{version}`, `/v1/registry/verify`, `/v1/deployment`.
- **Releases**: list, get, `/evaluations`, `/shadow` (GET and POST), create, `/stage`, `/canary`, `/evaluate`, `/approve`, `/pause`, `/rollback`.
- **Chaos** *(off by default)*: `POST` and `DELETE /v1/chaos/faults`; return 403 unless `SCW_ENABLE_CHAOS=1`.
- **Evidence and telemetry**: `/v1/events`, `/v1/events/stream` (SSE), `/v1/requests/recent`, `/v1/traffic/series`, `/v1/drift`, `/v1/overview`.
- **Runs**: `/v1/runs`, `/v1/runs/{run_id}`, `/v1/runs/{run_id}/postmortem`.
- **Read-only contract** for dashboards: `/health`, `/capabilities`, `/runs`, `/runs/{run_id}`. `scripts/check_contract.py`

### 4.6 Chaos suite (11 scenarios)
`make chaos-replay`; each scenario is also its own pytest test. `scenarios.py`, `chaos.py`
- **schema_break**, **distribution_shift** (alert, no traffic moved), **degraded_predictions**, **latency_spike**, **candidate_timeout** (champion answers, still rolls back), **gateway_saturation** (pauses, then resumes), **partial_outage** (fails closed with 503), **corrupted_artifact** (never becomes champion), **repeat_rollback** (idempotent), **restart_recovery**, **promotion_race** (approve vs rollback).

### 4.7 Control room (web UI)
`web/src/App.tsx`, `LlmLane.tsx`, `charts.tsx`; renders only recorded API data.
- **Release track**, **traffic** and **quality** charts (hand-written SVG), **drift**, **compare**, **registry**, **postmortem** and a **reproducibility drawer**.
- **Incident replay** with a cursor; the evidence panel shows the latest evaluation at or before the cursor, or says there is none yet.
- **Live event feed** over SSE.
- **LLM lane panel** with bundle releases and gate numbers.
- **Served by the gateway** on the same port after `make web-build`; `make web` runs the Vite dev server on 5121 for UI work.

### 4.8 LLM release lane
`src/spectral_canary/llm/`, design in `docs/LLM_RELEASE_LANE.md`
- **Prompt bundles**: prompt + model tag + pinned Ollama weights digest + sampling parameters as one canonical, hashed, signed JSON. Tampering with the prompt or the weights digest is blocked (tested). `bundles.py`, `configs/llm/bundles/` (`triage-p1`, `triage-p2`, `triage-p3`, `triage-p2-qwen`, `triage-p2-ctx4k`)
- **Triage service** with a JSON output contract, bundle router and fallback to the champion. `POST /v1/llm/triage`. `service.py`
- **Gold set**: 150 labeled alerts from a seeded generator (`evals/triage/triage_gold_v1.jsonl`); `scw llm-gold` checks it, `--write` regenerates it. Labels come from a rule table, not from people.
- **Code graders**: schema validity, escalation decision, reason, one-sentence check. No LLM-as-judge. `graders.py`
- **Offline eval gate**: 3 seeded tries per case, pass^3 (all three tries correct), escalation recall for must-escalate cases overall and per segment, false-escalation increase, reason accuracy, cost and latency ratios (reported, not blocking offline), non-inferiority vs the champion on the same cases and seeds. `gates.py`, `evals.py`
- **Shadow and 10% canary** with online code grading, windows of 200 routed requests, paired McNemar on must-escalate alerts, p95 latency SLO (over 2,000 ms and over 1.5x the champion arm), token-cost SLO (1.25x), invalid-output and error budgets. `monitor.py`
- **Input-drift rollback**: unlike the ML lane, a major input drift (PSI 0.25 or more, at least 150 rows) rolls back, because the offline evidence no longer describes the traffic.
- **Same controller**: `llm/controller.py` subclasses the ML controller and supplies only the LLM evidence; rollback verifies with 20 real triage calls and writes a postmortem.
- **LLM API (16 routes under `/v1/llm`)**: triage, feedback, overview, registry (list, get, verify), releases (list, get, shadow, create, approve, pause, rollback), events, recent requests, drift.
- **OpenTelemetry GenAI metrics** on `/metrics`, Grafana panels and a control-room panel. `otel.py`
- **promptfoo regression gate**: `evals/triage/promptfooconfig.yaml`; absolute 90% threshold (`make llm-gate`) and a relative base-vs-head gate for CI (`make llm-gate-relative`, fails on a drop over 2.2 points).
- **Not built**: LLM-as-judge or human-calibrated graders, hosted models, human labels. An MLflow export is described in ADR-9, not implemented. *(Planned)*

### 4.9 Observability and deployment
- **Prometheus metrics**, **SSE event stream**, **redacted JSON logs** and **trace IDs** (`X-Trace-Id`). `telemetry.py`
- **Docker image**: non-root, read-only root filesystem; web image runs as user 101.
- **Compose stack** (`make up`): gateway 5120, UI 5121, a simulated-traffic service, Prometheus 5122, Grafana 5123, all bound to 127.0.0.1. `docker-compose.yml`, `deploy/`
- **OpenTofu `infra/local`**: Docker provider; applied and destroyed against local Docker during the build; `fmt` and `validate` in CI.
- **OpenTofu `infra/aws`** *(Waiting on owner)*: ECR plus one `t4g.micro`, SSM, IMDSv2, Budgets. Validated, not provisioned, $0 spent; `scripts/aws_deploy.sh` refuses to run without `SCW_CONFIRM_BILLABLE=yes`; `scripts/aws_teardown.sh` exists. Estimated at about US$10 to 11 per month (README).

### 4.10 Load testing and evidence
- **HTTP load test** with SLO cells at concurrency 1, 4, 16 and 32, in champion and canary mode; raw per-request results are saved. `loadtest.py`, `docs/LOAD_TEST_RESULTS.md`
- **Scoring benchmark**: `scripts/bench_scoring.py`.
- **Evidence builders**: `scripts/build_evidence.py`, `scripts/build_llm_evidence.py`; generated RESULTS pages and postmortems under `docs/evidence/`.

### 4.11 Security and repo hygiene
- **Only verified bytes serve**: hash check plus type allowlist before loading; corrupted artifact never becomes champion.
- **Secrets stay local**: operator tokens and the signing key are generated into `var/secrets` (gitignored). No CI job needs a secret.
- **gitleaks hooks** (`.githooks/pre-commit`, `.githooks/pre-push`) and a CI gitleaks scan over history; the pre-push hook blocks direct pushes to `main`.
- **Threat model and security notes**: `docs/THREAT_MODEL.md`, `docs/SECURITY_AND_SECRETS.md`.

### 4.12 Documentation set
`docs/` holds 19 documents: architecture (code level and system), ADRs, release policy, release and rollback, drift and limitations, data card, threat model, security and secrets, operations and deployment runbooks, load test results, cloud costs, incident postmortem, LLM lane, AI-engineer readiness, public case study, interview defense and recruiter demo. A walkthrough video and three screenshots are in `docs/media/`.

## 5. Integrations

- **Ollama** (local, default `http://127.0.0.1:11434`): `llama3.2:3b` for the triage prompt; `qwen2.5-coder:3b` for the model-swap candidate.
- **promptfoo** 0.125.1 for the regression gate; **OpenTelemetry**, **Prometheus** and **Grafana** for metrics.
- **GitHub Actions**: `ci.yml` (5 jobs) and `llm-gate.yml` (see section 7). Standard runners, free for a public repo.
- **Trivy, pip-audit, npm audit, gitleaks** in the `security` and `image` jobs.
- **Dashboards**: the read-only `/health`, `/capabilities` and `/runs` contract is what the owner's dashboard reads; `./scripts/start.sh` starts the app.

## 6. Data and privacy

- **All data is synthetic**: generated by this code; no real people, cards or payments. The dataset and traffic are labeled `simulated` on every request, event and screen.
- **LLM-lane labels** come from a rule table, not from people.
- **Local storage**: SQLite registry, event log and telemetry, artifacts and run reports under `var/` (gitignored), including local secrets.
- **What leaves the machine**: nothing by default. The LLM lane calls a local Ollama. CI runs on GitHub-hosted runners with no secrets.
- **Committed**: manifests, configs, gold set, recorded evidence (with no local paths since PR #12), media.
- **Never committed**: secrets, `.env*`, `var/`, `*.sqlite`, `*.tfstate`, `*.tfvars` (`CONTRIBUTING.md`).

## 7. Tests and evidence

Counted from source on 2026-11-15, not run.

| suite | command | count |
| --- | --- | --- |
| Python (pytest) | `make test` (`make test-fast` skips e2e) | 80 test functions across 9 files; README reports 99 collected (89 + 10 in `test_fastscore.py`) |
| of which: release controller | `tests/test_release_controller.py` | 17 |
| of which: LLM lane | `tests/test_llm_lane.py` | 16 |
| of which: API | `tests/test_api.py` | 11 |
| of which: data and training | `tests/test_data_and_training.py` | 10 |
| of which: LLM gate compare | `tests/test_llm_gate_compare.py` | 8 |
| of which: fast scorer | `tests/test_fastscore.py` | 7 |
| of which: store and routing | `tests/test_store_and_routing.py` | 6 |
| of which: end to end | `tests/test_e2e.py` | 4 |
| of which: CLI | `tests/test_cli.py` | 1 |
| Web | `cd web && npm test` | 7 |

**CI** (`.github/workflows/ci.yml`, pull requests and manual dispatch): `python` (ruff, mypy, fixture hashes, contract check, one chaos CLI smoke test, pytest on 4 xdist workers), `security` (pip-audit, gitleaks, npm audit, Trivy filesystem), `web` (typecheck, tests, build), `image` (Docker build, container smoke test, Trivy image scan), `iac` (OpenTofu `fmt` and `validate`). `llm-gate.yml` runs the relative promptfoo gate only on PRs that touch the LLM lane.

**Recorded evidence (from the README and docs, with their dates)**
- Held-out PR-AUC (2026-11-09): v1 0.600, v2 0.620, v3 0.600, v4 0.493.
- Rollback run `demo-20261109-001407` and rerun `demo-20261109-140904`: same 65 vs 83 of 112, p = 0.0015, 50/50 verified, 34-event chain intact.
- Chaos replay on merged code (2026-11-09 23:36 CST): 11/11 scenarios matched.
- Load test, merged code, quiet machine (2026-11-10): peak champion-only throughput 1,052 to 2,196 rps with the compiled scorer; canary c=4 p95 15.1 to 4.2 ms. One in-process release story 31.4 s to 3.3 s. Relative numbers from one laptop, not capacity planning.
- LLM run `llm-demo-20261109-130256` (1,408 s): `triage-p2` promoted, `triage-p3` and `triage-p2-qwen` blocked offline, `triage-p2-ctx4k` rolled back on the latency SLO (2.11x vs a 1.5x limit).
- promptfoo gate: champion 139/150 (92.7%), regressed prompt 74.0% (2026-11-09).
- Portability finding: the same champion scored 84.7% on a GitHub CPU runner, so CI gates on base vs head instead of an absolute threshold.
- CI on `main` at `661003f`: all 5 jobs green, `python` job 6 min 39 s (README, 2026-11-10).

## 8. Known limits and gaps

- Synthetic data and a constructed defect: the metrics describe the generator, not real fraud.
- Saturation handling is a fixed 100 ms threshold, verified by fault injection, not a capacity model. *(Planned: derive it from the champion's measured baseline.)*
- Guardrails re-test every window at p < 0.01 without alpha-spending. *(Planned: sequential testing such as mSPRT.)*
- Single process: window counters live in the API process; replicas would need a shared store and an elected controller. *(Planned: Postgres registry, multi-replica controller.)*
- Local roles, not enterprise identity management.
- The offline latency check measures on the machine it runs on; under extreme load it failed closed twice during the build (README, 2026-11-09).
- Not deployed: the AWS path is validated, not provisioned.
- LLM lane: rule-derived labels, code graders only; a 3B local model gets 37% of whole replies right on this policy; promptfoo's absolute threshold is backend-specific; canary windows are small (about 20 candidate calls per 200 requests), so p95 is noisy.

## 9. Change log

| Date | PR | Change |
| --- | --- | --- |
| 2026-11-10 | #12 | Hygiene: no local paths in public evidence or new event logs; README CI numbers refreshed |
| 2026-11-10 | #11 | Bit-identical compiled single-row scorer; 2.1 to 2.6x gateway throughput |
| 2026-11-10 | #10 | `make demo` gets its own port, starving `--scale` refused, `start.sh` honours `SCW_HOME` |
| 2026-11-10 | #9 | Postmortem impact and incident replay fixed to match the evidence |
| 2026-11-10 | #8 | Guardrail pauses instead of rolling back when the gateway is saturated |
| 2026-11-10 | #7 | CI: parallel pytest, chaos per scenario, LLM gate only on LLM-lane PRs |
| 2026-11-10 | #6 | Relative LLM triage gate on pull requests |
| 2026-11-10 | #4 | Full README; CI enabled, fixed and green |
| 2026-11-09 | #5 | MIT license |
| 2026-11-09 | #3 | LLM release lane: bundles, eval gate, canary SLOs, promptfoo gate, OTel metrics, UI panel |
| 2026-11-09 | #2 | Read-only `/health` `/capabilities` `/runs` contract; UI and API on one port |
| 2026-11-09 | #1 | v0.1: release gates, canary guardrails, audited rollback, chaos suite, load test, UI, Docker, OpenTofu |

## Keeping this file true

- **Same PR, same change:** a PR that adds, changes, removes or switches off something a person can see or use updates this file in the same PR.
- **Status moves with reality:** a feature changes status word when the thing behind it changes.
- **Only what is true:** no invented numbers; recorded results keep their date and source.
- **Repo gates:** CI must pass, gitleaks must be clean, and no local paths, secrets or `var/` content may be committed.
