# Deployment runbook

Four ways to run it, from least to most infrastructure. Only the first three have been run; the fourth is
**implemented but not provisioned**.

## 1. Native macOS (verified)

```bash
make setup            # uv sync --frozen, local secrets, web npm ci
make train-fixtures   # seeded dataset + v1..v4 (fails if hashes differ from manifests/)
make demo             # recorded release story over real HTTP on :5150 (server started and stopped by the demo)
make serve            # gateway on 127.0.0.1:5120 serving the latest recorded run
make web              # control room on 127.0.0.1:5121
```

Teardown: stop the two processes (Ctrl-C). Generated state is in `var/`; `make clean-state` removes it after asking.

## 2. Local production-like stack: docker compose (verified)

```bash
make up      # builds images (the builder retrains fixtures and verifies hashes), starts:
             #   gateway :5120, web :5121, Prometheus :5122, Grafana :5123 (dashboard pre-provisioned),
             #   and a traffic generator sending labeled SIMULATED requests
make down    # stops and removes containers, the network and the named volumes
```

All ports bind to 127.0.0.1. The gateway runs as uid 10001 with a read-only rootfs and no capabilities.

## 3. Local infrastructure from IaC (validated; applies only to local Docker)

```bash
make docker-build
cd infra/local && tofu init && tofu apply    # gateway container on 127.0.0.1:5140
tofu destroy                                 # remove it
```

`make iac-validate` runs `fmt -check` and `validate` for both `infra/local` and `infra/aws`. It never plans or
applies, and it needs no credentials. Without a local `tofu` it runs the official
`ghcr.io/opentofu/opentofu:1.8.5` image.

## 4. AWS (implemented, NOT PROVISIONED; needs explicit owner approval)

Prerequisites: an AWS account, a deployer identity with only `infra/aws/deployer-policy.json`, OpenTofu, the
AWS CLI, and Docker buildx.

1. Read `docs/CLOUD_COSTS.md` (estimate ~US$10–11/month).
2. `cp infra/aws/terraform.tfvars.example infra/aws/terraform.tfvars` and set `operator_cidr` (your IP/32;
   0.0.0.0/0 is rejected), `budget_email` and `image_tag`.
3. `SCW_CONFIRM_BILLABLE=yes scripts/aws_deploy.sh`. This creates the ECR repo, pushes an immutable
   `linux/arm64` image, then creates the VPC, security group, IAM role, instance and budget.
4. Verify: `curl http://<ip>:5120/readyz` from the operator IP; use SSM Session Manager for shell access
   (there is no SSH).
5. **Teardown:** `scripts/aws_teardown.sh`, then confirm in the console that nothing tagged
   `project=spectral-canary-works` remains.

State is local (`infra/aws/terraform.tfstate`, gitignored). For team use, configure an S3 backend with
locking first.

## Release of the gateway itself

Annotated git tags (`v0.1.0`). Image tags are immutable in ECR. A gateway rollback means redeploying the
previous tag (`image_tag=v0.0.x`). A *model* rollback never needs a redeploy: it is the in-app pointer swap
(`RELEASE_AND_ROLLBACK.md`).
