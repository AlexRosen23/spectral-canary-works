# AI Engineer readiness: what this repo evidences

_Written 2026-11-09 on branch `feat/ai-engineer-readiness`. Every number below comes from a saved run file produced by a
command in this repo (linked). Nothing was deployed; no hosted model API was used; alert traffic is simulated._

## Who this is for

Late-2026 AI Engineer / forward-deployed engineer postings increasingly ask for **evals as an engineering discipline**,
**release gating for LLM changes**, **cost/latency awareness** and **observability**, on top of shipping Python LLM
applications. This repo already was an ML release-engineering lab (registry -> offline gate -> shadow -> 10% canary ->
automatic rollback). This branch adds an **LLM release lane** that puts prompt/model changes through the same
machinery.

Sample of postings this was checked against (9 postings read on 2026-11-09; counts are approximate keyword matches):

- Python LLM applications in production (9/9), agents/tool design (6/9), security + governance + human oversight
  (5/9), cloud + CI/CD (5/9), evals with graders calibrated against humans (4/9), RAG (4/9), tracing/observability
  (4/9), cost + latency (3/9), MCP (1/9).
- Postings: [Endeavor FDE](https://api.ashbyhq.com/posting-api/job-board/endeavorai) ·
  [Kirkland AI Engineer II (Dice mirror)](https://www.dice.com/job-detail/6e8edb07-ec39-4fc1-886f-990b692abc49) ·
  [iManage Sr AI Engineer (closed)](https://www.purpose.jobs/discover/companies/imanage-2-30d3782f-8d61-4fd1-aa6b-9695834634e0/jobs/84445120-senior-ai-engineer) ·
  [OpenAI Applied AI Engineer](https://jobs.ashbyhq.com/openai/6fc25168-bc4c-47a0-8601-583dfaf7fcbd) ·
  [Anthropic Staff GTM AI Engineer](https://job-boards.greenhouse.io/anthropic/jobs/5390966008) ·
  [Writer AI Engineer](https://jobs.ashbyhq.com/WRITER/93fbe5ce-2ce9-41ea-a998-a433c0f7f085) ·
  [Databricks Sr FDE FinServ](https://databricks.com/company/careers/open-positions/job?gh_jid=8592938002)
- Practice references: [Anthropic, "Demystifying evals for AI agents" (2026-01-09)](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) ·
  [Hamel Husain, LLM-judge guide](https://hamel.dev/blog/posts/llm-judge/) ·
  [promptfoo CI/CD](https://www.promptfoo.dev/docs/integrations/ci-cd/) ·
  [MLflow GenAI](https://mlflow.org/docs/latest/genai/) ·
  [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai)

## What this branch added (measured)

All numbers: recorded runs `llm-demo-20261109-130256` (primary) and `llm-demo-20261109-111237` (first run), on an Apple M5 Pro
laptop shared with other workloads, local Ollama, gold test split of 150 synthetic alerts x 3 seeded tries, code
graders. Tables are generated from the run files: [`docs/evidence/llm/`](evidence/llm/).

| capability | what exists | measured result | evidence |
|---|---|---|---|
| LLM component in production shape | FastAPI `/v1/llm/triage`: structured JSON output (Ollama JSON schema), contract validation, champion fallback on an invalid candidate reply, fail-closed 503 + `routed_to_human`, 30 s timeout, bulkheads | 100% schema-valid replies for every bundle offline (2,250 tries) | `llm/service.py`, `tests/test_llm_lane.py` |
| Releasable unit = prompt + model + params | immutable bundle, SHA-256 + HMAC, Ollama weights digest pinned; registry rows cannot be updated (SQLite triggers) | tampered prompt file and changed weights digest both blocked (tests) | `llm/bundles.py` |
| Offline eval suite | 150-case gold set (rule-derived labels, generator-checked), k=3 seeded tries, pass@1 and pass^3, per-segment escalation recall, non-inferiority vs champion | p1 -> p2: escalation recall 75.2% -> 83.3%, pass^3 31.3% -> 36.7%, reason accuracy 54.4% -> 56.0% | `RESULTS.md` |
| Catching a regressed prompt | p3 = p2 minus one escalation rule | **blocked**: escalation recall 40.9% vs 83.3%; failed all three segment checks. Its pass^3 (34.0%) was *inside* the margin, so an aggregate-correctness gate alone would have shipped it | gate report `llm-rel-0002` |
| Cost/latency vs quality on a model swap | same prompt on qwen2.5-coder:3b | 4.6% cheaper per call (notional), offline p95 13% lower, but escalation recall 42.4% vs 83.3% -> **blocked** | gate report `llm-rel-0003` |
| Canary with online grading + SLOs | 10% sticky canary, paired champion call, McNemar on must-escalate alerts, p95 latency and token-cost SLOs, input-drift trigger | p2: 70 shadow requests with 0 candidate failures; 2 healthy canary windows (400 routed requests, 54 answered by p2); paired must-escalate alerts 12/12 for both -> promoted after release-manager approval (self-approval refused, 403) | `llm/monitor.py` |
| Automatic rollback + postmortem | same `Controller.rollback` (CAS + audit event in one transaction), 20 real verification calls, generated postmortem | ctx4k config change (same answers offline): window 1 candidate p95 2,830 ms vs champion arm 1,342 ms (2.11x; first run 5,105 vs 2,319 ms, 2.20x) -> **rolled back**; 20/20 verification calls answered by p2, 30 post-rollback requests all served by p2, event chain verified (30 events) | `postmortem-llm-rel-0004.md` |
| Observability | OTel GenAI metrics (`gen_ai.client.token.usage`, `gen_ai.client.operation.duration`, Development status) on `/metrics`, two Grafana panels | series present on the recorded run's `/metrics` | `metrics-genai.prom` |
| CI gate for prompts | promptfoo 0.125.1 (pinned), local Ollama, pass-rate threshold 90; CI job on manual dispatch (threshold calibrated on Metal; the CPU runner scored the champion 84.7%, see LLM_RELEASE_LANE.md) | champion p2: 139/150 cases pass (92.7%, exit 0; the 11 failures are its 11 missed escalations); regressed p3: 111/150 (74.0%) -> promptfoo exit 100 |  `docs/evidence/llm/promptfoo/` |
| Governance / human oversight | separation of duties for promotion, hash-chained audit log, OWASP LLM Top 10 mapping | event chains verified on both runs | `docs/THREAT_MODEL.md` |

Two findings worth reading in full ([`LLM_RELEASE_LANE.md`](LLM_RELEASE_LANE.md)):

1. **Aggregate metrics hide asymmetric regressions.** The regressed prompt and the model swap had equal or higher
   overall escalate accuracy than the champion (61.3% and 69.3% vs 62.0%), because under-escalating wins on the
   should-not-escalate cases. Only recall on must-escalate cases, per segment, caught them; the promptfoo gate was
   rewritten to fail on missed escalations for the same reason.
2. **A "no-op" config change failed in canary for a real reason.** Raising `num_ctx` from 2048 to 4096 gave
   identical answers offline (each bundle is evaluated alone), but on one Ollama server two context sizes of the same
   model force a reload on every switch. The canary's same-window latency SLO caught it; nothing here was simulated
   except the alert traffic.

## Requirement map (postings -> this repo)

| posting requirement (count of 9) | evidenced here? | where |
|---|---|---|
| Python LLM applications in production (9/9) | partly: a served LLM component with contracts, fallbacks, timeouts and release control; local model, not deployed | `src/spectral_canary/llm/` |
| Evals as an engineering discipline (4/9, most detailed) | yes for code-graded regression evals, pass^k, segment slicing, CI gating; **no** human-calibrated LLM judge | `llm/evals.py`, `llm/gates.py`, `evals/triage/` |
| Observability / tracing (4/9) | metrics yes (OTel GenAI semconv); **no** traces/spans | `llm/otel.py`, Grafana |
| Cost + latency (3/9) | yes: token-cost and p95 SLOs in canary, offline cost/latency on the model swap | `llm/monitor.py` |
| Security, governance, human oversight (5/9) | approvals with separation of duties, signed immutable bundles, audit chain, OWASP LLM mapping; **no** prompt-injection test set | `docs/THREAT_MODEL.md` |
| Cloud + CI/CD (5/9) | GitHub Actions enabled 2026-11-09 (5 PR jobs green, LLM gate on manual dispatch); Docker/IaC from the ML lane; **not deployed** | `.github/workflows/ci.yml` |
| Agents / tool design (6/9) | **no** (this repo has no tool-using agent) | — |
| RAG (4/9) | **no** | — |
| MCP (1/9) | **no** | — |

## What is still missing (honest)

- **Human labels and an LLM judge.** All labels are rule-derived and all graders are code. There is no judge
  calibrated against human labels (TPR/TNR on a held-out split, as in Hamel Husain's guide); rationales are only
  checked for presence, length and one-sentence form.
- **Model quality.** A 3B model is fully correct on about 37% of cases and over-escalates about half of the
  should-not-escalate alerts. The lane shows how such a component would be gated, not that it is good enough to use.
- **Statistical power.** 150 cases cannot separate candidates a few points apart; canary windows on a laptop hold
  ~20-30 candidate calls. Margins are wide on purpose.
- **Scale and deployment.** One process, one laptop, one Ollama server; nothing deployed. (GitHub Actions has been enabled since 2026-11-09.)
- **Tracing.** OTel metrics only; no spans or trace export, and the GenAI conventions are still in Development
  (inference metric names are already moving on the spec's main branch).
- **Security testing.** No prompt-injection or red-team suite (`promptfoo redteam` not run).
- **Smoke-check hypothesis did not hold.** The design expected the 12-case textbook smoke subset to miss the p3
  regression. With this model, dropping one rule degraded escalation everywhere, and the smoke subset showed it too
  (6/12 vs the champion's 8/12). The first run treated that hypothesis as a pass/fail expectation and was therefore
  recorded as FAILED; it is now an observation.

## Owner decisions needed

- Whether to enable the `llm-gate` CI job (needs Actions on this repo and pinning the Ollama image by digest).
- Whether a hosted model (e.g. a Claude model via API) should be added as a second candidate; that needs an API key
  and spend approval. Nothing in this branch calls a paid API.
- Whether to have a person label a sample of alerts (and rationales) so an LLM judge can be calibrated.
