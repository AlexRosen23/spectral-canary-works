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
```

Test failures: keep the full log and triage by failure signature (the owner's test-triage skill). Don't
paste whole logs into PRs.

## Changing models, data or policy

- **A model:** add a new `configs/models/<version>.toml`. Versions are immutable; never retrain an existing
  version with a different config (`train-fixtures` refuses).
- **The dataset generator:** bump `GENERATOR_VERSION`, run `scw train-fixtures --update-manifests`, and
  commit the new `manifests/` files in the same PR. The diff in the hashes *is* the review artifact.
- **The policy:** edit `configs/release_policy.toml`. Every release event records the policy SHA-256.

## Environment variables (names only)

| name | purpose |
|---|---|
| `SCW_HOME` | state directory (default `./var`) |
| `SCW_DB` | registry DB to serve (default `var/runs/latest/registry.sqlite`) |
| `SCW_ENABLE_CHAOS` | `1` enables the simulated fault endpoints (default off) |
| `SCW_API_PORT`, `SCW_DEMO_PORT` | default 5120 |
| `OMP_NUM_THREADS` | default 1 (see `serving.limit_native_threads`) |
| `VITE_API_URL` | UI → gateway URL (default `http://127.0.0.1:5120`) |
| `SCW_CONFIRM_BILLABLE` | must be `yes` for `scripts/aws_deploy.sh` to run |

## Never commit

Secrets, `.env*` (except `.env.example`), `var/` (data, artifacts, registries, event logs, run reports,
operator tokens, signing key), `*.sqlite`, `*.tfstate`, `*.tfvars`. The gitleaks hooks check staged changes and
outgoing commits.

## CI and deployment status

- `.github/workflows/ci.yml` is **defined but GitHub Actions is disabled** on this private repo. Enabling it
  is the owner's decision. `make ci` runs the same checks locally.
- Nothing is deployed. The AWS path in `infra/aws` is implemented and validated but not provisioned (see
  `docs/DEPLOYMENT_RUNBOOK.md`).
