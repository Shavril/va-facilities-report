terraform {
  required_version = ">= 1.11" # write-only arguments (value_wo)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State lives in a separate bucket created with the AWS CLI, outside any
  # Terraform config, so no teardown can delete it (see infrastructure/README.md).
  # Each part has its own state file. Locking uses S3 conditional writes.
  backend "s3" {
    bucket       = "va-facilities-report-tfstate-740948698458"
    key          = "base/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

# Credentials come from the environment: AWS_PROFILE locally, the OIDC role
# in GitHub Actions. No profile is hardcoded, so the same code runs in both.
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Part      = "base"
    }
  }
}
