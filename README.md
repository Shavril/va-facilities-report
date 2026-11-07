# VA Facilities Report

A data engineering portfolio project. It collects data from the U.S.
Department of Veterans Affairs (VA) [Facilities API](https://developer.va.gov/explore/api/va-facilities),
stores and models it on AWS (S3 and Redshift), and reports in Looker Studio.

**Analytical goal:** demonstrate how one would find VA health facilities
that may need improvement, based on appointment wait times, patient
satisfaction and operating status, and track how this changes over time.

> **Demonstration only: the data is test data, not reality.**
> This is an independent project, not an official VA product. It uses the
> VA API **sandbox**, a test environment for developers. VA's sandbox is
> populated with test data; it is not guaranteed to be accurate, complete or
> current, and parts of it may be synthetic. The facility directory looks
> real (e.g. `vha_402` is Togus VA Medical Center, Augusta, ME, with its
> real phone number), but measurements such as wait times and satisfaction
> scores cannot be assumed to be real, and they change in ways real data
> would not (see [Day-to-day changes](docs/api/README.md#day-to-day-changes-2026-11-06-vs-2026-11-07)).
>
> **The project demonstrates an approach and engineering capability.** Any
> rankings, findings or conclusions it produces are illustrations of the
> method, **not statements about real VA facilities**, their performance or
> the care Veterans receive. Production data needs separate production API
> access, which this project does not have.

## Status

| Phase | State |
|---|---|
| 1. API exploration | ✅ Done: see [docs/api/README.md](docs/api/README.md) |
| 2. Python ingestion | ✅ Extract + process pipeline |
| 3. S3 data lake | ✅ Bucket + API key in SSM (Terraform, applied via GitHub Actions); pipeline reads/writes S3. Runs from a laptop, no scheduled cloud job yet |
| 4. Redshift warehouse | Planned |
| 5. Looker Studio reporting | Planned |
| 6. Production readiness | Planned |

## Data source: key findings

Full details: [docs/api/README.md](docs/api/README.md).

- **Size:** 2,586 facilities (1,401 health, 435 benefits, 417 Vet Centers,
  333 cemeteries). `GET /facilities` returns up to 1,000 per page, so a full
  pull takes 3 requests. Filtering with `type=health` takes 2.
- **Access:** API key in the `apikey` header. Rate limit is 60 requests/minute.
- **Facility data** (`/facilities`): ID, name, type, classification,
  coordinates, address, phones, opening hours (free text), operating status,
  VISN (regional health network), parent facility, a list of offered
  services, and patient satisfaction scores.
- **Wait times are not in the facility listing.** They come from
  `GET /facilities/{id}/services`, which returns `DetailedService` objects with
  `waitTime` (average days to an appointment for new and established
  patients). That means one request per facility: about 1,400 requests, or
  about 24 minutes per run at the rate limit. They exist only for health
  facilities, for 12 core services (primary care, mental health,
  cardiology, …).
- **Satisfaction scores are unstable in sandbox:** on 1 facility on
  2026-11-06, on 1,086 health facilities the next day, all with the same
  date (2026-03-30). Many urgent primary-care scores are exactly `0.0`,
  which may mean "no data". When missing, the key is omitted, never `null`.
- **Sandbox data changes daily:** wait times and service lists are
  refreshed every day. Satisfaction changed in a different, sudden way;
  an hourly investigation is under way.
- **Not every facility can be assessed:** 142 health facilities (10%, mostly
  mobile units) list no services, and 48 more have services but no wait
  times. They must be reported as "not assessed", not "no wait".
- **Some measurements are stale:** 256 of 6,185 wait times are more than a
  week old, some from 2024. They are flagged with `wait_time_is_stale`.
- **No change timestamps** and no incremental queries. Each run takes a full
  snapshot, and changes are detected by comparing snapshots.
- **Data quality issues:** some facilities have no address; hours are in
  mixed text formats; non-US locations exist (Philippines, South Korea,
  territories); names are not unique (use `id`); mobile facilities may have
  inaccurate locations.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14.

```bash
uv sync
cp .env.example .env        # then fill in the bucket name
uv run pytest
uv run ruff check .
```

Run the API exploration script:

```bash
uv run --env-file .env python scripts/explore_api.py
```

## Running the pipeline

The pipeline is a single command, `va-facilities`, with two subcommands:

```bash
uv run --env-file .env va-facilities run                        # daily job
uv run --env-file .env va-facilities process --date 2026-11-06  # rebuild a saved day
```

- **`run`** downloads today's data from the API (stage 1, ~25 min, limited by
  the 60 requests/minute rate limit), then builds the processed tables
  (stage 2). It always works on today (UTC), because the API only returns
  current data. Re-running it resumes an interrupted download. If the download
  is already complete, it makes no API calls and only rebuilds the tables.
- **`process [--date YYYY-MM-DD]`** rebuilds the processed tables for a saved
  day from its raw files, e.g. after changing processing logic. It makes no API
  calls and takes seconds. The date defaults to today.

Only health facilities are processed, because wait times exist only there.
Storage location is configuration, not a command option: set `DATA_DIR` in
`.env` (default `data/`). `uv run va-facilities --help` shows the same summary.

**Known trade-off:** a download that crosses midnight UTC and fails is not
resumed into the previous day. The next `run` starts a fresh download for the
new day, and the unfinished day is never processed.

### Why the stages are separate

- **Re-processing without re-downloading.** Raw data is the source of truth.
  When processing logic changes (new columns, better hours parsing, bug fixes),
  `process --date ...` rebuilds any past day in seconds. The API only serves
  current data, so past days could not be downloaded again anyway.
- **Different failure modes.** Downloads fail on external problems (rate
  limits, outages), processing on our own bugs. A failed processing step is
  re-run without downloading again.
- **Independent deployment later.** Each stage can become its own scheduled
  job on AWS without restructuring the code.
- An orchestration framework is not needed at this scale.

### Stage 1: extract (raw zone)

Saves the raw API responses unchanged, one partition per facility type and
date. The layout matches the planned S3 bucket:

```
raw/facilities/facility_type=health/ingestion_date=YYYY-MM-DD/page_0001.json
raw/facility_ids/facility_type=health/ingestion_date=YYYY-MM-DD/ids.json
raw/facility_services/facility_type=health/ingestion_date=YYYY-MM-DD/part_00001.jsonl
metadata/ingestion_runs/facility_type=health/ingestion_date=YYYY-MM-DD/manifest.json
```

- **Rate limiting:** requests are spaced about 1 s apart. On HTTP 429 the
  client waits as long as the `ratelimit-reset` header says. Server errors and
  timeouts are retried with exponential backoff.
- **Resumable:** files are written atomically. If a run fails, re-running the
  same command skips finished steps and service batches.
- **Idempotent:** a partition with a manifest is complete, and re-running it
  makes no API calls.
- **Services** are stored as JSON Lines, one facility per line:
  `{facility_id, fetched_at, status_code, response}`. A facility that returns
  404 is recorded and the run continues.

### Stage 2: process (processed zone)

Reads only the raw partition (it refuses to run until extraction has written
its manifest) and writes typed Parquet tables plus a data quality report.
Output is fully rewritten each time, so re-running is idempotent.

```
processed/facilities/facility_type=health/ingestion_date=YYYY-MM-DD/facilities.parquet
processed/facility_services/facility_type=health/ingestion_date=YYYY-MM-DD/facility_services.parquet
metadata/data_quality/facility_type=health/ingestion_date=YYYY-MM-DD/report.json
```

| Table | Grain | Main columns |
|---|---|---|
| `facilities` | facility × ingestion date | id, name, type, classification, parent, VISN, coordinates, address, `zip5`, operating status, opening hours (raw text), satisfaction scores, `satisfaction_age_days`, `satisfaction_is_stale`, `no_service`, `no_wait_time_listed`, `record_hash` |
| `facility_services` | facility × service × ingestion date | service id/name/type, `wait_time_new_days`, `wait_time_established_days`, `wait_time_effective_date`, `wait_time_age_days`, `wait_time_is_stale` |

- **Fixed schemas:** column types are declared in code, not guessed from the
  data, so every day's files are compatible.
- **`record_hash`:** a SHA-256 hash of the source record. It changes only when
  the facility's data changes, which will drive change detection between
  snapshots.
- **Staleness:** `*_age_days` is the number of days between a measurement's
  `effectiveDate` and the ingestion (download) date. `*_is_stale` is `true`
  when that is **more than 7 days**, `false` when fresh, and `null` when there
  is no measurement. A measurement without a date counts as stale. The API
  returns some wait times that are months or years old: 256 of 6,185 on
  2026-11-06.
- **Coverage tags** (on `facilities`, independent of each other and of staleness):
  - `no_service`: the services endpoint returned no services for the facility.
  - `no_wait_time_listed`: none of the facility's services has a wait time.
    This is also `true` when `no_service` is `true`. Facilities with services
    but no wait times: `no_wait_time_listed AND NOT no_service`.
  - Both are `null` when the facility's services could not be downloaded
    (unknown, not `false`).
  - A facility whose wait times are all stale has `no_wait_time_listed = false`.
    Staleness is answered by `wait_time_is_stale`.
  - On 2026-11-06: 142 `no_service`, 190 `no_wait_time_listed` (142 + 48).
    Reports should show these facilities as "not assessed", never as "no wait".
- **Validation:** records without an id or with a duplicate id are dropped.
  Invalid coordinates and invalid wait times (negative or non-numeric) are set
  to null. Missing addresses, unparseable zips, unknown operating statuses,
  facilities whose services could not be fetched, and services for unknown
  facilities are reported. Every issue is listed in the data quality report.
- **Not yet processed:** opening hours stay as raw text (parsing planned), as
  do service locations and booking details (online scheduling, walk-ins,
  referrals).

### Configuration and secrets

Configuration comes from environment variables (see `.env.example`),
loaded from a gitignored `.env` file. Secrets are never committed.

| Variable | Purpose |
|---|---|
| `VA_API_BASE_URL` | Sandbox or production API |
| `VA_API_KEY_SSM_PARAMETER` | SSM parameter holding the API key, read and decrypted at startup |
| `VA_API_KEY` | Optional: the key itself, overriding SSM (e.g. to run without AWS). Not in `.env.example` |
| `DATA_DIR` | `data` (local folder, default) or `s3://<bucket>[/prefix]` |
| `AWS_PROFILE` | AWS CLI profile used for S3 and SSM |

Running against AWS, with no secret in `.env` at all:

```bash
VA_API_BASE_URL=https://sandbox-api.va.gov/services/va_facilities/v1
VA_API_KEY_SSM_PARAMETER=/va-facilities-report/sandbox/api-key
DATA_DIR=s3://va-facilities-report-<account-id>
AWS_PROFILE=pipeline-va-facilities-report
```

The storage layout is identical locally and in S3: the same keys
(`raw/...`, `processed/...`, `metadata/...`) become file paths or object
keys. Both backends write atomically (S3 `PUT` is atomic; locally a temp
file is renamed into place), so resuming works the same way on both.

## AWS infrastructure

Defined with Terraform in [`infrastructure/`](infrastructure/), region
`us-east-1`, split into three parts by **what a teardown removes**. Details,
including why each decision was made: [infrastructure/README.md](infrastructure/README.md).

| Part | Resources | Costs money | Removed by teardown "costs" | Removed by teardown "full" |
|---|---|---|---|---|
| `github-access/` | IAM roles GitHub Actions logs in with (OIDC, no stored keys) | no | no | no |
| `base/` | SSM parameter `/va-facilities-report/sandbox/api-key` | no | no | yes |
| `data/` | S3 data lake `va-facilities-report-<account-id>` (Phase 4: Redshift) | yes | yes | yes |

The data lake bucket blocks all public access, has ACLs disabled, is
encrypted at rest (SSE-S3), rejects non-HTTPS requests, and cleans up
incomplete uploads after 7 days. Nothing else expires: raw snapshots are
the history.

**The API key never enters Terraform state.** Terraform creates the
parameter through a write-only argument (`value_wo`) holding a placeholder;
the real key is written afterwards with `aws ssm put-parameter`. This was
verified: an *imported* parameter does get its value copied into state, a
parameter created with `value_wo` does not, even after a refresh. If the
placeholder is still there, the pipeline stops with a clear error.

**Terraform state** is in a separate S3 bucket
(`va-facilities-report-tfstate-<account-id>`, versioned, encrypted, created
once with the AWS CLI), one state file per part, locked with S3
conditional writes. It is outside every Terraform part, so no teardown can
delete the record of what exists.

### CI/CD (GitHub Actions)

| Workflow | Trigger | What it does |
|---|---|---|
| `ci` | every push and pull request | Ruff, pytest, `terraform fmt` and `validate` (no AWS access) |
| `terraform` | pull request changing `infrastructure/` | `terraform plan` for `base/` and `data/` with a read-only role, shown in the job summary |
| `terraform` | push to `main` changing `infrastructure/`, or manual "Run workflow" (rebuild) | `terraform apply` for `base/` then `data/`, then writes the API key from a GitHub secret into SSM if the parameter holds the placeholder |
| `teardown` | manual, scope `costs` or `full` + typed confirmation | backs up the data lake as a run artifact, then destroys `data/` (and `base/` for `full`) |

Every job that changes AWS runs in the GitHub environment `aws`, which
requires approval. The deploy role trusts only jobs in that environment;
pull requests get the read-only plan role. `github-access/` is applied by
hand and no workflow can modify it, so after a full teardown the
`terraform` workflow can rebuild everything from GitHub.

### Costs

Expected cost: well under $1/month.

- **S3 Standard:** one full health snapshot is about 22 MB raw + 0.25 MB
  processed. Running daily adds roughly 8 GB a year. At S3 Standard's rate
  of roughly $0.02 per GB-month in us-east-1 (check the
  [S3 pricing page](https://aws.amazon.com/s3/pricing/)), that's about
  $0.20/month after a year. Requests are about 25 PUTs per run, which is
  negligible.
- **SSM Parameter Store:** standard parameters and standard-throughput
  API calls have no additional charge
  ([pricing](https://aws.amazon.com/systems-manager/pricing/), checked
  2026-11-07).
- No compute runs in AWS yet; the pipeline runs on a laptop.

### Tearing it down

Use the `teardown` workflow (Actions → teardown → Run workflow):

- **costs**: type `destroy costs`. Removes the data lake (and later
  Redshift).
- **full**: type `destroy everything`. Also removes the API key parameter.

Both download the data lake first and attach it to the run as the artifact
`data-lake-backup`: **download it to your laptop from the run page**. GitHub
deletes artifacts after 90 days. Rebuild with the `terraform` workflow
("Run workflow").

What always stays: `github-access/` (two IAM roles, free) and the state
bucket (a few KB). To remove those too, when done with the project:
`terraform destroy` in `infrastructure/github-access/` with an admin
profile, then delete the state bucket with the AWS CLI.
