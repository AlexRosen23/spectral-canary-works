# Contributing

## Branches and merges

- `main` is the reviewed baseline. Work happens on `feat/…`, `fix/…`, `chore/…` or `docs/…` branches. Push
  the branch and open a PR with `gh pr create`.
- Merging into `main` needs the owner's approval. The `pre-push` hook blocks direct pushes to `main`
  (`ALLOW_MAIN_PUSH=1` is for deliberate exceptions only).
- Releases are annotated tags (`git tag -a v0.1.0 -m …`). Roll back with `git revert` through a PR. Never
  force-push.

## Setup and checks

```bash
make setup           # uv sync --frozen + local secrets + web deps
make train-fixtures  # dataset + v1..v4 (verifies hashes against manifests/)
make ci              # lint, mypy, fixtures, contract, all tests (incl. e2e rollback over HTTP), chaos replay, web build
make iac-validate    # OpenTofu fmt/validate (no plan/apply)
make docker-build    # image build (retrains fixtures and verifies hashes inside the build)
make llm-gate        # promptfoo regression suite for the triage prompt (needs local Ollama + llama3.2:3b)
make llm-gate-relative LLM_GATE_BASE=origin/main  # same suite for the base and this checkout; fails on a >2.2-point drop
make llm-demo        # LLM lane story over real HTTP on :5141 (long; real local model calls)
```

Test failures: keep the full log and triage by failure signature (the owner's test-triage skill). Don't
paste whole logs into PRs.

## Changing models, data or policy

- **A model:** add a new `configs/models/<version>.toml`. Versions are immutable; never retrain an existing
  version with a different config (`train-fixtures` refuses).
- **The dataset generator:** bump `GENERATOR_VERSION`, run `scw train-fixtures --update-manifests`, and
  commit the new `manifests/` files in the same PR. The diff in the hashes *is* the review artifact.
- **The policy:** edit `configs/release_policy.toml`. Every release event records the policy SHA-256.
- **A prompt or LLM bundle:** add `configs/llm/prompts/<name>.md` and a new `configs/llm/bundles/<version>.toml`
  (bundles are immutable once registered). Run `make llm-gate-relative` (or `make llm-gate` on the Metal machine
  its threshold was calibrated on) and, for a candidate, `make llm-eval B=<version>`.
  The LLM lane's thresholds live in `configs/llm/release_policy.toml`.
- **The triage gold set:** change `llm/alerts.py`, run `scw llm-gold --write`, and commit the regenerated
  `evals/triage/triage_gold_v1.jsonl` (bump the file name if labels change).

## Environment variables (names only)

| name | purpose |
|---|---|
| `SCW_HOME` | state directory (default `./var`) |
| `SCW_DB` | registry DB to serve (default `var/runs/latest/registry.sqlite`) |
| `SCW_ENABLE_CHAOS` | `1` enables the simulated fault endpoints (default off) |
| `SCW_API_PORT`, `SCW_DEMO_PORT` | default 5120 (gateway, `make start`) and 5150 (`make demo`'s own gateway) |
| `OMP_NUM_THREADS` | default 1 (see `serving.limit_native_threads`) |
| `VITE_API_URL` | UI → gateway URL (default `http://127.0.0.1:5120`) |
| `SCW_CONFIRM_BILLABLE` | must be `yes` for `scripts/aws_deploy.sh` to run |
| `SCW_LLM_DB` | LLM lane registry DB to serve (default `var/runs/llm-latest/llm-registry.sqlite`) |
| `SCW_OLLAMA_URL` | Ollama server for the LLM lane (default `http://127.0.0.1:11434`) |
| `SCW_LLM_DEMO_PORT` | default 5141 |

## Never commit

Secrets, `.env*` (except `.env.example`), `var/` (data, artifacts, registries, event logs, run reports,
operator tokens, signing key), `*.sqlite`, `*.tfstate`, `*.tfvars`. The gitleaks hooks check staged changes and
outgoing commits.

## CI and deployment status

- GitHub Actions is **enabled** (owner decision, 2026-11-09). `.github/workflows/ci.yml` runs 5 jobs on every
  pull request and on manual dispatch (`gh workflow run ci --ref <branch>`). The `llm-gate` job runs on pull
  requests that touch the LLM lane (prompts, bundles, policy, gold set, LLM code, `scripts/llm_gate_*`, the Makefile
  or the workflow) and on manual dispatch. It is **relative**: it scores the PR base and the PR head with the same
  promptfoo suite on the same runner and fails when the head is more than 2.2 pass-rate points below the base (the
  policy's 0.05 escalation-recall margin over 66 of 150 must-escalate cases). No absolute threshold: the Metal-calibrated
  90% doesn't hold on the runner's CPU backend (`docs/LLM_RELEASE_LANE.md`). The log and step summary show both pass
  rates. Locally, `make llm-gate-relative` does the same. `make ci` runs the python and web jobs locally;
  `make workflow-lint` checks the workflow file itself before you push.
- Nothing is deployed. The AWS path in `infra/aws` is implemented and validated but not provisioned (see
  `docs/DEPLOYMENT_RUNBOOK.md`).
