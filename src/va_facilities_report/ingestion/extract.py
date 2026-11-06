"""Daily raw extraction: one full snapshot of a facility type per ingestion date.

Steps (each saves raw API responses unchanged):
1. GET /facilities?type=...      -> raw/facilities/.../page_NNNN.json
2. GET /ids?type=...             -> raw/facility_ids/.../ids.json
3. GET /facilities/{id}/services -> raw/facility_services/.../part_NNNNN.jsonl
4. run summary                   -> metadata/ingestion_runs/.../manifest.json

The API offers no incremental queries, so every run is a full snapshot.
Re-running for the same date resumes: finished steps and service batches are
skipped, so an interrupted ~25 minute run does not start over. A date that
already has a manifest is complete and is not fetched again.
"""

import json
import logging
import time
from datetime import UTC, date, datetime
from typing import Any

from va_facilities_report.api import ApiError, VAFacilitiesClient
from va_facilities_report.storage import Partition, Storage

log = logging.getLogger(__name__)


def run_extraction(
    client: VAFacilitiesClient,
    storage: Storage,
    *,
    facility_type: str,
    ingestion_date: date,
    api_base_url: str,
    batch_size: int = 100,
    limit: int | None = None,
    include_services: bool = True,
) -> dict[str, Any] | None:
    """Run (or resume) the extraction. Returns the manifest for a full run,
    None for a partial run (`limit` set or services skipped)."""
    partition = Partition(facility_type, ingestion_date)

    # A manifest means this day was fully downloaded before: no API calls.
    manifest_key = partition.key("ingestion_runs", "manifest.json", zone="metadata")
    if storage.exists(manifest_key):
        log.info(
            "partition already complete, nothing to do", extra={"key": manifest_key}
        )
        return json.loads(storage.read_bytes(manifest_key))

    # Wall-clock time for the manifest, monotonic clock for the duration
    # (monotonic is not affected by system clock changes).
    started_at = _now()
    started = time.monotonic()

    # Steps 1 and 2: facility pages, then /ids as a completeness cross-check.
    # Each step skips itself if its files already exist (resume).
    facility_ids = extract_facilities(client, storage, partition)
    id_check = extract_and_check_ids(client, storage, partition, facility_ids)

    # Step 3: services per facility, the slow part (~1 request per facility).
    # Sorted so batch contents are the same on every run, which is what
    # makes skipping finished batches on resume safe.
    services_summary = None
    if include_services:
        targets = sorted(facility_ids)[:limit] if limit else sorted(facility_ids)
        services_summary = extract_services(
            client, storage, partition, targets, batch_size
        )

    # A partial run must not look complete, so it writes no manifest.
    if limit is not None or not include_services:
        log.info("partial run finished; no manifest written")
        return None

    # Step 4: the manifest. Written last, so it only exists when every file
    # above is in place; processing relies on that.
    manifest = {
        "facility_type": facility_type,
        "ingestion_date": ingestion_date.isoformat(),
        "api_base_url": api_base_url,
        "environment": "sandbox" if "sandbox" in api_base_url else "production",
        "started_at": started_at,
        "finished_at": _now(),
        "duration_seconds": round(time.monotonic() - started, 1),
        "facility_count": len(facility_ids),
        "id_check": id_check,
        "services": services_summary,
    }
    storage.write_bytes(manifest_key, _json_bytes(manifest))
    log.info("extraction complete", extra={"key": manifest_key})
    return manifest


def extract_facilities(
    client: VAFacilitiesClient, storage: Storage, partition: Partition
) -> list[str]:
    """Save every /facilities page; return the facility ids they contain."""
    success_key = partition.key("facilities", "_SUCCESS")
    if storage.exists(success_key):
        # Resume: pages were saved by an earlier attempt today. Read them back
        # instead of re-downloading, so ids match the batches already saved.
        page_count = int(storage.read_bytes(success_key))
        pages = [
            json.loads(storage.read_bytes(partition.key("facilities", _page_name(n))))
            for n in range(1, page_count + 1)
        ]
        log.info("facilities already extracted", extra={"pages": page_count})
    else:
        # Fresh download: save each page as soon as it arrives.
        pages = []
        for number, page in enumerate(
            client.iter_facility_pages(partition.facility_type), start=1
        ):
            storage.write_bytes(
                partition.key("facilities", _page_name(number)), _json_bytes(page)
            )
            pages.append(page)
            log.info(
                "facilities page saved",
                extra={"page": number, "records": len(page["data"])},
            )
        # Written last: marks the page set as complete and records its size.
        storage.write_bytes(success_key, str(len(pages)).encode())

    # Collect the ids for step 3. Pages should not overlap; warn if they do
    # (e.g. data changed between page requests).
    ids = [record["id"] for page in pages for record in page["data"]]
    if len(ids) != len(set(ids)):
        log.warning(
            "duplicate facility ids across pages",
            extra={"duplicates": len(ids) - len(set(ids))},
        )
    return list(dict.fromkeys(ids))  # de-duplicated, order kept


def extract_and_check_ids(
    client: VAFacilitiesClient,
    storage: Storage,
    partition: Partition,
    facility_ids: list[str],
) -> dict[str, Any]:
    """Save /ids and compare it with the ids found in /facilities pages."""
    # Download /ids, or reuse it if an earlier attempt today saved it.
    key = partition.key("facility_ids", "ids.json")
    if storage.exists(key):
        body = json.loads(storage.read_bytes(key))
    else:
        body = client.get_facility_ids(partition.facility_type)
        storage.write_bytes(key, _json_bytes(body))

    # Compare both directions. A mismatch is recorded, not fatal: the
    # download is still usable, but the manifest shows what was missing.
    listed, from_pages = set(body["data"]), set(facility_ids)
    check = {
        "ids_endpoint_count": len(listed),
        "missing_from_facilities": sorted(listed - from_pages),
        "missing_from_ids": sorted(from_pages - listed),
    }
    if check["missing_from_facilities"] or check["missing_from_ids"]:
        log.warning("/ids and /facilities disagree", extra=check)
    return check


def extract_services(
    client: VAFacilitiesClient,
    storage: Storage,
    partition: Partition,
    facility_ids: list[str],
    batch_size: int,
) -> dict[str, Any]:
    """Fetch services per facility, saved as JSON Lines in fixed-size batches.

    A batch file is written only once complete, so on resume any existing
    batch with the expected facility ids is skipped.
    """
    # Split the ids into fixed-size chunks: batch 1 = ids 0-99, batch 2 = ...
    batches = [
        facility_ids[i : i + batch_size]
        for i in range(0, len(facility_ids), batch_size)
    ]
    summary = {
        "facilities": len(facility_ids),
        "batches": len(batches),
        "batches_skipped": 0,
        "not_found": [],
    }

    for number, batch in enumerate(batches, start=1):
        # Skip a batch saved by an earlier attempt, but only if it holds
        # exactly the expected ids. A different batch (e.g. from a --limit
        # test run, or a different batch size) is re-fetched and replaced.
        key = partition.key("facility_services", f"part_{number:05d}.jsonl")
        if storage.exists(key) and _batch_ids(storage.read_bytes(key)) == batch:
            summary["batches_skipped"] += 1
            continue

        # Fetch the whole batch in memory, then save it in one write: a batch
        # file either exists complete or not at all.
        lines = []
        for facility_id in batch:
            line = _fetch_services_line(client, facility_id)
            if line["status_code"] == 404:
                summary["not_found"].append(facility_id)
            lines.append(json.dumps(line, ensure_ascii=False))
        storage.write_bytes(key, ("\n".join(lines) + "\n").encode("utf-8"))
        log.info(
            "services batch saved",
            extra={"batch": number, "of": len(batches), "facilities": len(batch)},
        )

    return summary


def _fetch_services_line(
    client: VAFacilitiesClient, facility_id: str
) -> dict[str, Any]:
    """One JSON Lines record: the raw response plus when and how it was fetched."""
    fetched_at = _now()
    try:
        response = client.get_facility_services(facility_id)
    except ApiError as error:
        if error.status_code != 404:
            raise  # retries exhausted or unexpected error: stop, resume later
        # Facility disappeared between steps 1 and 3; record it, keep going.
        log.warning("facility not found", extra={"facility_id": facility_id})
        return {
            "facility_id": facility_id,
            "fetched_at": fetched_at,
            "status_code": 404,
            "response": None,
            "error": error.body,
        }
    return {
        "facility_id": facility_id,
        "fetched_at": fetched_at,
        "status_code": 200,
        "response": response,
    }


def _batch_ids(data: bytes) -> list[str]:
    return [
        json.loads(line)["facility_id"]
        for line in data.decode("utf-8").splitlines()
        if line
    ]


def _page_name(number: int) -> str:
    return f"page_{number:04d}.json"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
