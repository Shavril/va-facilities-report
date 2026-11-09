# How GitHub Actions logs into AWS: two IAM roles that GitHub's OIDC tokens
# can assume, so no AWS access key is ever stored in GitHub.
#
# Applied once, by hand, from a laptop with a profile that can manage IAM.
# No workflow can change or destroy this part (the deploy role has no
# permission to), which is what lets a full teardown be rebuilt from GitHub.
#
# Role names start with "github-actions-", not the project prefix, so that a
# future "manage IAM roles named va-facilities-report-*" permission for the
# deploy role can never cover the deploy role itself (no self-escalation).

variable "github_owner" {
  description = "GitHub user or organization that owns the repository."
  type        = string
  default     = "Shavril"
}

variable "github_repo" {
  description = "GitHub repository name."
  type        = string
  default     = "va-facilities-report"
}

variable "github_environment" {
  description = "GitHub environment that deploy jobs run in (deployments restricted to main)."
  type        = string
  default     = "aws"
}

data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "arn:aws:s3:::${var.project}-tfstate-${local.account_id}"
  data_bucket  = "arn:aws:s3:::${var.project}-${local.account_id}"
  parameters   = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/${var.project}/*"
  # IAM roles the data/ part creates (e.g. the role Redshift uses for COPY).
  # The deploy role's own name starts with "github-actions-", so it is not
  # covered: no self-escalation.
  project_roles = "arn:aws:iam::${local.account_id}:role/${var.project}-*"

  # State files the workflows may use. github-access/ itself is excluded.
  workflow_state = ["${local.state_bucket}/base/*", "${local.state_bucket}/data/*"]

  # GitHub's token "sub" claim, in both formats: classic
  # "repo:OWNER/REPO:..." and the immutable "repo:OWNER@ID/REPO@ID:..." used
  # by repositories created after 2026-07-15 (this one). Lesson from the
  # previous project: matching only the classic format fails.
  subject_prefixes = [
    "repo:${var.github_owner}/${var.github_repo}",
    "repo:${var.github_owner}@*/${var.github_repo}@*",
  ]
}

# There is one GitHub OIDC provider per AWS account, and it already exists
# (created by the pipeline-gcp-to-aws-migration project). Referenced, not
# managed: destroying that project's infrastructure would remove it and
# break this project's GitHub access too.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

# Trust policy: "a GitHub Actions job whose token subject matches".
data "aws_iam_policy_document" "trust" {
  for_each = {
    plan   = ":pull_request"                          # any PR in this repo
    deploy = ":environment:${var.github_environment}" # only jobs in the environment
  }

  statement {
    effect = "Allow"
    # sts:TagSession is required too: configure-aws-credentials tags the
    # session, and without it AWS rejects the whole call with a misleading
    # "not authorized to perform sts:AssumeRoleWithWebIdentity".
    actions = ["sts:AssumeRoleWithWebIdentity", "sts:TagSession"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [for prefix in local.subject_prefixes : "${prefix}${each.value}"]
    }
  }
}

# --- plan role: read-only, for `terraform plan` on pull requests ----------

resource "aws_iam_role" "plan" {
  name                 = "github-actions-${var.project}-plan"
  description          = "GitHub Actions: terraform plan on pull requests (read-only)"
  assume_role_policy   = data.aws_iam_policy_document.trust["plan"].json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "plan" {
  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject"]
    resources = local.workflow_state
  }
  statement {
    sid       = "ListState"
    actions   = ["s3:ListBucket"]
    resources = [local.state_bucket]
  }
  statement {
    # Even `plan` takes the state lock: it writes and deletes a .tflock file.
    sid       = "StateLock"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = [for prefix in local.workflow_state : "${prefix}.tflock"]
  }
  statement {
    sid       = "ReadDataLake"
    actions   = ["s3:Get*", "s3:List*"]
    resources = [local.data_bucket]
  }
  statement {
    sid       = "ReadParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:ListTagsForResource"]
    resources = [local.parameters]
  }
  statement {
    sid       = "DescribeParameters" # cannot be limited to a path
    actions   = ["ssm:DescribeParameters"]
    resources = ["*"]
  }
  statement {
    # Redshift and EC2 read actions cannot be limited to our resources:
    # namespace/workgroup ARNs contain generated ids. Read-only.
    sid = "ReadWarehouseAndNetwork"
    actions = [
      "redshift-serverless:Get*",
      "redshift-serverless:List*",
      "ec2:Describe*",
    ]
    resources = ["*"]
  }
  statement {
    sid = "ReadProjectRoles"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
    ]
    resources = [local.project_roles]
  }
}

resource "aws_iam_role_policy" "plan" {
  name   = "terraform-plan"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.plan.json
}

# --- deploy role: apply, rebuild and teardown -----------------------------

resource "aws_iam_role" "deploy" {
  name                 = "github-actions-${var.project}-deploy"
  description          = "GitHub Actions: terraform apply/destroy of base/ and data/, from main"
  assume_role_policy   = data.aws_iam_policy_document.trust["deploy"].json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "State"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = local.workflow_state
  }
  statement {
    sid       = "ListState"
    actions   = ["s3:ListBucket"]
    resources = [local.state_bucket]
  }
  statement {
    # Create, configure, back up (read objects) and destroy the data lake.
    sid       = "DataLake"
    actions   = ["s3:*"]
    resources = [local.data_bucket, "${local.data_bucket}/*"]
  }
  statement {
    sid = "Parameters"
    actions = [
      "ssm:PutParameter",
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:DeleteParameter",
      "ssm:AddTagsToResource",
      "ssm:RemoveTagsFromResource",
      "ssm:ListTagsForResource",
    ]
    resources = [local.parameters]
  }
  statement {
    sid       = "DescribeParameters" # cannot be limited to a path
    actions   = ["ssm:DescribeParameters"]
    resources = ["*"]
  }
  statement {
    # Namespace, workgroup and usage limit. Not limitable by name (ARNs
    # contain generated ids), so this also covers other Redshift Serverless
    # resources in the account; acceptable for a single-project sandbox
    # account, and documented in infrastructure/README.md.
    sid       = "RedshiftServerless"
    actions   = ["redshift-serverless:*"]
    resources = ["*"]
  }
  statement {
    # bootstrap.sh: SQL through the Data API, as the namespace superuser.
    sid = "RedshiftDataApi"
    actions = [
      "redshift-data:ExecuteStatement",
      "redshift-data:BatchExecuteStatement",
      "redshift-data:DescribeStatement",
      "redshift-data:GetStatementResult",
    ]
    resources = ["*"]
  }
  statement {
    # Looker Studio security group, and the network reads Redshift needs
    # (destroy also needs ec2:DescribeNetworkInterfaces: lesson from the
    # previous project).
    sid = "Network"
    actions = [
      "ec2:Describe*",
      "ec2:CreateSecurityGroup",
      "ec2:DeleteSecurityGroup",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupIngress",
      "ec2:AuthorizeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupEgress",
      "ec2:ModifySecurityGroupRules",
      "ec2:UpdateSecurityGroupRuleDescriptionsIngress",
      "ec2:CreateTags",
      "ec2:DeleteTags",
    ]
    resources = ["*"]
  }
  statement {
    # Roles named va-facilities-report-* only (see local.project_roles).
    sid = "ProjectRoles"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:UpdateRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:PutRolePolicy",
      "iam:GetRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = [local.project_roles]
  }
  statement {
    # Attaching the COPY role to the namespace "passes" it to Redshift.
    sid       = "PassProjectRolesToRedshift"
    actions   = ["iam:PassRole"]
    resources = [local.project_roles]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["redshift.amazonaws.com", "redshift-serverless.amazonaws.com"]
    }
  }
  statement {
    # Redshift's service-linked role (manages the endpoint's network
    # interfaces); created by AWS on first use, if it does not exist yet.
    sid       = "RedshiftServiceLinkedRole"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]
    condition {
      test     = "StringLike"
      variable = "iam:AWSServiceName"
      values   = ["redshift.amazonaws.com", "redshift-serverless.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "terraform-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

output "plan_role_arn" {
  description = "GitHub repository variable AWS_PLAN_ROLE_ARN."
  value       = aws_iam_role.plan.arn
}

output "deploy_role_arn" {
  description = "GitHub repository variable AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.deploy.arn
}
