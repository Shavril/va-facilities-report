from datetime import date

import polars as pl
import pytest

from va_facilities_report.processing.transform import (
    FACILITY_SCHEMA,
    SERVICE_SCHEMA,
    add_service_flags,
    deduplicate_services,
    facility_row,
    service_rows,
    to_frame,
    validate_facilities,
)

DAY = date(2026, 11, 6)


def raw_facility(facility_id="vha_358", **overrides):
    attributes = {
        "name": "Manila VA Clinic",
        "facilityType": "va_health_facility",
        "classification": "Other Outpatient Services (OOS)",
        "parent": {"id": "vha_358", "link": "..."},
        "lat": 14.54408,
        "long": 120.99139,
        "timeZone": "Asia/Manila",
        "address": {
            "physical": {
                "zip": "01302-1234",
                "city": "Pasay City",
                "state": "PH",
                "address1": "1501 Roxas Boulevard",
            }
        },
        "phone": {"main": "808-433-5254"},
        "hours": {"monday": "730AM-430PM", "sunday": "Closed"},
        "services": {"health": [], "lastUpdated": "2026-11-05"},
        "mobile": False,
        "operatingStatus": {"code": "NORMAL"},
        "visn": "21",
        "healthCareSystem": {"name": "VA Manila health care"},
        "satisfaction": {
            "health": {"primaryCareRoutine": 0.78},
            "effectiveDate": "2026-03-30",
        },
    }
    attributes.update(overrides)
    return {"id": facility_id, "type": "va_facilities", "attributes": attributes}


def test_facility_row_flattens_fields() -> None:
    row = facility_row(raw_facility(), DAY)

    assert row["facility_id"] == "vha_358"
    assert row["ingestion_date"] == DAY
    assert row["latitude"] == 14.54408
    assert row["state"] == "PH"
    assert row["zip"] == "01302-1234"
    assert row["zip5"] == "01302"
    assert row["hours_monday"] == "730AM-430PM"
    assert row["hours_tuesday"] is None
    assert row["parent_id"] == "vha_358"
    assert row["services_last_updated"] == date(2026, 11, 5)
    assert row["satisfaction_primary_care_routine"] == 0.78
    assert row["satisfaction_primary_care_urgent"] is None
    assert row["satisfaction_effective_date"] == date(2026, 3, 30)


@pytest.mark.parametrize(
    ("notes", "expected"),
    [
        (["Mon-Fri 8-4:30."], "Mon-Fri 8-4:30."),
        (["Closed holidays. ", "Call ahead."], "Closed holidays. | Call ahead."),
        ([], None),
        (["", "  "], None),
        (None, None),
        ("not a list", None),
    ],
)
def test_special_instructions_become_one_text(notes, expected) -> None:
    record = raw_facility(operationalHoursSpecialInstructions=notes)

    assert facility_row(record, DAY)["hours_special_instructions"] == expected


def test_facility_row_handles_missing_sections() -> None:
    record = {
        "id": "vha_568PB",
        "attributes": {"name": "Sturgis", "lat": 44.4, "long": -103.4},
    }

    row = facility_row(record, DAY)

    assert row["address_line1"] is None
    assert row["operating_status_code"] is None
    assert row["satisfaction_effective_date"] is None


def test_record_hash_changes_only_when_source_changes() -> None:
    first = facility_row(raw_facility(), DAY)["record_hash"]
    same = facility_row(raw_facility(), date(2026, 11, 7))["record_hash"]
    renamed = facility_row(raw_facility(name="New name"), DAY)["record_hash"]

    assert first == same
    assert first != renamed


def test_validation_drops_duplicates_and_missing_ids() -> None:
    rows = [
        facility_row(r, DAY)
        for r in [raw_facility("vha_1"), raw_facility("vha_1"), {"attributes": {}}]
    ]

    clean, issues = validate_facilities(rows)

    assert [r["facility_id"] for r in clean] == ["vha_1"]
    assert {i.check for i in issues} >= {"duplicate_id", "missing_id"}


def test_validation_nulls_invalid_coordinates() -> None:
    rows = [
        facility_row(raw_facility(lat=123.0), DAY),
        facility_row(raw_facility("vha_2", long="abc"), DAY),
    ]

    clean, issues = validate_facilities(rows)

    assert all(r["latitude"] is None and r["longitude"] is None for r in clean)
    assert [i.check for i in issues] == ["invalid_coordinates", "invalid_coordinates"]


def test_validation_flags_missing_address_and_unknown_status() -> None:
    row = facility_row(raw_facility(address={}, operatingStatus={"code": "WEIRD"}), DAY)

    _, issues = validate_facilities([row])

    assert {i.check for i in issues} == {"missing_address", "unknown_operating_status"}


def test_valid_facility_has_no_issues() -> None:
    _, issues = validate_facilities([facility_row(raw_facility(), DAY)])

    assert issues == []


def services_response(*services):
    return {"data": list(services), "meta": {"pagination": {"totalPages": 1}}}


def test_service_rows_extract_wait_times() -> None:
    response = services_response(
        {
            "serviceInfo": {
                "serviceId": "cardiology",
                "name": "Cardiology",
                "serviceType": "health",
            },
            "waitTime": {"new": 61, "established": 11, "effectiveDate": "2026-11-05"},
        },
        {
            "serviceInfo": {
                "serviceId": "addiction",
                "name": "Addiction",
                "serviceType": "health",
            }
        },
        {
            "serviceInfo": {"serviceId": "mentalHealth"},
            "waitTime": {"established": 0, "effectiveDate": "2026-11-05"},
        },
    )

    rows, issues = service_rows("vha_402", response, DAY)

    assert issues == []
    assert [r["service_id"] for r in rows] == [
        "cardiology",
        "addiction",
        "mentalHealth",
    ]
    assert rows[0]["wait_time_new_days"] == 61.0
    assert rows[0]["wait_time_effective_date"] == date(2026, 11, 5)
    assert rows[1]["wait_time_new_days"] is None  # service without wait times
    assert rows[2]["wait_time_new_days"] is None  # 'new' missing
    assert rows[2]["wait_time_established_days"] == 0.0  # zero is valid


def test_service_rows_without_data_key_is_empty() -> None:
    rows, issues = service_rows("vc_1", {"meta": {}}, DAY)

    assert rows == [] and issues == []


def test_invalid_wait_times_are_nulled_and_reported() -> None:
    response = services_response(
        {"serviceInfo": {"serviceId": "a"}, "waitTime": {"new": -5, "established": "x"}}
    )

    rows, issues = service_rows("vha_1", response, DAY)

    assert rows[0]["wait_time_new_days"] is None
    assert rows[0]["wait_time_established_days"] is None
    assert [i.check for i in issues] == ["invalid_wait_time", "invalid_wait_time"]


def test_duplicate_services_are_dropped() -> None:
    response = services_response(
        {"serviceInfo": {"serviceId": "a"}}, {"serviceInfo": {"serviceId": "a"}}
    )
    rows, _ = service_rows("vha_1", response, DAY)

    clean, issues = deduplicate_services(rows)

    assert len(clean) == 1
    assert issues[0].check == "duplicate_service"


def test_frames_have_fixed_schema_even_when_empty() -> None:
    assert to_frame([], FACILITY_SCHEMA).schema == pl.Schema(FACILITY_SCHEMA)
    assert to_frame([], SERVICE_SCHEMA).schema == pl.Schema(SERVICE_SCHEMA)

    frame = to_frame([facility_row(raw_facility(), DAY)], FACILITY_SCHEMA)
    assert frame.schema == pl.Schema(FACILITY_SCHEMA)


def wait_service(effective_date, **wait):
    return {
        "serviceInfo": {"serviceId": "primaryCare"},
        "waitTime": {
            "new": 10,
            "established": 2,
            **wait,
            "effectiveDate": effective_date,
        },
    }


@pytest.mark.parametrize(("effective_date", "age", "stale"), [
    ("2026-11-05", 1, False),
    ("2026-10-30", 7, False),  # exactly one week: still fresh
    ("2026-10-29", 8, True),
    ("2024-05-20", 900, True),
])  # fmt: skip
def test_wait_time_staleness(effective_date, age, stale) -> None:
    rows, _ = service_rows(
        "vha_1", services_response(wait_service(effective_date)), DAY
    )

    assert rows[0]["wait_time_age_days"] == age
    assert rows[0]["wait_time_is_stale"] is stale


def test_staleness_is_null_without_wait_time() -> None:
    response = services_response({"serviceInfo": {"serviceId": "addiction"}})

    rows, _ = service_rows("vha_1", response, DAY)

    assert rows[0]["wait_time_is_stale"] is None
    assert rows[0]["wait_time_age_days"] is None


def test_wait_time_without_date_counts_as_stale() -> None:
    response = services_response(
        {"serviceInfo": {"serviceId": "a"}, "waitTime": {"new": 5}}
    )

    rows, _ = service_rows("vha_1", response, DAY)

    assert rows[0]["wait_time_is_stale"] is True


def test_satisfaction_staleness() -> None:
    stale = facility_row(raw_facility(), DAY)  # effectiveDate 2026-03-30
    fresh = facility_row(
        raw_facility(
            satisfaction={
                "health": {"primaryCareRoutine": 0.5},
                "effectiveDate": "2026-11-04",
            }
        ),
        DAY,
    )
    absent = facility_row(raw_facility(satisfaction=None), DAY)

    assert (stale["satisfaction_age_days"], stale["satisfaction_is_stale"]) == (
        221,
        True,
    )
    assert fresh["satisfaction_is_stale"] is False
    assert absent["satisfaction_is_stale"] is None


def test_service_flags_are_independent() -> None:
    facilities = [
        facility_row(raw_facility(fid), DAY)
        for fid in ("with_wait", "no_wait", "no_service", "stale", "unfetched")
    ]
    services = []
    for fid, service in [
        ("with_wait", wait_service("2026-11-05")),
        ("no_wait", {"serviceInfo": {"serviceId": "laboratory"}}),
        ("stale", wait_service("2024-05-20")),
    ]:
        services += service_rows(fid, services_response(service), DAY)[0]
    fetched = {"with_wait", "no_wait", "no_service", "stale"}

    flagged = {
        r["facility_id"]: (r["no_service"], r["no_wait_time_listed"])
        for r in add_service_flags(facilities, services, fetched)
    }

    assert flagged == {
        "with_wait": (False, False),
        "no_wait": (False, True),
        "no_service": (True, True),
        "stale": (False, False),  # stale wait times are still listed
        "unfetched": (None, None),  # unknown, not false
    }
