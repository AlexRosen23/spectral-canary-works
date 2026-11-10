# Architecture decision records

## ADR-1: one container + docker compose locally, IaC for reproducibility

**Context:** one CPU-bound model gateway, $0 budget, Apple Silicon laptop, recruiter-reviewable.
**Decision:** a single multi-stage, non-root image. Docker compose runs the production-like local stack
(gateway, web, simulated traffic, Prometheus, Grafana). OpenTofu describes the same gateway for local Docker
(`infra/local`) and for an optional single-VM AWS path (`infra/aws`).
**Consequences:** one command up/down; nothing billable by default. No HA, and no rolling deploys of the
gateway itself. Model releases (the subject of this project) are already zero-downtime pointer swaps inside the
gateway.

## ADR-2: no Kubernetes at this scale

**Options:** k3d/kind locally, EKS/AKS in cloud, or none.
**Decision:** none. The spec requires a fully working cluster if Kubernetes is added at all, and empty YAML
would be resume theater. A real cluster adds a control plane (EKS about $73/month before nodes), ingress,
secrets, RBAC and upgrade work. It would serve a single replica that already gets model-level canaries from the
application.
**When to revisit:** more than two services, horizontal autoscaling on real load, or a platform team that
already runs a cluster. In that case the gateway is stateless apart from the window counter (ADR-4), so it
moves to a Deployment + HPA directly. Argo Rollouts / Flagger could then also canary the *gateway* image, with
the app-level model canary unchanged.

## ADR-3: SQLite (WAL) for registry, events and telemetry

**Decision:** one SQLite file per run directory. Triggers make model rows immutable and events append-only.
A hash chain makes tampering detectable. CAS on `deployment.generation` serializes pointer changes.
**Why:** zero ops, transactional pointer + audit writes, and portable evidence (a run directory *is* the
record). **Trade-off:** single writer. Telemetry inserts are batched by a bounded writer, and the load test
measures this path. **Upgrade:** Postgres for the registry, pointer and events (same schema and triggers);
telemetry to a queue + columnar store.

## ADR-4: guardrail windows on a logical clock, evaluated in the request path

**Decision:** evaluate every 1,500 routed requests, synchronously on the request that crosses the boundary.
**Why:** replay determinism (`test_release_story_passes_and_replays_deterministically`) and no dependence on wall-clock speed, which on
a loaded laptop varies 10× or more. **Cost:** one request per window pays the evaluation latency, visible as
p99/max spikes in the load test. **Upgrade for replicas:** a single elected controller evaluating from the
shared store, with a time-based safety evaluation for low-traffic periods (the `stale_after_seconds` check
already refuses promotion on stale evidence).

## ADR-5: a paired design for labeled guardrails

**Decision:** on candidate-arm requests the gateway also scores the champion (not returned, only recorded).
Labeled quality is judged with an exact McNemar test on the paired outcomes.
**Why:** a 10% canary with 6% fraud yields few labeled positives. Pairing removes between-request variance, so
far fewer samples are needed than for comparing independent arms. **Cost:** about 1 extra model call on 10% of
traffic (a measured CPU cost). Arm-vs-arm Newcombe intervals are also reported.

## ADR-6: skops artifacts, not pickle

**Decision:** skops with an explicit type allowlist; anything outside the list is refused before load.
**Trade-off:** each new model family needs its types reviewed and added. The two HGB entries
(`functools.partial`, `sklearn.utils.validation.check_array`) were added after review and are documented in code.

## ADR-7: an AWS path on a single EC2 instance (not App Runner, Fargate, Lambda or Azure)

**Context:** no cloud credentials are configured on this machine, so the provider is chosen on cost and
least privilege. **Decision:** ECR + one `t4g.micro` running the same image, with SSM (no SSH), IMDSv2,
ingress from a single CIDR, and AWS Budgets alerts. Estimated about $10–11/month including the public IPv4 charge, versus App
Runner (about $2.60/month idle for 0.5 GB provisioned memory, plus vCPU while active) or Fargate (about
$9/month for 0.25 vCPU / 0.5 GB always on, plus the same IPv4 charge). Lambda is a poor fit for a stateful SQLite store plus a background ticker. Estimates are in
`CLOUD_COSTS.md`. **Status:** validated, not provisioned.

## ADR-8: React + Vite instead of Next.js

**Decision:** a static Vite build. No server rendering is needed, nginx serves it as non-root, and there are
fewer moving parts. Charts are hand-written SVG to avoid chart-library weight and keep every mark tied to
API data.

## ADR-9: the LLM lane reuses this repo's registry and controller instead of MLflow (with an export path)

**Context:** the LLM triage lane needs an immutable, hashed record of each releasable unit (prompt text + model +
weights digest + sampling params), a release state machine, an audit trail and a rollback path. MLflow's GenAI
stack covers much of the bookkeeping: a Prompt Registry with commit-style immutable versions, mutable aliases and
lineage to traces and evaluations (https://mlflow.org/docs/latest/genai/prompt-registry/), tracing it describes as
OpenTelemetry-compatible (https://mlflow.org/docs/latest/genai/tracing/), and `mlflow.genai.evaluate` with code and
LLM-judge scorers (https://mlflow.org/docs/latest/genai/eval-monitor/). Retrieved 2026-11-09.
**Decision:** keep the existing registry for this lab. A prompt bundle is registered through the same
`model_versions` table, SHA-256 and HMAC signature as a model artifact (`llm/bundles.py`), and released by the same
`Controller` (`llm/controller.py` only plugs LLM evidence into its hooks).
**Why:** (1) the lab's point is the release *controller*: CAS pointer swaps, separation of duties, paired online
guardrails, idempotent rollback, hash-chained events. MLflow aliases are mutable pointers without that state
machine, so MLflow would add a second source of truth to keep consistent with the controller's pointer. (2) one
SQLite file per run is portable evidence (ADR-3); an MLflow tracking server is another process to run and back up.
(3) the bundle pins the Ollama weights digest and the output schema in generation order; the MLflow prompt version
holds the template, and per its docs the model config stored with a version is mutable.
**Consequences / export path:** the bundle JSON (`var/.../llm/bundles/<version>.json`) already carries everything a
prompt-registry entry needs (name, template, version, model, params, hash). A thin exporter (`mlflow.genai.register_prompt`
per bundle plus an alias for the champion) is straightforward but **not implemented**: it would add a heavy
dependency for no behavior the gate uses. Revisit when a team already runs MLflow, or when traces and eval runs need
to be browsed across many projects; the OTel GenAI metrics this lane exports are the bridge in the meantime.
