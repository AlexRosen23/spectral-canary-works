data "aws_caller_identity" "me" {}

data "aws_ssm_parameter" "al2023_arm" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

# --- container registry (images are pushed by scripts/aws_deploy.sh) -----------------------------------
resource "aws_ecr_repository" "gateway" {
  name                 = "spectral-canary-works"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # teardown must be able to remove pushed images
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

resource "aws_ecr_lifecycle_policy" "keep_few" {
  repository = aws_ecr_repository.gateway.name
  policy = jsonencode({ rules = [{ rulePriority = 1, description = "keep last 5 images",
    selection = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 },
  action = { type = "expire" } }] })
}

# --- network: one public subnet, ingress only from the operator's CIDR ------------------------------------
resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/24"
  enable_dns_hostnames = true
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
}

resource "aws_subnet" "public" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.42.0.0/26"
  # Accepted risk (ADR-7, docs/SECURITY_AND_SECRETS.md): no NAT gateway and no VPC endpoints, to keep the path at
  # ~$10-11/month. The public IP is how the instance reaches ECR and SSM; inbound is only port 5120 from
  # var.operator_cidr. The no-public-IP alternative (private subnet + 5 interface endpoints + S3 gateway endpoint,
  # access via SSM port forwarding) is estimated at ~$43/month in docs/CLOUD_COSTS.md.
  #trivy:ignore:AVD-AWS-0164
  map_public_ip_on_launch = true
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "gateway" {
  name        = "scw-gateway"
  description = "Gateway port from the operator CIDR only; no SSH (SSM Session Manager instead)"
  vpc_id      = aws_vpc.main.id
  ingress {
    description = "gateway API"
    from_port   = 5120
    to_port     = 5120
    protocol    = "tcp"
    cidr_blocks = [var.operator_cidr]
  }
  egress {
    description = "pull image from ECR, SSM agent"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    # Accepted risk (ADR-7): HTTPS only. ECR, S3 and SSM have no fixed IP ranges to pin; VPC endpoints would allow
    # restricting egress to the VPC CIDR (the ~$43/month alternative above).
    #trivy:ignore:AVD-AWS-0104
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- instance role: ECR pull + SSM only -------------------------------------------------------------------
resource "aws_iam_role" "instance" {
  name = "scw-gateway-instance"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow",
  Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "ecr_pull" {
  name = "ecr-pull-this-repo"
  role = aws_iam_role.instance.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
    Resource = aws_ecr_repository.gateway.arn }
  ] })
}

resource "aws_iam_instance_profile" "instance" {
  name = "scw-gateway-instance"
  role = aws_iam_role.instance.name
}

resource "aws_instance" "gateway" {
  ami                    = data.aws_ssm_parameter.al2023_arm.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.gateway.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name
  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }
  root_block_device {
    volume_size = 8
    volume_type = "gp3"
    encrypted   = true
  }
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region = var.region, registry = split("/", aws_ecr_repository.gateway.repository_url)[0],
    image  = "${aws_ecr_repository.gateway.repository_url}:${var.image_tag}"
  })
  user_data_replace_on_change = true
}

# --- cost guardrail ---------------------------------------------------------------------------------------
resource "aws_budgets_budget" "monthly" {
  name         = "spectral-canary-works-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  cost_filter {
    name   = "TagKeyValue"
    values = ["user:project$spectral-canary-works"]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}
