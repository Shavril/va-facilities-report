import io
import json
from datetime import date

import polars as pl
import pytest

from va_facilities_report.processing.pipeline import (
    RawDataIncompleteError,
    run_processing,
)
from va_facilities_report.storage import LocalStorage, Partition

DAY = date(2026, 11, 6)
PARTITION = Partition("health", DAY)


def write_raw(storage, pages, service_lines):
    """Write a raw partition the way extraction does."""
    for number, records in enumerate(pages, start=1):
        storage.write_bytes(
            PARTITION.key("facilities", f"page_{number:04d}.json"),
            json.dumps({"data": records}).encode(),
        )
    storage.write_bytes(
        PARTITION.key("facilities", "_SUCCESS"), str(len(pages)).encode()
    )
    storage.write_bytes(
        PARTITION.key("facility_services", "part_00001.jsonl"),
        "\n".join(json.dumps(line) for line in service_lines).encode(),
    )
    manifest = {"environment": "sandbox", "services": {"batches": 1}}
    storage.write_bytes(
        PARTITION.key("ingestion_runs", "manifest.json", "metadata"),
        json.dumps(manifest).encode(),
    )


def facility(facility_id, **attrs):
    return {
        "id": facility_id,
        "attributes": {
            "name": facility_id,
            "lat": 40.0,
            "long": -75.0,
            "address": {"physical": {"address1": "1 Main St", "zip": "19104"}},
            "operatingStatus": {"code": "NORMAL"},
            **attrs,
        },
    }


def services_line(facility_id, status_code=200, services=()):
    response = {"data": list(services)} if status_code == 200 else None
    return {
        "facility_id": facility_id,
        "status_code": status_code,
        "response": response,
    }


def read_parquet(storage, key):
    return pl.read_parquet(io.BytesIO(storage.read_bytes(key)))


def test_writes_parquet_tables_and_report(tmp_path) -> None:
    storage = LocalStorage(tmp_path)
    write_raw(
        storage,
        pages=[[facility("vha_1")], [facility("vha_2", address={})]],
        service_lines=[
            services_line(
                "vha_1",
                services=[
                    {
                        "serviceInfo": {"serviceId": "primaryCare"},
                        "waitTime": {
                            "new": 12,
                            "established": 3,
                            "effectiveDate": "2026-11-05",
                        },
                    }
                ],
            ),
            services_line("vha_2", status_code=404),
        ],
    )

    report = run_processing(storage, facility_type="health", ingestion_date=DAY)

    facilities = read_parquet(
        storage, PARTITION.key("facilities", "facilities.parquet", "processed")
    )
    services = read_parquet(
        storage,
        PARTITION.key("facility_services", "facility_services.parquet", "processed"),
    )
    assert facilities["facility_id"].to_list() == ["vha_1", "vha_2"]
    assert services.row(0, named=True)["wait_time_new_days"] == 12.0
    assert report["row_counts"] == {
        "facilities": 2,
        "facility_services": 1,
        "services_with_wait_time": 1,
        "stale_wait_times": 0,
        "facilities_no_service": 0,
        "facilities_no_wait_time_listed": 0,
    }
    assert report["issue_counts"] == {
        "facilities.missing_address": 1,
        "facility_services.services_not_fetched": 1,
    }
    assert storage.exists(PARTITION.key("data_quality", "report.json", "metadata"))


def test_reports_services_for_unknown_facility(tmp_path) -> None:
    storage = LocalStorage(tmp_path)
    write_raw(
        storage,
        pages=[[facility("vha_1")]],
        service_lines=[
            services_line("vha_9", services=[{"serviceInfo": {"serviceId": "x"}}])
        ],
    )

    report = run_processing(storage, facility_type="health", ingestion_date=DAY)

    assert report["issue_counts"] == {"facility_services.unknown_facility": 1}


def test_reprocessing_is_idempotent(tmp_path) -> None:
    storage = LocalStorage(tmp_path)
    write_raw(
        storage, pages=[[facility("vha_1")]], service_lines=[services_line("vha_1")]
    )
    key = PARTITION.key("facilities", "facilities.parquet", "processed")

    run_processing(storage, facility_type="health", ingestion_date=DAY)
    first = read_parquet(storage, key)
    run_processing(storage, facility_type="health", ingestion_date=DAY)

    assert read_parquet(storage, key).equals(first)


def test_refuses_incomplete_raw_partition(tmp_path) -> None:
    with pytest.raises(RawDataIncompleteError, match="va-facilities run"):
        run_processing(
            LocalStorage(tmp_path), facility_type="health", ingestion_date=DAY
        )
