# VA Facilities API — exploration findings

Explored on 2026-11-06 against the **sandbox** environment
(`https://sandbox-api.va.gov/services/va_facilities/v1`).
Reproduce with `uv run --env-file .env python scripts/explore_api.py`.
Sample responses are in [`samples/`](samples/).

> **Sandbox data is test data.** VA states it "is not guaranteed to be up-to-date".
> Nothing in this project should be read as current production VA data.

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
| `satisfaction` (patient survey scores) | 1 facility only | — | — | — |

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
- `services.lastUpdated` (health only) and `waitTime.effectiveDate` were both
  2026-11-05, the day before exploration. They may just be the date of the
  sandbox's last refresh, not real change dates. To be verified.
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

**Sandbox limitation:** only **1 of 2,586** facilities has `satisfaction`
(`vha_573GO`, a health facility; no Vet Center, benefits office or cemetery
has it). We have not verified whether production has better coverage. In
sandbox, satisfaction cannot support facility comparisons.

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

The `0.0` is an explicit value in the data. With a single example we cannot
tell a real 0% from a placeholder. Processing keeps it as `0.0` and maps
omitted scores to `null`, so "no data" and "zero" stay distinguishable.

## Stale measurements (first full run, 2026-11-06)

Wait times and satisfaction carry an `effectiveDate`, and **it is not always
recent**. The API keeps returning old measurements:

- **Wait times:** 5,929 of 6,185 are ≤7 days old (5,922 dated 2026-11-05).
  **256 are older**, some going back to **May 2024**.
- 14 facilities have only stale wait times. 196 have a mix of fresh and stale.
- **Satisfaction:** the only value is 221 days old.
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

## Open questions

1. Does sandbox data change between days? A baseline snapshot from 2026-11-06
   is in `data/exploration/` (gitignored). Compare it with a later pull.
   Most wait-time `effectiveDate`s were 2026-11-05, which suggests daily
   refreshes, but 256 were older (see "Stale measurements").
2. What is satisfaction coverage in production?
3. What is the production rate limit? It needs a production access request
   and is out of scope for now.
