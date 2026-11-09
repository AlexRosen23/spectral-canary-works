#!/usr/bin/env bash
# Removes EVERYTHING the AWS path created (instance, network, IAM role/profile, ECR repo + images, budget).
set -euo pipefail
cd "$(dirname "$0")/../infra/aws"
[ -f terraform.tfstate ] || { echo "no local state: nothing was deployed from this checkout"; exit 0; }
tofu destroy -input=false
echo "verify in the console: no resources tagged project=spectral-canary-works remain"
