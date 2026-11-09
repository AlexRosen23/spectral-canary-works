output "ecr_repository_url" { value = aws_ecr_repository.gateway.repository_url }
output "gateway_url" { value = "http://${aws_instance.gateway.public_ip}:5120" }
output "instance_id" { value = aws_instance.gateway.id }
output "account_id" { value = data.aws_caller_identity.me.account_id }
