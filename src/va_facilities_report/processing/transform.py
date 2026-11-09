"""Turn raw API records into flat, typed rows and validate them.

Pure functions only (no I/O), so they are easy to test. Field names follow
the API (docs/api/README.md); nothing is invented.
"""

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import polars as pl

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
KNOWN_OPERATING_STATUSES = {
    "NORMAL", "NOTICE", "LIMITED", "TEMPORARY_CLOSURE", "TEMPORARY_LOCATION",
    "CLOSED", "COMING_SOON", "VIRTUAL_CARE",
}  # fmt: skip

# A measurement is stale when its effectiveDate is more than this many days
# before the ingestion (download) date.
STALE_AFTER_DAYS = 7

FACILITY_SCHEMA: dict[str, pl.DataType] = {
    "facility_id": pl.String(),
    "ingestion_date": pl.Date(),
    "name": pl.String(),
    "facility_type": pl.String(),
    "classification": pl.String(),
    "parent_id": pl.String(),
    "health_care_system": pl.String(),
    "visn": pl.String(),
    "latitude": pl.Float64(),
    "longitude": pl.Float64(),
    "time_zone": pl.String(),
    "mobile": pl.Boolean(),
    "website": pl.String(),
    "address_line1": pl.String(),
    "address_line2": pl.String(),
    "address_line3": pl.String(),
    "city": pl.String(),
    "state": pl.String(),
    "zip": pl.String(),
    "zip5": pl.String(),
    "phone_main": pl.String(),
    "operating_status_code": pl.String(),
    "operating_status_info": pl.String(),
    **{f"hours_{day}": pl.String() for day in WEEKDAYS},
    # The API sends a list of notes (at most 2 seen); joined into one text so
    # the column loads into Redshift and Looker Studio like any other.
    "hours_special_instructions": pl.String(),
    "services_last_updated": pl.Date(),
    "satisfaction_primary_care_urgent": pl.Float64(),
    "satisfaction_primary_care_routine": pl.Float64(),
    "satisfaction_specialty_care_urgent": pl.Float64(),
    "satisfaction_specialty_care_routine": pl.Float64(),
    "satisfaction_effective_date": pl.Date(),
    "satisfaction_age_days": pl.Int32(),
    "satisfaction_is_stale": pl.Boolean(),
    # Coverage tags, from /facilities/{id}/services (see add_service_flags).
    "no_service": pl.Boolean(),
    "no_wait_time_listed": pl.Boolean(),
    "record_hash": pl.String(),
}

SERVICE_SCHEMA: dict[str, pl.DataType] = {
    "facility_id": pl.String(),
    "ingestion_date": pl.Date(),
    "service_id": pl.String(),
    "service_name": pl.String(),
    "service_type": pl.String(),
    "wait_time_new_days": pl.Float64(),
    "wait_time_established_days": pl.Float64(),
    "wait_time_effective_date": pl.Date(),
    "wait_time_age_days": pl.Int32(),
    "wait_time_is_stale": pl.Boolean(),
}


@dataclass(frozen=True)
class Issue:
    table: str
    check: str
    facility_id: str | None
    detail: str
    service_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- facilities -------------------------------------------------------------


def facility_row(record: dict[str, Any], ingestion_date: date) -> dict[str, Any]:
    """Flatten one /facilities record into a row matching FACILITY_SCHEMA."""
    # Pull out the nested sections once. Any section can be missing from the
    # API record, so each falls back to {} and its fields become None.
    attrs = record.get("attributes") or {}
    physical = (attrs.get("address") or {}).get("physical") or {}
    status = attrs.get("operatingStatus") or {}
    hours = attrs.get("hours") or {}
    satisfaction = attrs.get("satisfaction") or {}
    scores = satisfaction.get("health") or {}
    zip_code = physical.get("zip")

    # Satisfaction freshness: stale if older than a week before download.
    satisfaction_date = _date_or_none(satisfaction.get("effectiveDate"))
    satisfaction_age, satisfaction_stale = staleness(
        has_measurement=any(v is not None for v in scores.values()),
        effective_date=satisfaction_date,
        ingestion_date=ingestion_date,
    )

    return {
        # Identity and classification
        "facility_id": record.get("id"),
        "ingestion_date": ingestion_date,
        "name": attrs.get("name"),
        "facility_type": attrs.get("facilityType"),
        "classification": attrs.get("classification"),
        "parent_id": (attrs.get("parent") or {}).get("id"),
        "health_care_system": (attrs.get("healthCareSystem") or {}).get("name"),
        "visn": _str_or_none(attrs.get("visn")),
        # Location and contact
        "latitude": _float_or_none(attrs.get("lat")),
        "longitude": _float_or_none(attrs.get("long")),
        "time_zone": attrs.get("timeZone"),
        "mobile": attrs.get("mobile"),
        "website": attrs.get("website"),
        "address_line1": physical.get("address1"),
        "address_line2": physical.get("address2"),
        "address_line3": physical.get("address3"),
        "city": physical.get("city"),
        "state": physical.get("state"),
        "zip": zip_code,
        "zip5": _zip5(zip_code),
        "phone_main": (attrs.get("phone") or {}).get("main"),
        # Status and opening hours (hours stay free text, as the API sends them)
        "operating_status_code": status.get("code"),
        "operating_status_info": status.get("additionalInfo"),
        **{f"hours_{day}": hours.get(day) for day in WEEKDAYS},
        "hours_special_instructions": _joined_text(
            attrs.get("operationalHoursSpecialInstructions")
        ),
        "services_last_updated": _date_or_none(
            (attrs.get("services") or {}).get("lastUpdated")
        ),
        # Patient satisfaction (null when the API omits a score)
        "satisfaction_primary_care_urgent": _float_or_none(
            scores.get("primaryCareUrgent")
        ),
        "satisfaction_primary_care_routine": _float_or_none(
            scores.get("primaryCareRoutine")
        ),
        "satisfaction_specialty_care_urgent": _float_or_none(
            scores.get("specialtyCareUrgent")
        ),
        "satisfaction_specialty_care_routine": _float_or_none(
            scores.get("specialtyCareRoutine")
        ),
        "satisfaction_effective_date": satisfaction_date,
        "satisfaction_age_days": satisfaction_age,
        "satisfaction_is_stale": satisfaction_stale,
        # Coverage tags: placeholders, filled in by add_service_flags
        "no_service": None,
        "no_wait_time_listed": None,
        # Fingerprint of the source record, for change detection between snapshots.
        "record_hash": hashlib.sha256(
            json.dumps(attrs, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }


def validate_facilities(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[Issue]]:
    """Drop rows that cannot be keyed, null out invalid values, report issues."""
    issues: list[Issue] = []
    seen: set[str] = set()
    clean: list[dict[str, Any]] = []

    def issue(row: dict[str, Any], check: str, detail: str) -> None:
        issues.append(Issue("facilities", check, row.get("facility_id"), detail))

    for row in rows:
        # Fatal checks: a row without a unique id cannot be stored, so drop it.
        facility_id = row["facility_id"]
        if not facility_id:
            issue(row, "missing_id", "record dropped")
            continue
        if facility_id in seen:
            issue(row, "duplicate_id", "duplicate dropped, first kept")
            continue
        seen.add(facility_id)

        # Non-fatal checks: keep the row, report the problem.
        if not row["name"]:
            issue(row, "missing_name", "kept")

        # Coordinates must be on Earth; otherwise null both, as one without
        # the other is useless for maps.
        lat, lon = row["latitude"], row["longitude"]
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            issue(row, "invalid_coordinates", f"lat={lat} long={lon}; set to null")
            row = {**row, "latitude": None, "longitude": None}

        # Report-only checks: values are kept as the API sent them.
        if not row["address_line1"]:
            issue(row, "missing_address", "no physical address")
        if row["zip"] and row["zip5"] is None:
            issue(row, "unparseable_zip", f"zip={row['zip']!r}")
        status = row["operating_status_code"]
        if status not in KNOWN_OPERATING_STATUSES:
            issue(row, "unknown_operating_status", f"code={status!r}")
        clean.append(row)

    return clean, issues


def add_service_flags(
    facilities: list[dict[str, Any]],
    services: list[dict[str, Any]],
    fetched_ids: set[str],
) -> list[dict[str, Any]]:
    """Tag each facility with two independent coverage flags.

    no_service           no services returned by /facilities/{id}/services
    no_wait_time_listed  none of its services has a wait time (so also true
                         when no_service is true)

    Both are null when the facility's services could not be fetched: unknown,
    not false. Staleness is a separate flag and does not affect these.
    """
    # Which facilities have at least one service / one wait time.
    with_services = {row["facility_id"] for row in services}
    with_wait_times = {
        row["facility_id"]
        for row in services
        if row["wait_time_new_days"] is not None
        or row["wait_time_established_days"] is not None
    }
    # Set the flags. `known` is False when the services download failed for
    # this facility (404): then we cannot say, so both stay null.
    flagged = []
    for row in facilities:
        facility_id = row["facility_id"]
        known = facility_id in fetched_ids
        flagged.append(
            {
                **row,
                "no_service": facility_id not in with_services if known else None,
                "no_wait_time_listed": (
                    facility_id not in with_wait_times if known else None
                ),
            }
        )
    return flagged


# --- services and wait times -------------------------------------------------


def service_rows(
    facility_id: str, response: dict[str, Any] | None, ingestion_date: date
) -> tuple[list[dict[str, Any]], list[Issue]]:
    """Flatten one /facilities/{id}/services response into rows.

    A response without a 'data' key means "no services" (documented quirk).
    """
    rows: list[dict[str, Any]] = []
    issues: list[Issue] = []
    for service in (response or {}).get("data") or []:
        info = service.get("serviceInfo") or {}
        wait = service.get("waitTime") or {}  # only ~12 services have one

        # A service without an id cannot be keyed: drop it.
        service_id = info.get("serviceId")
        if not service_id:
            issues.append(
                Issue(
                    "facility_services",
                    "missing_service_id",
                    facility_id,
                    "service dropped",
                )
            )
            continue

        # Base row; wait-time values are filled in by the loop below.
        row = {
            "facility_id": facility_id,
            "ingestion_date": ingestion_date,
            "service_id": service_id,
            "service_name": info.get("name"),
            "service_type": info.get("serviceType"),
            "wait_time_new_days": None,
            "wait_time_established_days": None,
            "wait_time_effective_date": _date_or_none(wait.get("effectiveDate")),
        }

        # Copy both wait times (new / established patients). A value that is
        # present but not a non-negative number is reported and set to null;
        # a missing value is simply null (not an issue).
        for source, column in (
            ("new", "wait_time_new_days"),
            ("established", "wait_time_established_days"),
        ):
            value = _float_or_none(wait.get(source))
            if wait.get(source) is not None and (value is None or value < 0):
                issues.append(
                    Issue(
                        "facility_services",
                        "invalid_wait_time",
                        facility_id,
                        f"{source}={wait.get(source)!r}; set to null",
                        service_id,
                    )
                )
                value = None
            row[column] = value

        # Freshness: stale if the wait time is older than a week before download.
        has_wait_time = (
            row["wait_time_new_days"] is not None
            or row["wait_time_established_days"] is not None
        )
        row["wait_time_age_days"], row["wait_time_is_stale"] = staleness(
            has_measurement=has_wait_time,
            effective_date=row["wait_time_effective_date"],
            ingestion_date=ingestion_date,
        )
        rows.append(row)
    return rows, issues


def deduplicate_services(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[Issue]]:
    seen: set[tuple[str, str]] = set()
    clean, issues = [], []
    for row in rows:
        key = (row["facility_id"], row["service_id"])
        if key in seen:
            issues.append(
                Issue(
                    "facility_services",
                    "duplicate_service",
                    key[0],
                    "duplicate dropped, first kept",
                    key[1],
                )
            )
            continue
        seen.add(key)
        clean.append(row)
    return clean, issues


# --- helpers -----------------------------------------------------------------


def staleness(
    *, has_measurement: bool, effective_date: date | None, ingestion_date: date
) -> tuple[int | None, bool | None]:
    """(age in days, is_stale) of a measurement relative to the download date.

    No measurement -> (None, None): freshness does not apply.
    Measurement without a date -> (None, True): freshness cannot be shown.
    """
    if not has_measurement:
        return None, None
    if effective_date is None:
        return None, True
    age = (ingestion_date - effective_date).days
    return age, age > STALE_AFTER_DAYS


def to_frame(
    rows: list[dict[str, Any]], schema: dict[str, pl.DataType]
) -> pl.DataFrame:
    """Build a DataFrame with a fixed schema (no type guessing from the data)."""
    return (
        pl.DataFrame(rows, schema=schema, orient="row")
        if rows
        else pl.DataFrame(schema=schema)
    )


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _joined_text(value: Any, separator: str = " | ") -> str | None:
    """A list of strings as one text; None for anything empty or not a list."""
    if not isinstance(value, list):
        return None
    parts = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return separator.join(parts) or None


def _date_or_none(value: Any) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if isinstance(value, str) else None
    except ValueError:
        return None


def _zip5(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    digits = value.strip()[:5]
    return digits if len(digits) == 5 and digits.isdigit() else None
