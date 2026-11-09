#!/usr/bin/env bash
# OPTIONAL, BILLABLE. Deploys the gateway to AWS (see docs/DEPLOYMENT_RUNBOOK.md, docs/CLOUD_COSTS.md).
# Refuses unless SCW_CONFIRM_BILLABLE=yes is set by a human who has read the cost estimate.
set -euo pipefail
cd "$(dirname "$0")/.."
cat <<'MSG'
This creates billable AWS resources (estimated ~US$10-11/month: t4g.micro + public IPv4 + 8 GB gp3 + ECR
in us-east-2, plus data transfer; an estimate, not a quote). A budget alert is created. Tear down with scripts/aws_teardown.sh.
MSG
if [ "${SCW_CONFIRM_BILLABLE:-}" != "yes" ]; then
  echo "Refusing: set SCW_CONFIRM_BILLABLE=yes to proceed (owner approval required)." >&2
  exit 2
fi
command -v tofu >/dev/null || { echo "install OpenTofu first" >&2; exit 1; }
command -v aws >/dev/null || { echo "install + configure the AWS CLI with the deployer policy (infra/aws/deployer-policy.json)" >&2; exit 1; }
[ -f infra/aws/terraform.tfvars ] || { echo "copy infra/aws/terraform.tfvars.example to terraform.tfvars and fill it in" >&2; exit 1; }
TAG="${SCW_IMAGE_TAG:-v0.1.0}"
cd infra/aws
tofu init -input=false
tofu apply -input=false -target=aws_ecr_repository.gateway -target=aws_ecr_lifecycle_policy.keep_few -var "image_tag=$TAG"
REPO="$(tofu output -raw ecr_repository_url)"
REGION="$(grep -E '^region' terraform.tfvars | cut -d'"' -f2)"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO%%/*}"
docker buildx build --platform linux/arm64 -t "$REPO:$TAG" --push ../..
tofu apply -input=false -var "image_tag=$TAG"
echo "gateway: $(tofu output -raw gateway_url)  (reachable only from operator_cidr)"
