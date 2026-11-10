# Spectral Canary Works — local-first commands. Nothing here touches the network except `setup`
# (dependency install) and the optional Docker/IaC targets, and nothing deploys anywhere.
SHELL := /bin/bash
export OMP_NUM_THREADS ?= 1
UV ?= uv
RUN := $(UV) run --frozen
API_PORT ?= 5120
WEB_PORT ?= 5121
LLM_DEMO_PORT ?= 5141
# promptfoo pass-rate threshold (percent) for `make llm-gate`. A case fails on an invalid reply or a missed
# must-escalate alert (66 of the 150 test cases). Recorded champion triage-p2: escalation recall 0.8333 on the gold
# test split (docs/evidence/llm/). Allowed recall = 0.8333 - 0.05 (the gate margin) -> 100 * (1 - (1 - 0.7833) * 66/150)
# = 90.5 -> 90.
LLM_GATE_THRESHOLD ?= 90
# That absolute threshold only holds on the backend it was calibrated on (Apple Metal). On a GitHub CPU runner the same
# weights, Ollama, prompt and seed score the champion 84.7%. `make llm-gate-relative` (the CI pull-request gate) instead
# runs the suite for a base checkout and for this one on the same machine, and fails when this one drops more than
# 100 * 0.05 * 66/150 = 2.2 pass-rate points below the base: the same 0.05 recall margin, read from the base's
# configs/llm/release_policy.toml ([offline_gate] max_escalation_recall_drop), with the case counts from the base run.
LLM_GATE_BASE ?= origin/main

.PHONY: start setup train-fixtures test test-fast eval serve web demo chaos-replay verify lint typecheck ci \
        loadtest web-build up down clean-state iac-validate contract-check docker-build \
        llm-gold llm-eval llm-demo llm-gate llm-gate-relative workflow-lint

setup:            ## install Python + web deps, create local secrets (gitignored)
	$(UV) sync --frozen
	$(RUN) scw setup
	@if [ -d web ]; then cd web && npm ci --no-audit --no-fund; fi

train-fixtures:   ## generate the seeded dataset and train v1..v4 (skips versions already trained)
	$(RUN) scw train-fixtures

eval:             ## held-out metrics table for every trained version
	$(RUN) scw eval

test:             ## full test suite incl. end-to-end failure->rollback scenario
	$(RUN) pytest

test-fast:        ## unit tests only
	$(RUN) pytest -m "not e2e"

start:            ## everything on one port: builds what is missing, then gateway + control room on 127.0.0.1:$(API_PORT)
	SCW_API_PORT=$(API_PORT) ./scripts/start.sh

serve:            ## gateway on 127.0.0.1:$(API_PORT) (reads var/runs/latest if present)
	$(RUN) scw serve --port $(API_PORT)

web:              ## control-room UI dev server on 127.0.0.1:$(WEB_PORT)
	cd web && npm run dev -- --port $(WEB_PORT) --host 127.0.0.1

web-build:
	cd web && npm run build

demo:             ## fixture state -> release v2 (promoted) -> v4 (blocked) -> v3 canary + SIMULATED shift -> rollback
	$(RUN) scw demo --port $(API_PORT)

chaos-replay:     ## every seeded failure scenario with expected outcomes
	$(RUN) scw chaos-replay

verify:           ## re-hash artifacts, check manifest signatures and the event hash chain
	$(RUN) scw verify

loadtest:         ## real HTTP load test against a dedicated gateway on :5130; results in var/runs/loadtest-*
	$(RUN) scw loadtest

lint:
	$(RUN) ruff check src tests
	$(RUN) ruff format --check src tests

typecheck:
	$(RUN) mypy

contract-check:   ## validate trained manifests against contracts/model_artifact_v1.schema.json
	$(RUN) python scripts/check_contract.py

iac-validate:     ## fmt + validate OpenTofu (local docker stack and the not-provisioned AWS path)
	./scripts/iac_validate.sh

llm-gold:         ## check the committed triage gold set against its seeded generator
	$(RUN) scw llm-gold

llm-eval:         ## offline regression suite (k=3, gold test split) for a bundle, real Ollama calls: make llm-eval B=triage-p2
	$(RUN) scw llm-eval $(or $(B),triage-p2) --k 3

llm-demo:         ## LLM triage lane story over real HTTP on :$(LLM_DEMO_PORT) with real local Ollama calls (long: ~2 h here)
	$(RUN) scw llm-demo --port $(LLM_DEMO_PORT)

llm-gate:         ## promptfoo regression suite (pinned 0.125.1) vs local Ollama, fails under LLM_GATE_THRESHOLD % pass rate
	cd evals/triage && npm ci --no-audit --no-fund --silent && mkdir -p results && \
	PROMPTFOO_PASS_RATE_THRESHOLD=$(LLM_GATE_THRESHOLD) PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_DISABLE_UPDATE=1 \
	npx promptfoo eval -c promptfooconfig.yaml --no-cache --no-progress-bar \
	  -o results/promptfoo-results.json -o results/promptfoo-results.junit.xml

llm-gate-relative: ## base-vs-head promptfoo gate on one machine (LLM_GATE_BASE=<ref>, default origin/main; ~2x llm-gate)
	LLM_GATE_BASE="$(LLM_GATE_BASE)" PYTHON="$${PYTHON:-$(RUN) python}" ./scripts/llm_gate_relative.sh

ci: lint typecheck train-fixtures contract-check llm-gold test chaos-replay  ## what .github/workflows/ci.yml runs
	@if [ -d web/node_modules ]; then cd web && npm run typecheck && npm run build; fi
	@echo "make ci: all checks passed"

workflow-lint:    ## parse + lint .github/workflows with actionlint (Docker); catches YAML errors GitHub would reject
	docker run --rm -v "$$PWD:/repo" -w /repo rhysd/actionlint:1.7.7 -color

docker-build:
	docker build -t spectral-canary-works:local .

up:               ## production-like local stack: gateway + web + simulated traffic + Prometheus + Grafana
	docker compose up -d --build --wait api web prometheus grafana
	docker compose up -d traffic
	@echo "gateway http://127.0.0.1:5120  web http://127.0.0.1:5121  prometheus http://127.0.0.1:5122  grafana http://127.0.0.1:5123"

down:             ## stop the stack and remove its containers, network and volumes
	docker compose down -v --remove-orphans

clean-state:      ## remove generated local state (data, artifacts, registries, runs). Asks first.
	@read -p "Delete ./var (generated data/artifacts/registries)? [y/N] " a && [ "$$a" = y ] && rm -rf var || echo "kept"
