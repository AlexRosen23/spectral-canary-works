variable "region" {
  type    = string
  default = "us-east-2"
}

variable "operator_cidr" {
  type        = string
  description = "The ONLY CIDR allowed to reach the gateway port (your IP /32). No 0.0.0.0/0 default."
  validation {
    condition     = can(cidrhost(var.operator_cidr, 0)) && var.operator_cidr != "0.0.0.0/0"
    error_message = "operator_cidr must be a specific CIDR (e.g. 203.0.113.7/32), never 0.0.0.0/0."
  }
}

variable "budget_email" {
  type        = string
  description = "Address that receives AWS Budgets alerts."
}

variable "monthly_budget_usd" {
  type    = number
  default = 10
}

variable "instance_type" {
  type    = string
  default = "t4g.micro" # 2 vCPU burstable ARM, 1 GiB; matches the image's linux/arm64 build
}

variable "image_tag" {
  type    = string
  default = "local"
}
