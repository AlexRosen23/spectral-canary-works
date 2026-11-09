# Security and secrets

## What counts as a secret here, and where it lives

| secret | file | created by | permissions | in git? |
|---|---|---|---|---|
| manifest signing key (HMAC-SHA256) | `var/secrets/manifest-signing.key` | `scw setup` / first registration | 0600 | no (`var/` gitignored) |
| operator bearer tokens | `var/secrets/operators.local.json` | `scw setup` | 0600 | no |
| AWS variables (if ever used) | `infra/aws/terraform.tfvars` | the operator | user | no (`*.tfvars` gitignored; only `.example` committed) |

There are **no API keys, cloud credentials or paid-service tokens** anywhere in the repository or required by
any test. CI needs no secrets.

## Controls

- **Secret scanning:** gitleaks pre-commit (staged changes) and pre-push (outgoing commits) hooks from
  `.githooks/`, a full-history scan before the repo is first published, and a gitleaks job in CI.
- **Never committed:** `var/` (datasets, artifacts, registries, events, run reports, secrets), `.env*`,
  `*.sqlite`, `*.tfstate`, `*.tfvars`. Only small manifests (`manifests/`) and an example contract file are tracked.
- **Logs:** structured JSON. Features and authorization headers are redacted, and only ids, versions, status
  and latency are logged.
- **Metrics:** bounded labels only (arm, model_version, status, reason). No request, card or trace ids.
- **Artifacts:** skops with a type allowlist, SHA-256-verified on every load, read-only files (0444).
- **Containers:** non-root uid 10001, read-only rootfs, `cap_drop: ALL`, `no-new-privileges`, ports bound to
  127.0.0.1, resource limits in compose, and health checks.
- **Web:** CSP restricting `connect-src` to the local gateway, `nosniff`, `no-referrer`. CORS on the API is
  limited to the UI origin.
- **Chaos:** disabled by default (`SCW_ENABLE_CHAOS=1` to enable), needs the operator role, and every injection
  is an audit event marked `simulated`.
- **Dependencies:** `uv.lock` / `package-lock.json` pin every version. CI runs `pip-audit`, `npm audit`, and
  Trivy filesystem + image scans.

## Accepted risks (scanner exceptions)

Trivy's filesystem scan runs in CI with `--severity HIGH,CRITICAL --exit-code 1`. Two findings in the optional,
**not provisioned** AWS path are accepted on purpose and suppressed inline, rule by rule, next to the code they
cover (`#trivy:ignore:<rule>` in `infra/aws/main.tf`). Nothing else is suppressed.

| rule | finding | why it is accepted | what would remove it |
|---|---|---|---|
| AVD-AWS-0164 (HIGH) | the subnet assigns public IPs | there is no NAT gateway; the public IP is the instance's path to ECR and SSM. Inbound is only port 5120 from `operator_cidr`, there is no SSH, and IMDSv2 is required | a private subnet with VPC endpoints and SSM port forwarding, estimated at ~US$43/month vs ~US$10–11 (`CLOUD_COSTS.md`) |
| AVD-AWS-0104 (CRITICAL) | the security group allows egress to any IP | the egress is TCP 443 only; ECR, S3 and SSM have no fixed IP ranges to pin | the same VPC-endpoint variant, with egress limited to the VPC CIDR |

Both decisions belong to the owner if the AWS path is ever provisioned (ADR-7).

## Rotating local secrets

Delete `var/secrets/operators.local.json` and run `scw setup` to issue new tokens; restart the gateway.
Rotating the signing key invalidates existing signatures, which is intentional: re-register the artifacts
into a fresh registry (`make demo` does this for each run).

## Reporting

This is a personal portfolio project. Report issues privately to the repository owner.
