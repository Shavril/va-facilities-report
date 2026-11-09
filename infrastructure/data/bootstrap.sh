#!/usr/bin/env bash
# Database setup that Terraform cannot do: users, schemas and grants inside
# Redshift. Run by the "AWS: plan / deploy" workflow after `terraform apply`,
# as the GitHub deploy role, which is the namespace's superuser because it
# created the namespace. Idempotent: safe to run after every apply.
#
# Result:
#   - IAM:<pipeline user>   owns the schemas staging, core and reporting, so
#                           the pipeline (Data API, IAM login, no password)
#                           can create and replace its own tables and views
#   - looker_reader         password login for Looker Studio, read-only on
#                           reporting; password kept in SSM only
#
# Usage: bootstrap.sh <workgroup> <database> <pipeline IAM user> <SSM parameter for the Looker password>
set -euo pipefail

workgroup=$1
database=$2
pipeline_user="IAM:$3" # how Redshift names an IAM user's database user
looker_parameter=$4
looker_user=looker_reader

# Run one SQL statement through the Data API and wait for it. Prints the
# statement id; fails the script if the statement fails. The SQL text is
# never printed (it may contain a password hash).
run_sql() {
  local id status
  id=$(aws redshift-data execute-statement --workgroup-name "$workgroup" \
    --database "$database" --sql "$1" --query Id --output text)
  while true; do
    status=$(aws redshift-data describe-statement --id "$id" --query Status --output text)
    case $status in
      FINISHED) echo "$id"; return 0 ;;
      FAILED | ABORTED)
        echo "::error::statement $status: $(aws redshift-data describe-statement --id "$id" --query Error --output text)" >&2
        return 1 ;;
    esac
    sleep 1
  done
}

# First column of the first row, or empty if there is no row.
query_value() {
  local id
  id=$(run_sql "$1")
  aws redshift-data get-statement-result --id "$id" \
    --query 'Records[0][0].*' --output text 2>/dev/null | sed 's/^None$//'
}

user_exists() {
  [ -n "$(query_value "SELECT 1 FROM pg_user WHERE usename = '$1'")" ]
}

echo "Running as: $(query_value "SELECT current_user")"

# 1. The pipeline's database user. Created ahead of its first login, so it
# can be given schemas now. PASSWORD DISABLE: IAM login only.
if user_exists "$pipeline_user"; then
  echo "User $pipeline_user exists"
else
  run_sql "CREATE USER \"$pipeline_user\" PASSWORD DISABLE" > /dev/null
  echo "Created user $pipeline_user"
fi

# 2. Schemas, owned by the pipeline user (see sql/README.md for what lives where).
for schema in staging core reporting; do
  run_sql "CREATE SCHEMA IF NOT EXISTS $schema AUTHORIZATION \"$pipeline_user\"" > /dev/null
done
# Nobody else may create objects in the default schema.
run_sql "REVOKE CREATE ON SCHEMA public FROM PUBLIC" > /dev/null
echo "Schemas staging, core, reporting owned by $pipeline_user"

# 3. Looker Studio's read-only user. The password lives only in SSM: reused
# if it exists (so Looker keeps working after a rebuild), generated
# otherwise. Redshift accepts an MD5 hash instead of the password, so the
# password itself never appears in SQL or in query history.
password=$(aws ssm get-parameter --name "$looker_parameter" --with-decryption \
  --query Parameter.Value --output text 2>/dev/null || true)
if [ -z "$password" ]; then
  # Redshift requires upper case, lower case and a digit.
  until [[ $password =~ [A-Z] && $password =~ [a-z] && $password =~ [0-9] ]]; do
    password=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32)
  done
  aws ssm put-parameter --name "$looker_parameter" --type SecureString \
    --value "$password" > /dev/null
  echo "Generated a new Looker password in $looker_parameter"
fi
echo "::add-mask::$password"
hash="md5$(printf '%s%s' "$password" "$looker_user" | md5sum | cut -d' ' -f1)"

if user_exists "$looker_user"; then
  run_sql "ALTER USER $looker_user PASSWORD '$hash'" > /dev/null
else
  run_sql "CREATE USER $looker_user PASSWORD '$hash'" > /dev/null
fi
run_sql "GRANT USAGE ON SCHEMA reporting TO $looker_user" > /dev/null
run_sql "GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO $looker_user" > /dev/null
# Views and tables the pipeline creates in reporting later are readable too.
run_sql "ALTER DEFAULT PRIVILEGES FOR USER \"$pipeline_user\" IN SCHEMA reporting GRANT SELECT ON TABLES TO $looker_user" > /dev/null
echo "User $looker_user: read-only on reporting (password in $looker_parameter)"
