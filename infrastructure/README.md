# Infrastructure (Terraform)

Three independent Terraform parts, each with its own state file. They are
split by **what a teardown removes**:

| Part | Resources | Applied by | Destroyed by |
|---|---|---|---|
| `github-access/` | IAM roles GitHub Actions assumes via OIDC: `github-actions-va-facilities-report-plan` (read-only) and `-deploy` | hand, once, from a laptop | hand only |
| `base/` | SSM parameter holding the VA API key | `terraform` workflow | `teardown` (full) |
| `data/` | S3 data lake (Phase 4: Redshift Serverless) | `terraform` workflow | `teardown` (costs and full) |

Order matters: `github-access/` first (once), then `base/`, then `data/`.
The parts share nothing in Terraform; they only rely on naming conventions
(`va-facilities-report-*`).

## Remote state

State lives in `va-facilities-report-tfstate-<account-id>` (`base/`,
`data/` and `github-access/` each under their own key). The bucket is
versioned (an overwritten or corrupted state file can be restored),
encrypted, private, and created **with the AWS CLI, not Terraform**: state
must never live in something a Terraform teardown could delete. Locking
uses S3 conditional writes (`use_lockfile = true`, Terraform 1.10+), so no
DynamoDB table is needed.

Created once with:

```bash
B=va-facilities-report-tfstate-<account-id>
aws s3api create-bucket --bucket $B --region us-east-1
aws s3api put-public-access-block --bucket $B --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket $B --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket $B --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

Backend blocks cannot use variables, so the bucket name is written out in
each `versions.tf`.

## Credentials

No profile is hardcoded. Locally, set `AWS_PROFILE` (PowerShell:
`$env:AWS_PROFILE = "pipeline-va-facilities-report"`); in GitHub Actions the
OIDC role provides credentials.

## The API key and Terraform state

`base/` creates the parameter with the write-only argument `value_wo`
(placeholder `set-me-with-aws-ssm-put-parameter`). Write-only values are
never stored in state. The real key is written afterwards, outside
Terraform: by the `terraform` workflow from the `VA_API_KEY` secret, or by
hand:

```bash
aws ssm put-parameter --name /va-facilities-report/sandbox/api-key \
  --type SecureString --overwrite --value "<key>"
```

Tested on 2026-11-07 with a throwaway parameter:

| How the parameter got into Terraform | Real value in state? |
|---|---|
| `import` of an existing parameter (even with `value_wo` in the config) | **yes** |
| created by Terraform with `value_wo`, value set later by `put-parameter`, then refreshed | no |

So this parameter must **never be imported**. If it ever has to be
re-adopted, delete it and let Terraform create it, then put the key again.

## GitHub Actions access (`github-access/`)

GitHub issues each workflow job a short-lived OIDC token. AWS trusts
GitHub's OIDC provider and exchanges the token for temporary credentials of
a role, if the token's claims match the role's trust policy. No AWS keys
are stored in GitHub.

- **Plan role**: trusted for tokens with subject `...:pull_request`.
  Read-only, except the state lock file that even `plan` writes.
- **Deploy role**: trusted only for jobs running in the GitHub environment
  `aws` (subject `...:environment:aws`), which only `main` may deploy to. Can manage
  the data lake, the project's SSM parameters, and the `base/` and `data/`
  state files. It cannot touch `github-access/` or its state, so no
  workflow can lock itself out or widen its own permissions.

Role names start with `github-actions-`, not `va-facilities-report-`, so a
later permission like "manage roles named `va-facilities-report-*`" (Phase 4)
can never include the deploy role itself.

Lessons carried over from the previous project
(`pipeline-gcp-to-aws-migration`):

- **One OIDC provider per account.** It already exists (created by that
  project), so it is referenced with a data source, not created. Destroying
  that project's infrastructure would delete it and break this project's
  GitHub access.
- **`sts:TagSession`** must be allowed next to
  `sts:AssumeRoleWithWebIdentity`, or the login fails with a misleading
  "not authorized" error.
- **Two subject formats.** Repositories created after 2026-07-15 send
  `repo:OWNER@ID/REPO@ID:...` instead of `repo:OWNER/REPO:...`. The trust
  policy matches both.
- **Don't destroy the role a workflow runs as.** The previous project's full
  teardown did, so rebuilding needed a laptop. Here `github-access/` is
  outside every teardown.
- `terraform_wrapper: false` in `setup-terraform`, so `terraform output -raw`
  prints plain values in scripts.

### Applying it (once, by hand)

Needs a profile that can create IAM roles and read the OIDC provider (the
pipeline's own IAM user cannot).

```powershell
cd infrastructure\github-access
$env:AWS_PROFILE = "<admin profile>"
terraform init
terraform plan     # 2 roles + 2 inline policies to add
terraform apply
terraform output   # role ARNs for the GitHub settings below
```

### GitHub settings (once)

1. **Settings → Environments → New environment** `aws`:
   restrict deployment branches to `main` (a required reviewer is optional).
2. In that environment, **add secret** `VA_API_KEY` (the VA sandbox key).
3. **Settings → Secrets and variables → Actions → Variables**: add
   `AWS_PLAN_ROLE_ARN` and `AWS_DEPLOY_ROLE_ARN` from `terraform output`.

## Teardown and rebuild

See the project README ("Tearing it down"). In short: the `teardown`
workflow backs up the data lake as a run artifact, then destroys `data/`
(scope `costs`) or `data/` and `base/` (scope `full`); the `terraform`
workflow ("Run workflow") rebuilds both and writes the API key back.
`force_destroy = true` on the bucket lets `destroy` delete it with
objects in it; the typed confirmation and the backup are the
safeguards.
