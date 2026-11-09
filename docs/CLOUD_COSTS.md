# Cloud costs

## Incurred to date: **US$0.00**

Nothing in this repository has been deployed to any cloud account. GitHub Actions is disabled on the repo,
so CI minutes used are 0 as well. Everything verified so far ran on the owner's Mac (native and Docker/OrbStack).

## Estimated cost of the optional AWS path (`infra/aws`, NOT PROVISIONED)

**Estimates**, based on public on-demand list prices for us-east-2 as I understand them in November 2026.
**Not a quote.** Re-check them in the AWS Pricing Calculator before any apply.

| item | assumption | est. US$/month |
|---|---|---|
| EC2 `t4g.micro` (on-demand) | 730 h × ~$0.0084/h | ~6.13 |
| Public IPv4 address | 730 h × $0.005/h | ~3.65 |
| EBS gp3 root volume | 8 GB × ~$0.08/GB-month | ~0.64 |
| ECR storage | ≤ 5 images × ~0.3 GB, ~$0.10/GB-month | ~0.15 |
| Data transfer out | demo traffic, < 1 GB | ~0.00–0.09 |
| AWS Budgets | first 2 budgets free | 0.00 |
| **Total** | | **≈ 10–11** |

Free-tier eligibility (if the account qualifies) could reduce the EC2 line. Don't rely on it.

### Guardrails built into the path

- An AWS Budgets monthly cost budget (default $10) on the `project=spectral-canary-works` tag, alerting at 50%
  actual and 100% forecast to `budget_email`.
- Every resource is tagged `project`, `managed_by`, `teardown`.
- `scripts/aws_deploy.sh` prints the estimate and **refuses** unless `SCW_CONFIRM_BILLABLE=yes` is set.
- `scripts/aws_teardown.sh` runs `tofu destroy`. The ECR repo has `force_delete` so the images go too.

## Alternatives considered (estimates)

| option | est. US$/month | why not chosen |
|---|---|---|
| ECS Fargate, 0.25 vCPU / 0.5 GB always on | ~9 + IPv4 ~3.65 | similar cost, more resources (cluster, task def, ALB or public IP) |
| App Runner, 0.25 vCPU / 0.5 GB | ~2.6 idle + vCPU while active | cheapest when idle; fewer network controls; a reasonable alternative |
| EKS | ≥ 73 control plane + nodes | not warranted (ADR-2) |
| Azure Container Apps (consumption) | ~0 idle within the free grant | no Azure tooling on this machine; equally valid |

## Local performance/cost trade-offs (measured)

See `LOAD_TEST_RESULTS.md` for CPU%, RSS and throughput per concurrency level. Those are the inputs for
sizing an instance. This document does not claim any cloud saving from them.
