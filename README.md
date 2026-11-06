# VA Facilities Report

A data engineering portfolio project. It collects data from the U.S.
Department of Veterans Affairs (VA) [Facilities API](https://developer.va.gov/explore/api/va-facilities),
stores and models it on AWS (S3 and Redshift), and reports in Looker Studio.

**Analytical goal:** find VA health facilities that may need improvement,
based on appointment wait times, patient satisfaction and operating status,
and track how this changes over time.

> Independent project, not an official VA product. It currently uses
> **sandbox** API data, which VA does not guarantee to be up to date. Results
> do not describe the real state of VA facilities.

## Status

| Phase | State |
|---|---|
| 1. API exploration | ✅ Done: see [docs/api/README.md](docs/api/README.md) |
| 2. Python ingestion | ✅ Extract + process pipeline (local storage) |
| 3. S3 data lake | ⏳ Next |
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
- **Satisfaction scores** are on only 1 facility in sandbox, so they are not
  usable for comparisons there. When missing, the key is omitted, never
  `null` or zero.
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
cp .env.example .env        # then fill in VA_API_KEY
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

Configuration comes from environment variables (see `.env.example`).
Locally they are loaded from a gitignored `.env` file. On AWS (planned)
they will come from SSM Parameter Store. Secrets are never committed.
