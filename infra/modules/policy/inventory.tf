# Every managed address needs an absence check before setup or teardown.
output "resource_manifest" {
  value = {
    bootstrap = merge({
      "aws_iam_role.provisioner" = {
        kind = "role"
        identity = {
          arn  = local.provisioner_role_arn
          name = "${local.config.project_name}-provisioner"
        }
      }
      "aws_iam_policy.provisioner" = {
        kind = "policy"
        identity = {
          arn  = replace(local.provisioner_role_arn, ":role/", ":policy/")
          name = "${local.config.project_name}-provisioner"
        }
      }
      "aws_iam_role_policy_attachment.provisioner" = {
        kind = "role_attachment"
        identity = {
          role       = "${local.config.project_name}-provisioner"
          policy_arn = replace(local.provisioner_role_arn, ":role/", ":policy/")
        }
      }
      }, { for key, arn in local.boundary_arns : "aws_iam_policy.worker_boundary[\"${key}\"]" => {
        kind     = "policy"
        identity = { arn = arn, name = "${local.role_names[key]}-boundary" }
      }
    })
    main = merge({
      "aws_s3_bucket.data" = {
        kind     = "bucket"
        identity = { bucket = local.bucket_name }
      }
      "aws_cloudwatch_log_group.worker" = {
        kind     = "log_group"
        identity = { name = local.log_group_name }
      }
      "aws_secretsmanager_secret.openrouter" = {
        kind     = "secret"
        identity = { name = local.secret_name }
      }
      }, local.github_enabled ? {
      "aws_secretsmanager_secret.github[0]" = {
        kind     = "secret"
        identity = { name = local.github_secret_name }
      }
      } : {}, { for address in [
        "aws_s3_bucket_public_access_block.data",
        "aws_s3_bucket_ownership_controls.data",
        "aws_s3_bucket_server_side_encryption_configuration.data",
        "aws_s3_bucket_policy.tls",
        "aws_s3_bucket_lifecycle_configuration.runs",
        ] : address => {
        kind       = "bucket_setting"
        identity   = { bucket = local.bucket_name }
        covered_by = "aws_s3_bucket.data"
      }
      }, { for key, name in local.role_names : "aws_iam_role.worker[\"${key}\"]" => {
        kind     = "role"
        identity = { name = name }
      }
      }, { for key, name in local.role_names : "aws_iam_role_policy.worker[\"${key}\"]" => {
        kind     = "role_policy"
        identity = { role = name, name = local.inline_policy_name }
      }
    })
  }
}
