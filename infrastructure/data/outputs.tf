output "bucket_name" {
  description = "Data lake bucket; use as DATA_DIR=s3://<bucket_name>."
  value       = aws_s3_bucket.data_lake.bucket
}

output "redshift_workgroup" {
  description = "Workgroup name, for the Redshift Data API (REDSHIFT_WORKGROUP)."
  value       = aws_redshiftserverless_workgroup.warehouse.workgroup_name
}

output "redshift_database" {
  description = "Database name (REDSHIFT_DATABASE)."
  value       = aws_redshiftserverless_namespace.warehouse.db_name
}

output "redshift_endpoint" {
  description = "Hostname Looker Studio connects to (port 5439, SSL)."
  value       = aws_redshiftserverless_workgroup.warehouse.endpoint[0].address
}

output "redshift_s3_role_arn" {
  description = "Role Redshift uses for COPY from the processed zone."
  value       = aws_iam_role.redshift_s3_read.arn
}
