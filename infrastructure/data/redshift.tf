# Redshift Serverless: the analytical warehouse.
#
# Serverless has no cluster to size or pause: compute (RPUs) is billed per
# second only while queries run (60 s minimum), storage separately. The
# "namespace" holds the data (database, users, schemas); the "workgroup" is
# the compute and network endpoint in front of it.
#
# No admin user/password is set. The only way in is IAM, through the
# Redshift Data API (an HTTPS AWS API, no network path into the VPC). The
# identity that creates the namespace becomes its superuser; that is the
# GitHub deploy role, which then runs bootstrap.sh to give the pipeline's
# IAM user its own schemas (see the README).

variable "redshift_database" {
  description = "Database created in the namespace."
  type        = string
  default     = "va_facilities"
}

variable "redshift_monthly_rpu_hours" {
  description = "Usage limit: RPU-hours per month before queries are stopped (4 RPU x 1 h = 4 RPU-hours = $1.50)."
  type        = number
  default     = 30
}

# --- network: the account's default VPC (referenced, never managed) -----

data "aws_vpc" "default" {
  default = true
}

# A workgroup needs subnets in at least 3 availability zones.
data "aws_subnets" "redshift" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "availability-zone"
    values = ["${var.region}a", "${var.region}b", "${var.region}c"]
  }
}

# Looker Studio's native Redshift connector needs a public endpoint (it has
# no Data API, SSH tunnel or private connectivity option). This security
# group keeps that opening narrow: only Google's published Looker Studio
# address range, only the Redshift port. Everything else, including the
# pipeline, uses the Data API and never this path.
resource "aws_security_group" "looker_studio" {
  name        = "${var.project}-looker-studio"
  description = "Looker Studio connector to Redshift Serverless on 5439 only"
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "${var.project}-looker-studio"
  }
}

# Source: https://docs.cloud.google.com/looker/docs/studio/connect-to-amazon-redshift
# (checked 2026-11-09; the page also lists an IPv6 range, not needed here
# because the workgroup endpoint is IPv4).
resource "aws_vpc_security_group_ingress_rule" "looker_studio" {
  security_group_id = aws_security_group.looker_studio.id
  description       = "Looker Studio (Google) published IP range"
  cidr_ipv4         = "142.251.74.0/23"
  from_port         = 5439
  to_port           = 5439
  ip_protocol       = "tcp"
}
# No egress rule: replies to allowed connections pass anyway (security groups
# are stateful), and COPY reads S3 through Redshift's own network, not this
# VPC.

# --- IAM role Redshift assumes to read the data lake during COPY ----------

data "aws_iam_policy_document" "redshift_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["redshift.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "redshift_s3_read" {
  name               = "${var.project}-redshift-s3-read"
  description        = "Redshift Serverless: read the processed zone of the data lake for COPY"
  assume_role_policy = data.aws_iam_policy_document.redshift_trust.json
}

# Read-only, and only the processed zone: the warehouse loads validated
# Parquet, never raw API responses.
data "aws_iam_policy_document" "redshift_s3_read" {
  statement {
    sid       = "ListProcessed"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.data_lake.arn]
  }
  statement {
    sid       = "ReadProcessed"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.data_lake.arn}/processed/*"]
  }
}

resource "aws_iam_role_policy" "redshift_s3_read" {
  name   = "read-processed-zone"
  role   = aws_iam_role.redshift_s3_read.id
  policy = data.aws_iam_policy_document.redshift_s3_read.json
}

# --- Redshift Serverless ---------------------------------------------------

resource "aws_redshiftserverless_namespace" "warehouse" {
  namespace_name       = var.project
  db_name              = var.redshift_database
  iam_roles            = [aws_iam_role.redshift_s3_read.arn]
  default_iam_role_arn = aws_iam_role.redshift_s3_read.arn
}

resource "aws_redshiftserverless_workgroup" "warehouse" {
  namespace_name = aws_redshiftserverless_namespace.warehouse.namespace_name
  workgroup_name = var.project

  # 4 RPU is the smallest base capacity ($0.375 per RPU-hour in us-east-1,
  # checked 2026-11-09). Plenty for a few MB of data.
  base_capacity = 4

  # Public endpoint only for Looker Studio, limited by the security group.
  # The public IPv4 address costs $0.005/hour (~$3.70/month) while the
  # workgroup exists. require_ssl is true by default and left that way.
  publicly_accessible = true
  subnet_ids          = data.aws_subnets.redshift.ids
  security_group_ids  = [aws_security_group.looker_studio.id]
}

# Cost guardrail: after this many RPU-hours in a month, Redshift stops
# accepting queries until the next month (or until the limit is raised).
resource "aws_redshiftserverless_usage_limit" "monthly" {
  resource_arn  = aws_redshiftserverless_workgroup.warehouse.arn
  usage_type    = "serverless-compute"
  amount        = var.redshift_monthly_rpu_hours
  period        = "monthly"
  breach_action = "deactivate"
}
