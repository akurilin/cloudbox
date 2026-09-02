variable "deployment" {
  description = "Non-secret settings from cloudbox.auto.tfvars.json."
  type        = any
}

module "policy" {
  source     = "./modules/policy"
  deployment = var.deployment
}

locals {
  config = module.policy.config
  names  = module.policy.names
}

# Bootstrap owns this role and its limits in a separate state.
provider "aws" {
  profile             = local.config.aws_profile
  region              = local.config.aws_region
  allowed_account_ids = [local.config.aws_account_id]

  assume_role {
    role_arn     = local.names.provisioner_role_arn
    session_name = "cloudbox-terraform"
  }

  default_tags {
    tags = module.policy.tags
  }
}

# Keep run data private. Image archives do not expire with run results.
resource "aws_s3_bucket" "data" {
  bucket        = local.names.bucket_name
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "tls" {
  bucket = aws_s3_bucket.data.id
  policy = jsonencode({
    Version = module.policy.iam_policy_version
    Statement = [{
      Sid       = "RequireTLS"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_s3_bucket_lifecycle_configuration" "runs" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-runs"
    status = "Enabled"

    filter {
      prefix = "runs/"
    }

    expiration {
      days = module.policy.retention_days
    }
  }
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = local.names.log_group_name
  retention_in_days = module.policy.retention_days
}

# The operator adds the key later. No secret value enters Terraform state.
resource "aws_secretsmanager_secret" "openrouter" {
  name                    = local.names.secret_name
  description             = "OpenRouter key for Cloudbox workers."
  recovery_window_in_days = module.policy.secret_recovery_days
}

resource "aws_iam_role" "worker" {
  for_each             = module.policy.role_policies
  name                 = local.names.role_names[each.key]
  max_session_duration = module.policy.sts_session_seconds
  permissions_boundary = local.names.boundary_arns[each.key]
  assume_role_policy   = each.key == "run_data" ? module.policy.run_data_trust : module.policy.lambda_trust
}

resource "aws_iam_role_policy" "worker" {
  for_each = module.policy.role_policies
  name     = module.policy.inline_policy_name
  role     = aws_iam_role.worker[each.key].id
  policy   = each.value
}

# Read only this non-secret output in the CLI; do not export the whole state.
output "cloudbox" {
  value = merge(local.config, {
    schema_version        = 1
    bucket_name           = aws_s3_bucket.data.bucket
    image_name            = local.names.image_name
    image_arn             = local.names.image_arn
    base_image_arn        = local.names.base_image_arn
    architecture          = "ARM_64"
    ingress_connector_arn = local.names.ingress_connector_arn
    image_source_prefix   = "images/"
    build_role_arn        = aws_iam_role.worker["build"].arn
    runtime_role_arn      = aws_iam_role.worker["runtime"].arn
    run_data_role_arn     = aws_iam_role.worker["run_data"].arn
    provisioner_role_arn  = local.names.provisioner_role_arn
    openrouter_secret_arn = aws_secretsmanager_secret.openrouter.arn
    log_group_name        = aws_cloudwatch_log_group.worker.name
    max_timeout_seconds   = module.policy.max_timeout_seconds
    sts_session_seconds   = module.policy.sts_session_seconds
    cleanup_seconds       = module.policy.cleanup_seconds
    max_result_bytes      = module.policy.max_result_bytes
    max_prompt_characters = module.policy.max_prompt_characters
  })
}
