# AWS deployment path — IMPLEMENTED BUT NOT PROVISIONED. Nothing in this directory has been applied.
# Applying it creates billable resources (estimate in docs/CLOUD_COSTS.md). Use scripts/aws_deploy.sh,
# which refuses to run without explicit confirmation, and scripts/aws_teardown.sh to remove everything.
terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.70" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      project     = "spectral-canary-works"
      managed_by  = "opentofu"
      cost_center = "portfolio-demo"
      teardown    = "scripts/aws_teardown.sh"
    }
  }
}
