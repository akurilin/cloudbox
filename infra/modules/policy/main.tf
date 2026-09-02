variable "deployment" {
  description = "One deployment's non-secret settings."
  type = object({
    aws_account_id          = string
    aws_region              = string
    aws_profile             = string
    project_name            = string
    sso_permission_set_name = optional(string, "AdministratorAccess")
    image_version           = optional(string, "")
    base_image_version      = optional(string)
    memory_mib              = optional(number, 1024)
    default_model           = optional(string, "z-ai/glm-5.3")
    timeout_seconds         = optional(number, 600)
  })

  validation {
    condition     = can(regex("^[0-9]{12}$", var.deployment.aws_account_id))
    error_message = "aws_account_id must contain 12 digits."
  }

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,19}$", var.deployment.project_name))
    error_message = "project_name must use 2-20 lowercase letters, digits, or hyphens."
  }

  validation {
    condition     = contains([512, 1024, 2048, 4096, 8192], var.deployment.memory_mib)
    error_message = "memory_mib must be a supported MicroVM baseline size."
  }

  validation {
    condition     = var.deployment.timeout_seconds >= 60 && var.deployment.timeout_seconds <= 3300 && floor(var.deployment.timeout_seconds) == var.deployment.timeout_seconds
    error_message = "timeout_seconds must be an integer from 60 through 3300."
  }

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.deployment.aws_region)) && length(trimspace(var.deployment.aws_profile)) > 0
    error_message = "Set a commercial AWS region and a named AWS profile."
  }
}

locals {
  config                = var.deployment
  iam_policy_version    = "2012-10-17"
  inline_policy_name    = "cloudbox-access"
  retention_days        = 30
  secret_recovery_days  = 7
  sts_session_seconds   = 3600
  max_timeout_seconds   = 3300
  cleanup_seconds       = 30
  max_result_bytes      = 1048576
  max_prompt_characters = 128000
  arn_prefix            = "arn:aws"
  account_arn           = "${local.arn_prefix}:iam::${local.config.aws_account_id}"
  region_arn            = "${local.arn_prefix}:lambda:${local.config.aws_region}"
  image_name            = "${local.config.project_name}-worker"
  image_arn             = "${local.region_arn}:${local.config.aws_account_id}:microvm-image:${local.image_name}"
  base_image_arn        = "${local.region_arn}:aws:microvm-image:al2023-1"
  bucket_name           = "${local.config.project_name}-${local.config.aws_account_id}-${local.config.aws_region}"
  bucket_arn            = "${local.arn_prefix}:s3:::${local.bucket_name}"
  secret_name           = "${local.config.project_name}/openrouter"
  secret_arn            = "${local.arn_prefix}:secretsmanager:${local.config.aws_region}:${local.config.aws_account_id}:secret:${local.secret_name}-??????"
  log_group_name        = "/aws/lambda-microvms/${local.image_name}"
  log_group_arn         = "${local.arn_prefix}:logs:${local.config.aws_region}:${local.config.aws_account_id}:log-group:${local.log_group_name}"
  provisioner_role_arn  = "${local.account_arn}:role/${local.config.project_name}-provisioner"
  ingress_connector_arn = "${local.region_arn}:aws:network-connector:aws-network-connector:NO_INGRESS"
  operator_arn_pattern  = "${local.account_arn}:role/aws-reserved/sso.amazonaws.com/*AWSReservedSSO_${local.config.sso_permission_set_name}_*"
  tags                  = { Project = local.config.project_name, ManagedBy = "Terraform" }
  role_names = {
    build    = "${local.config.project_name}-build"
    runtime  = "${local.config.project_name}-runtime"
    run_data = "${local.config.project_name}-run-data"
  }
  role_arns     = { for key, name in local.role_names : key => "${local.account_arn}:role/${name}" }
  boundary_arns = { for key, name in local.role_names : key => "${local.account_arn}:policy/${name}-boundary" }

  # Boundaries and worker grants share one definition to prevent drift.
  logging_statement = {
    Effect   = "Allow"
    Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    Resource = [local.log_group_arn, "${local.log_group_arn}:*"]
  }
  role_statements = {
    build = [local.logging_statement, {
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = ["${local.bucket_arn}/images/*"]
    }]
    runtime = [local.logging_statement, {
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [local.secret_arn]
      }, {
      Effect   = "Allow"
      Action   = ["lambda:TerminateMicrovm"]
      Resource = [local.image_arn]
    }]
    run_data = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject"]
      Resource = ["${local.bucket_arn}/runs/*"]
    }]
  }
  role_policies = { for key, statements in local.role_statements : key => jsonencode({
    Version = local.iam_policy_version, Statement = statements
  }) }
  lambda_trust = jsonencode({
    Version = local.iam_policy_version
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
  run_data_trust = jsonencode({
    Version = local.iam_policy_version
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "${local.account_arn}:root" }
      Action    = ["sts:AssumeRole"]
      Condition = { ArnEquals = { "aws:PrincipalArn" = local.provisioner_role_arn } }
    }]
  })

  # The provisioner cannot change itself or these bootstrap-owned boundaries.
  provisioner_policy = jsonencode({
    Version = local.iam_policy_version
    Statement = concat([
      {
        Sid      = "ProjectStorage"
        Effect   = "Allow"
        Action   = ["s3:*"]
        Resource = [local.bucket_arn, "${local.bucket_arn}/*"]
      },
      {
        Sid      = "ProjectLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy", "logs:TagResource", "logs:UntagResource", "logs:ListTagsForResource", "logs:DescribeLogStreams", "logs:GetLogEvents", "logs:FilterLogEvents"]
        Resource = [local.log_group_arn, "${local.log_group_arn}:*"]
      },
      {
        Sid      = "ReadServiceLists"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups", "lambda:ListMicrovmImages", "lambda:ListMicrovms", "lambda:ListManagedMicrovmImages"]
        Resource = "*"
      },
      {
        Sid      = "ProjectSecretSetup"
        Effect   = "Allow"
        Action   = ["secretsmanager:CreateSecret", "secretsmanager:DescribeSecret", "secretsmanager:UpdateSecret", "secretsmanager:PutSecretValue", "secretsmanager:DeleteSecret", "secretsmanager:RestoreSecret", "secretsmanager:GetResourcePolicy", "secretsmanager:TagResource", "secretsmanager:UntagResource"]
        Resource = local.secret_arn
      },
      {
        Sid      = "ManageWorkerRoles"
        Effect   = "Allow"
        Action   = ["iam:GetRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateRoleDescription", "iam:UpdateAssumeRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole", "iam:GetRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:TagRole", "iam:UntagRole"]
        Resource = values(local.role_arns)
      },
      {
        # MicroVM calls omit PassedToService. Keep the grant on two exact roles.
        Sid      = "PassBuildAndRuntimeOnly"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [local.role_arns.build, local.role_arns.runtime]
      },
      {
        Sid      = "RunDataSessions"
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = local.role_arns.run_data
      },
      {
        Sid       = "CreateProjectImages"
        Effect    = "Allow"
        Action    = ["lambda:CreateMicrovmImage"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestTag/Project" = local.config.project_name, "aws:RequestedRegion" = local.config.aws_region } }
      },
      {
        # CreateMicrovmImage authorizes initial tags against *, before the ARN exists.
        Sid      = "InitialProjectImageTags"
        Effect   = "Allow"
        Action   = ["lambda:TagResource"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion"      = local.config.aws_region
            "aws:RequestTag/Project"   = local.config.project_name
            "aws:RequestTag/ManagedBy" = "CloudboxImageScript"
          }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Project", "ManagedBy"] }
        }
      },
      {
        Sid      = "ProjectImageAndRuns"
        Effect   = "Allow"
        Action   = ["lambda:GetMicrovmImage", "lambda:UpdateMicrovmImage", "lambda:DeleteMicrovmImage", "lambda:DeleteMicrovmImageVersion", "lambda:UpdateMicrovmImageVersion", "lambda:GetMicrovmImageVersion", "lambda:GetMicrovmImageBuild", "lambda:ListMicrovmImageVersions", "lambda:ListMicrovmImageBuilds", "lambda:ListTags", "lambda:TagResource", "lambda:UntagResource", "lambda:RunMicrovm", "lambda:GetMicrovm", "lambda:TerminateMicrovm"]
        Resource = local.image_arn
      },
      {
        Sid      = "ManagedBaseImage"
        Effect   = "Allow"
        Action   = ["lambda:ListManagedMicrovmImageVersions", "lambda:GetMicrovmImageVersion", "lambda:GetMicrovmImage"]
        Resource = local.base_image_arn
      },
      {
        # This action has no resource-level scope. Workers never receive it.
        Sid       = "RegionalNetworkConnectorUse"
        Effect    = "Allow"
        Action    = ["lambda:PassNetworkConnector"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.config.aws_region } }
      }
      ], [for key, arn in local.role_arns : {
        Sid       = "BoundedRole${replace(key, "_", "")}"
        Effect    = "Allow"
        Action    = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"]
        Resource  = arn
        Condition = { StringEquals = { "iam:PermissionsBoundary" = local.boundary_arns[key] } }
    }])
  })
}

output "config" { value = local.config }
output "iam_policy_version" { value = local.iam_policy_version }
output "inline_policy_name" { value = local.inline_policy_name }
output "retention_days" { value = local.retention_days }
output "secret_recovery_days" { value = local.secret_recovery_days }
output "sts_session_seconds" { value = local.sts_session_seconds }
output "max_timeout_seconds" { value = local.max_timeout_seconds }
output "cleanup_seconds" { value = local.cleanup_seconds }
output "max_result_bytes" { value = local.max_result_bytes }
output "max_prompt_characters" { value = local.max_prompt_characters }
output "tags" { value = local.tags }
output "lambda_trust" { value = local.lambda_trust }
output "run_data_trust" { value = local.run_data_trust }
output "role_policies" { value = local.role_policies }
output "provisioner_policy" { value = local.provisioner_policy }
output "names" {
  value = {
    bucket_name           = local.bucket_name
    image_name            = local.image_name
    image_arn             = local.image_arn
    base_image_arn        = local.base_image_arn
    log_group_name        = local.log_group_name
    secret_name           = local.secret_name
    role_names            = local.role_names
    boundary_arns         = local.boundary_arns
    provisioner_role_arn  = local.provisioner_role_arn
    ingress_connector_arn = local.ingress_connector_arn
    operator_arn_pattern  = local.operator_arn_pattern
  }
}
