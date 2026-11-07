# Free resources the project needs: the API key parameter (and, later, IAM
# roles for the pipeline and Redshift). Destroyed only by the full teardown.

variable "environment" {
  description = "VA API environment the API key belongs to (sandbox or production)."
  type        = string
  default     = "sandbox"

  validation {
    condition     = contains(["sandbox", "production"], var.environment)
    error_message = "environment must be \"sandbox\" or \"production\"."
  }
}

# --- API key in SSM Parameter Store --------------------------------------
# Terraform creates the parameter with a placeholder through the write-only
# argument `value_wo`, which is never stored in Terraform state. The real
# key is written afterwards with `aws ssm put-parameter` (by hand, or by the
# GitHub workflow from a repository secret).
#
# Verified (2026-11-07): created this way, the real value does not reach
# state even after a refresh. An *imported* parameter does get its value
# copied into state, so this parameter must never be imported.

resource "aws_ssm_parameter" "va_api_key" {
  name             = "/${var.project}/${var.environment}/api-key"
  description      = "VA Facilities API key (${var.environment})"
  type             = "SecureString" # encrypted with the AWS-managed aws/ssm key
  value_wo         = "set-me-with-aws-ssm-put-parameter"
  value_wo_version = 1 # bump only to deliberately reset to the placeholder
}

output "api_key_parameter" {
  description = "SSM parameter holding the VA API key; use as VA_API_KEY_SSM_PARAMETER."
  value       = aws_ssm_parameter.va_api_key.name
}
