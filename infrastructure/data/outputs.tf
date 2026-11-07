output "bucket_name" {
  description = "Data lake bucket; use as DATA_DIR=s3://<bucket_name>."
  value       = aws_s3_bucket.data_lake.bucket
}
