"""Processing: raw partition -> validated Parquet tables + data quality report.

Reads only from storage, never from the API, so any past date can be
re-processed after the logic changes. Output is fully rewritten each time,
so re-running is idempotent.

    processed/facilities/facility_type=.../ingestion_date=.../facilities.parquet
    processed/facility_services/facility_type=.../ingestion_date=.../facility_services.parquet
    metadata/data_quality/facility_type=.../ingestion_date=.../report.json
"""

import io
import json
import logging
from collections import Counter
from datetime import date
from typing import Any

import polars as pl

from va_facilities_report.processing.transform import (
    FACILITY_SCHEMA,
    SERVICE_SCHEMA,
    Issue,
    add_service_flags,
    deduplicate_services,
    facility_row,
    service_rows,
    to_frame,
    validate_facilities,
)
from va_facilities_report.storage import Partition, Storage

log = logging.getLogger(__name__)


class RawDataIncompleteError(RuntimeError):
    """The raw partition has no manifest, so extraction did not finish."""


def run_processing(
    storage: Storage, *, facility_type: str, ingestion_date: date
) -> dict[str, Any]:
    """Build the processed tables and data quality report for one day."""
    partition = Partition(facility_type, ingestion_date)

    # 1. Only process a complete download: no manifest means it never finished.
    manifest_key = partition.key("ingestion_runs", "manifest.json", zone="metadata")
    if not storage.exists(manifest_key):
        raise RawDataIncompleteError(
            f"no complete raw extraction for {facility_type} {ingestion_date}; "
            "run `va-facilities run` first"
        )
    manifest = json.loads(storage.read_bytes(manifest_key))

    # 2. Facilities: flatten each raw record into a row, then validate
    #    (drop unusable rows, null invalid values, collect issues).
    facilities, facility_issues = validate_facilities(
        [
            facility_row(r, ingestion_date)
            for r in _raw_facility_records(storage, partition)
        ]
    )

    # 3. Services: one row per facility and service, with wait times.
    services, service_issues, fetched_ids = _process_services(
        storage, partition, manifest
    )

    # 4. Tag facilities with no_service / no_wait_time_listed. Needs the
    #    service rows, which is why it runs after step 3.
    facilities = add_service_flags(facilities, services, fetched_ids)

    # 5. Cross-table check: services that belong to no known facility.
    known_ids = {row["facility_id"] for row in facilities}
    service_issues += [
        Issue(
            "facility_services",
            "unknown_facility",
            fid,
            "services for a facility missing from /facilities",
        )
        for fid in sorted({row["facility_id"] for row in services} - known_ids)
    ]

    # 6. Write both tables as Parquet with their fixed schemas. Overwrites any
    #    earlier output for this day, so re-processing is idempotent.
    facilities_df = to_frame(facilities, FACILITY_SCHEMA)
    services_df = to_frame(services, SERVICE_SCHEMA)
    _write_parquet(
        storage,
        partition.key("facilities", "facilities.parquet", "processed"),
        facilities_df,
    )
    _write_parquet(
        storage,
        partition.key("facility_services", "facility_services.parquet", "processed"),
        services_df,
    )

    # 7. Data quality report: row counts, issue counts by check, and every
    #    individual issue.
    issues = facility_issues + service_issues
    report = {
        "facility_type": facility_type,
        "ingestion_date": ingestion_date.isoformat(),
        "environment": manifest.get("environment"),
        "row_counts": {
            "facilities": facilities_df.height,
            "facility_services": services_df.height,
            "services_with_wait_time": services_df.filter(
                pl.col("wait_time_new_days").is_not_null()
                | pl.col("wait_time_established_days").is_not_null()
            ).height,
            "stale_wait_times": services_df.filter(pl.col("wait_time_is_stale")).height,
            "facilities_no_service": facilities_df.filter(pl.col("no_service")).height,
            "facilities_no_wait_time_listed": facilities_df.filter(
                pl.col("no_wait_time_listed")
            ).height,
        },
        "issue_counts": dict(Counter(f"{i.table}.{i.check}" for i in issues)),
        "issues": [i.to_dict() for i in issues],
    }
    storage.write_bytes(
        partition.key("data_quality", "report.json", zone="metadata"),
        json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    log.info(
        "processing complete",
        extra={**report["row_counts"], "issue_counts": report["issue_counts"]},
    )
    return report


def _raw_facility_records(
    storage: Storage, partition: Partition
) -> list[dict[str, Any]]:
    """All facility records from the saved pages (_SUCCESS holds the count)."""
    page_count = int(storage.read_bytes(partition.key("facilities", "_SUCCESS")))
    records = []
    for number in range(1, page_count + 1):
        page = json.loads(
            storage.read_bytes(partition.key("facilities", f"page_{number:04d}.json"))
        )
        records.extend(page["data"])
    return records


def _process_services(
    storage: Storage, partition: Partition, manifest: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[Issue], set[str]]:
    """Returns service rows, issues, and the ids whose services were fetched."""
    # The manifest says how many batch files the download wrote.
    batch_count = (manifest.get("services") or {}).get("batches", 0)
    rows: list[dict[str, Any]] = []
    issues: list[Issue] = []
    fetched_ids: set[str] = set()
    for number in range(1, batch_count + 1):
        data = storage.read_bytes(
            partition.key("facility_services", f"part_{number:05d}.jsonl")
        )
        # Each line is one facility's services response.
        for line in data.decode("utf-8").splitlines():
            if not line:
                continue
            entry = json.loads(line)
            # Facility returned 404 during download: no data, report it. It is
            # left out of fetched_ids, so its service flags become null.
            if entry["status_code"] != 200:
                issues.append(
                    Issue(
                        "facility_services",
                        "services_not_fetched",
                        entry["facility_id"],
                        f"HTTP {entry['status_code']}",
                    )
                )
                continue
            # Flatten the response into one row per service.
            fetched_ids.add(entry["facility_id"])
            new_rows, new_issues = service_rows(
                entry["facility_id"], entry["response"], partition.ingestion_date
            )
            rows += new_rows
            issues += new_issues
    # Duplicates are only detectable once all batches are read.
    rows, duplicate_issues = deduplicate_services(rows)
    return rows, issues + duplicate_issues, fetched_ids


def _write_parquet(storage: Storage, key: str, frame: pl.DataFrame) -> None:
    # Serialize in memory, then hand bytes to storage, so the same code works
    # for local disk and S3.
    buffer = io.BytesIO()
    frame.write_parquet(buffer, compression="zstd")
    storage.write_bytes(key, buffer.getvalue())
    log.info("parquet written", extra={"key": key, "rows": frame.height})
