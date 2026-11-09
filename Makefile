# Spectral Canary Works — local-first commands. Nothing here touches the network except `setup`
# (dependency install) and the optional Docker/IaC targets, and nothing deploys anywhere.
SHELL := /bin/bash
export OMP_NUM_THREADS ?= 1
UV ?= uv
RUN := $(UV) run --frozen
API_PORT ?= 5120
WEB_PORT ?= 5121

.PHONY: start setup train-fixtures test test-fast eval serve web demo chaos-replay verify lint typecheck ci \
        loadtest web-build up down clean-state iac-validate contract-check docker-build

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

ci: lint typecheck train-fixtures contract-check test chaos-replay  ## what .github/workflows/ci.yml runs
	@if [ -d web/node_modules ]; then cd web && npm run typecheck && npm run build; fi
	@echo "make ci: all checks passed"

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
