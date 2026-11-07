variable "project" {
  description = "Project name, used as a prefix for resource names and as a tag."
  type        = string
  default     = "va-facilities-report"
}

variable "region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}
