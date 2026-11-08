# VA Facilities API — exploration findings

Explored on 2026-11-06 against the **sandbox** environment
(`https://sandbox-api.va.gov/services/va_facilities/v1`).
Reproduce with `uv run --env-file .env python scripts/explore_api.py`.
Sample responses are in [`samples/`](samples/).

> **Sandbox data is test data.** VA states it "is not guaranteed to be up-to-date".
> Nothing in this project should be read as current production VA data.

## What the sandbox data is

The VA API sandbox (Lighthouse platform) is a development environment:
developers test integrations against it before requesting production
access. VA describes sandbox environments as populated with test data, free
of personal or health information, and in parts synthetic. For this API
that means, as far as we can observe:

- **The facility directory looks real.** IDs, names, addresses and phone
  numbers match real facilities (e.g. `vha_402`, Togus VA Medical Center,
  Augusta, ME, 207-623-8411).
- **Measurements cannot be trusted as real.** Nothing in the responses says
  whether a wait time or satisfaction score is a real measurement, a copy
  of an old one, or generated. They also behave unlike real data: satisfaction
  scores for 1,085 facilities appeared overnight, all dated 2026-03-30 (see
  "Day-to-day changes").

Consequence for the project: it is a **demonstration of the approach**
(ingestion, history, data quality, modelling, reporting). Analytical results
built on sandbox data illustrate the method and say nothing about real VA
facilities.

## Access

- **Auth:** API key in the `apikey` HTTP header.
- **Rate limit:** 60 requests/minute (`ratelimit-limit`, `ratelimit-remaining`,
  `ratelimit-reset` response headers). Exceeding it returns HTTP 429.
- **OpenAPI spec:** `GET {base_url}/docs` (needs the API key). The developer
  portal page is a JavaScript app and does not expose the spec directly.
- **Responses:** JSON:API style: `data`, `links` (self/first/prev/next/last),
  `meta.pagination` (currentPage, perPage, totalPages, totalEntries).

## Endpoints

| Endpoint | Purpose | Relevance |
|---|---|---|
| `GET /facilities` | List/search facilities; no filters = all | **Main ingestion source** |
| `GET /ids?type=` | All facility IDs (no paging) | Completeness check, deletion detection |
| `GET /facilities/{id}` | Single facility | Re-fetch / debugging |
| `GET /facilities/{id}/services` | Services for a facility, **with wait times** | Optional enrichment (see below) |
| `GET /facilities/{id}/services/{serviceId}` | One service, with wait times | Optional enrichment |
| `GET /nearby?lat=&long=&drive_time=` | Health facilities by drive time | Not needed (point queries) |

Filters on `/facilities`: `type` (health, benefits, cemetery, vet_center),
`state`, `zip`, `visn`, `lat`/`long`/`radius`, `bbox[]`, `services[]`,
`mobile`, `facilityIds`.

## Volume and pagination

| Type | ID prefix | Count |
|---|---|---|
| health | `vha_` | 1,401 |
| benefits | `vba_` | 435 |
| vet_center | `vc_` | 417 |
| cemetery | `nca_` | 333 |
| **total** | | **2,586** |

- `per_page` maximum is **1000** (higher returns HTTP 400). Default is 10.
- A full pull is **3 requests, about 7 seconds**. Pages of 1000 came back
  complete: 2,586 rows, 2,586 unique IDs, no duplicates or gaps.
- The dataset is small. Plain Python is enough; Spark is not needed.

## Record structure

All types share: `name`, `facilityType`, `lat`, `long`, `timeZone`,
`address.physical`, `phone.main`, `hours.<weekday>`, `operatingStatus.code`.
Fields differ by type:

| Field | health | benefits | cemetery | vet_center |
|---|---|---|---|---|
| `classification` | ✓ | ✓ | ✓ | — |
| `address.mailing` | — | — | ✓ | — |
| `services.health[]` | ✓ (most) | — | — | ✓ |
| `services.benefits[]` | — | ✓ | — | rare |
| `services.lastUpdated` | ✓ | — | — | — |
| `parent` (parent facility) | ✓ | — | — | ✓ |
| `visn` (VA health network region) | ✓ | — | — | ✓ |
| `healthCareSystem.name` | ✓ | — | — | — |
| `mobile` | ✓ | — | — | ✓ |
| `operatingStatus.supplementalStatus[]` | — | — | ✓ | — |
| `phone.pharmacy`, `.mentalHealthClinic`, … | ✓ | — | — | — |
| `satisfaction` (patient survey scores) | 1 facility on 2026-11-06, 1,086 on 2026-11-07 | — | — | — |

`operatingStatus.code` values seen: `NORMAL`, `NOTICE`, `LIMITED`,
`TEMPORARY_CLOSURE`, `TEMPORARY_LOCATION`, `CLOSED`, `COMING_SOON`, `VIRTUAL_CARE`.

`services` contains 115 distinct `serviceId`s. The API says service IDs never change.

## Data quality observations

- **Missing address:** 4 health facilities have no `address` at all
  (e.g. `vha_568PB`). Coordinates are still present.
- **Coordinates:** none missing, none out of range, none at 0/0.
- **Mobile facilities:** 190 (`mobile: true`). Per VA, their coordinates and
  address may be inaccurate.
- **Hours are free text in mixed formats:** `800AM-430PM`,
  `8:00 a.m. - 4:00 p.m.`, `24/7`, `Closed`, `Sunrise - Sunset`, `Dawn - Dusk`,
  or missing. Parsing them into structured times needs its own normalisation step.
- **Zip:** mix of 5-digit and ZIP+4 (`79720-5566`). One 9-digit value with no dash.
- **States:** 59 distinct values, including territories and non-US locations
  (`PH` Philippines, `KR` South Korea, `GU`, `AS`, `MP`, `VI`).
- **Names are not unique:** 50 names occur more than once, so `id` is the only key.
- **Hierarchy:** 1,678 facilities have a `parent` other than themselves
  (e.g. clinics under a medical center).

## Change detection

- **No record-level modification timestamp.** There is no `updatedAt` or
  similar on facilities, and no "changed since" filter.
- `services.lastUpdated` (health only) and `waitTime.effectiveDate` mostly
  equal the day before the download (2026-11-05 on 11-06, 2026-11-06 on
  11-07). They are the date of the sandbox's last batch refresh, not real
  change dates. See "Day-to-day changes" below.
- **Cheap "is there anything new?" check:** if `services.lastUpdated` is the
  same as in the last stored run, the data has not been refreshed. In 42
  hourly runs, the data never changed without this date moving.
- **Implication:** incremental extraction is not possible. Each run does a
  full pull (3 requests, which is cheap) and detects changes by **comparing
  snapshots** (e.g. a hash of each record). Facilities missing from `/ids`
  compared with the previous run are treated as deleted.

## Detailed services and wait times (`DetailedService`, `PatientWaitTime`)

The `/facilities` listing has only a **summary** of services: `serviceId`,
`name` and a `link`. Full service details, including wait times, come only from:

- `GET /facilities/{facilityId}/services`: all `DetailedService` objects for
  one facility (paginated; `per_page=1000` returns everything in one page)
- `GET /facilities/{facilityId}/services/{serviceId}`: a single one

No endpoint returns detailed services for many facilities at once, so you
need **one request per facility**.

`DetailedService` fields (from the spec; sandbox presence from a 20-facility sample):

| Field | Content | In sandbox |
|---|---|---|
| `serviceInfo` | `serviceId`, `name`, `serviceType` | always |
| `waitTime` (`PatientWaitTime`) | `new`, `established` (average days to a non-urgent appointment), `effectiveDate` | health facilities only, about 12 services each |
| `serviceLocations[]` | address, phones, hours, `onlineSchedulingAvailable`, `referralRequired`, `walkInsAccepted` | most health and Vet Center services |
| `appointmentPhones[]`, `appointmentLeadIn` | how to book (lead-in may contain HTML) | most health services |
| `lastUpdated`, `path` | CMS upload time, page URL | rare |

**Wait times exist only for health (`vha_`) facilities**, and only for
these services: audiology, cardiology, dermatology, gastroenterology,
gynecology, mentalHealth, ophthalmology, optometry, orthopedics, primaryCare,
urology, womensHealth. Vet Centers and benefits offices return services
with no wait times. Smaller clinics may have none (e.g. `vha_565QD`).

Quirk: a facility with zero services returns `links` and `meta` but **no
`data` key**. The client must treat this as an empty list, not an error.

**Cost:** 1,401 health facilities means 1,401 requests, about 24 minutes at
60 requests/minute.

## Patient satisfaction (`satisfaction`)

On `/facilities` records. `satisfaction.health` holds `primaryCareUrgent`,
`primaryCareRoutine`, `specialtyCareUrgent` and `specialtyCareRoutine`. Each
is the share of Veterans who say they usually or always get an appointment
when needed. There is also an `effectiveDate`.

**Coverage is unstable in sandbox.** On 2026-11-06 only **1 of 2,586**
facilities had `satisfaction` (`vha_573GO`). On 2026-11-07, **1,086 of 1,401
health facilities** had it (still no Vet Center, benefits office or
cemetery). The `effectiveDate` stayed `2026-03-30` for all of them, so this
looks like an existing dataset being (re)loaded, not new survey results.
It has not changed since: the wait-time refresh on 2026-11-08 left every
satisfaction value untouched (see "Hourly snapshots" below).

Coverage by score on 2026-11-07 (health facilities):

| Score | Present | Exactly `0.0` | Above 0 |
|---|---|---|---|
| `primaryCareRoutine` | 1,084 | 96 | 988 |
| `primaryCareUrgent` | 1,083 | **673** | 410 |
| `specialtyCareRoutine` | 140 | 1 | 139 |
| `specialtyCareUrgent` | 140 | 1 | 139 |

Scores range from 0.0 to 1.0. The large number of exact zeros for urgent
primary care (against 96 for routine) suggests `0.0` often means "no data"
rather than 0% of Veterans getting an appointment. Not confirmed.

How it looks when missing: **the `satisfaction` key is omitted entirely**. It
is never `null`, `{}` or zeros (checked on all 1,401 health facilities,
2026-11-06). Individual scores are omitted the same way. The one facility with
satisfaction has only two of the four scores:

```json
"satisfaction": {
  "health": { "primaryCareUrgent": 0.0, "primaryCareRoutine": 0.78 },
  "effectiveDate": "2026-03-30"
}
```

The `0.0` is an explicit value in the data. Processing keeps it as `0.0` and
maps omitted scores to `null`, so "no data" and "zero" stay distinguishable
and the interpretation of `0.0` can be decided later, in SQL.

## Stale measurements (first full run, 2026-11-06)

Wait times and satisfaction carry an `effectiveDate`, and **it is not always
recent**. The API keeps returning old measurements:

- **Wait times:** 5,929 of 6,185 are ≤7 days old (5,922 dated 2026-11-05).
  **256 are older**, some going back to **May 2024**.
- 14 facilities have only stale wait times. 196 have a mix of fresh and stale.
- **Satisfaction:** dated 2026-03-30 (221 days old on 2026-11-06), so every
  satisfaction score is flagged stale.
- No wait time lacks an `effectiveDate`, and none is dated in the future.

Processing therefore adds `*_age_days` and `*_is_stale` columns (see the
project README). A measurement is **stale when its `effectiveDate` is more
than 7 days before the ingestion (download) date**.

`services.lastUpdated` is deliberately **not** flagged. It is the date the
service *list* last changed, not a measurement, so an old date there is
normal.

## Facilities without services (first full run, 2026-11-06)

**142 of 1,401 health facilities (10%) list no services at all.**

- In `/facilities`, 135 have no `services` key and 7 have one with empty lists.
- `/facilities/{id}/services` returns no `data` key for exactly the same
  142 facilities. Both sources agree on whether a facility has services.
- Service counts match between the two sources for 1,400 of 1,401
  facilities. The exception is `vha_552GB`: the listing shows 4 services, the
  services endpoint 10 (it adds addiction, laboratory, mentalHealth, ptsd,
  socialWork and telehealth). **The services endpoint is the more complete
  source.**

| Classification | Without services |
|---|---|
| Other Outpatient Services (OOS), mostly mobile medical units | 123 |
| Residential Care Site (MH RRTP/DRRTP) | 12 |
| Primary Care CBOC | 4 |
| VA Medical Center (VAMC) | 2 (e.g. `vha_463A5` Anchorage Midtown) |
| Community Living Center | 1 |

- 91 of the 142 are mobile, which is almost half of all mobile facilities.
- 127 are `NORMAL` status, so a missing service list does not mean closed. It
  means the services are not published.
- A further **48 facilities list services but have no wait times**, because
  none of their services is one of the 12 wait-time services.

**Implication:** a facility without wait-time data must be reported as "not
assessed", never as "no wait". Processing tags this with two independent
booleans on `facilities`, `no_service` and `no_wait_time_listed` (see the
project README).

## Day-to-day changes (2026-11-06 vs 2026-11-07)

Two full health runs, compared:

| Run | Started (UTC) | Finished (UTC) |
|---|---|---|
| 2026-11-06 | 18:55:55 | 19:23:44 |
| 2026-11-07 | 02:43:46 | 03:08:33 |

The changes happened somewhere in those ~7 hours between runs.

| What | Change |
|---|---|
| Facilities | none added or removed (1,401 both days) |
| `satisfaction` | appeared on **1,085** more facilities (1 → 1,086), all dated 2026-03-30 |
| `services.lastUpdated` | moved 2026-11-05 → 2026-11-06 on 1,228 facilities |
| Service list | 12 services added, 3 removed (e.g. `vha_437GJ` urology added, `vha_459QA` mentalHealth removed) |
| Wait times | `effectiveDate` moved on 5,918 services (now mostly 2026-11-06); `new` days changed on 2,679, `established` on 1,648 |
| Other attributes | no changes (names, addresses, coordinates, hours, status) |

**Reading:** wait times and the service list look like a regular **daily
refresh**. Satisfaction behaves differently: a large, sudden change with an
unchanged date.

### Hourly snapshots (2026-11-07 to 2026-11-08)

To find out when and how the data changes, the full pipeline ran **every
hour** (identical code) and each run was compared with the previous one
(`scripts/compare_snapshots.py`, field by field, derived columns excluded).
42 runs, 2026-11-06 18:55 to 2026-11-08 19:00 UTC:

| Between runs (UTC) | Changes |
|---|---|
| 11-06 18:55 → 11-07 02:43 | data date 2026-11-05 → **2026-11-06**, satisfaction appears (above) |
| 11-07 02:43 → 11-08 12:00 (34 runs) | **none**, every value identical |
| 11-08 12:00 → 11-08 13:00 | data date 2026-11-06 → **2026-11-08** (see below) |
| 11-08 13:00 → 11-08 19:00 (6 runs) | **none** |

The 2026-11-08 refresh:

| What | Change |
|---|---|
| `services.lastUpdated` | 2026-11-06 → 2026-11-08 on all 1,228 facilities that have it |
| Wait-time `effectiveDate` | → 2026-11-08 on 5,927 services |
| Wait-time values | `new` changed on 3,419 services, `established` on 2,375; typically by ±1–2 days (median −1), extremes −149 / +124 |
| Wait times appearing / disappearing | `new` went value ↔ null on 60 services, `established` on 15 |
| Service list | 31 services added, 6 removed (18,219 → 18,244 rows) |
| Satisfaction, facility attributes | **no changes** (names, addresses, coordinates, hours, status) |

Findings:

1. **Refreshes are whole-dataset batches.** All facilities move to the same
   new date at once. No hourly run ever saw a half-refreshed dataset.
2. **The refresh time is not fixed.** The first fell between 19:23 and
   02:43 UTC; the second between **12:24 and 13:00 UTC** (the 12:00 run
   finished at 12:24 with old data, the 13:00 run had new data from its first
   request).
3. **Days can be skipped.** There was no data dated 2026-11-07 (a Saturday);
   the date jumped from 11-06 to 11-08.
4. **Only wait-time data changes daily.** About 30% of wait-time values
   move per refresh. A service row exists only when the facility publishes it
   that day: rows with no wait times at all also disappear and reappear, and
   4 rows flipped (removed in one refresh, back in the next, or the reverse,
   e.g. `vha_438GE` cardiology).
5. **Satisfaction was a one-off load** (effective date 2026-03-30) and has
   not changed since.

**Implications for the pipeline:**

- **One run a day is enough.** Hourly runs mostly produce identical copies.
  With the observed refresh times, a run in the afternoon (UTC) is the most
  likely to pick up the day's data, but this is based on two refreshes only.
- History comes from **comparing snapshots** (the record hashes already
  support this), and a changed `services.lastUpdated` signals a new refresh.
- A run takes ~25 minutes, so it could straddle a refresh. A data quality
  check that all facilities in one run share the same `services.lastUpdated`
  would catch a mixed snapshot. Not observed so far.

The hourly runs continue, to see whether refresh time and skipped days
differ on weekdays.

## Open questions

1. Is there a regular refresh time on weekdays, and are weekends skipped?
   Under investigation with hourly snapshots.
2. What is satisfaction coverage in production? Does `0.0` mean "no data"?
3. What is the production rate limit? It needs a production access request
   and is out of scope for now.
