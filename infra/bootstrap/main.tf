terraform {
  required_version = ">= 1.10, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.62.0"
    }
  }
}

variable "deployment" {
  description = "Use -var-file=../cloudbox.auto.tfvars.json; do not copy settings."
  type        = any
}

module "policy" {
  source     = "../modules/policy"
  deployment = var.deployment
}

provider "aws" {
  profile             = module.policy.config.aws_profile
  region              = module.policy.config.aws_region
  allowed_account_ids = [module.policy.config.aws_account_id]

  default_tags {
    tags = module.policy.tags
  }
}

# Only an approved administrator bootstrap can change these maximum grants.
resource "aws_iam_policy" "worker_boundary" {
  for_each = module.policy.role_policies
  name     = "${module.policy.names.role_names[each.key]}-boundary"
  policy   = each.value
}

resource "aws_iam_policy" "provisioner" {
  name   = "${module.policy.config.project_name}-provisioner"
  policy = module.policy.provisioner_policy
}

resource "aws_iam_role" "provisioner" {
  name                 = "${module.policy.config.project_name}-provisioner"
  max_session_duration = module.policy.sts_session_seconds
  permissions_boundary = aws_iam_policy.provisioner.arn

  assume_role_policy = jsonencode({
    Version = module.policy.iam_policy_version
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${module.policy.config.aws_account_id}:root" }
      Action    = ["sts:AssumeRole"]
      Condition = { ArnLike = { "aws:PrincipalArn" = module.policy.names.operator_arn_pattern } }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "provisioner" {
  role       = aws_iam_role.provisioner.name
  policy_arn = aws_iam_policy.provisioner.arn
}

output "provisioner_role_arn" {
  value = aws_iam_role.provisioner.arn
}
