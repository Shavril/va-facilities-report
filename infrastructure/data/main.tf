# Resources that cost money: the S3 data lake (and, in Phase 4, Redshift).
# Destroyed by both teardown workflows; the workflows back the bucket up first.

data "aws_caller_identity" "current" {}

locals {
  # The account id makes the bucket name globally unique.
  bucket_name = "${var.project}-${data.aws_caller_identity.current.account_id}"
}

# --- S3 data lake --------------------------------------------------------
# One bucket, zones separated by prefix: raw/, processed/, metadata/.

resource "aws_s3_bucket" "data_lake" {
  bucket = local.bucket_name

  # Let `terraform destroy` delete the bucket even when it holds objects.
  # The teardown workflows are the safeguard instead: typed confirmation,
  # approval, and a backup of the bucket before anything is deleted.
  force_destroy = true
}

# Nothing in this bucket is ever public.
resource "aws_s3_bucket_public_access_block" "data_lake" {
  bucket                  = aws_s3_bucket.data_lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Disable ACLs: access is controlled by IAM policies only.
resource "aws_s3_bucket_ownership_controls" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Encrypt at rest with S3-managed keys (SSE-S3, no extra cost).
resource "aws_s3_bucket_server_side_encryption_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Clean up parts of uploads that never completed (they are billed but
# invisible). Raw snapshots are history, so nothing else expires.
resource "aws_s3_bucket_lifecycle_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Refuse any request that is not over HTTPS.
resource "aws_s3_bucket_policy" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.data_lake.arn,
        "${aws_s3_bucket.data_lake.arn}/*",
      ]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  # The public access block must exist first, or AWS may reject the policy.
  depends_on = [aws_s3_bucket_public_access_block.data_lake]
}
