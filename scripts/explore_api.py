"""Phase 1 exploration of the VA Facilities API.

Saves representative sample responses to docs/api/samples/ and prints a
summary of counts, pagination limits and fields per facility type.

Usage:
    uv run --env-file .env python scripts/explore_api.py
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from va_facilities_report.config import load_settings

FACILITY_TYPES = ["health", "benefits", "cemetery", "vet_center"]
SAMPLES_DIR = Path(__file__).resolve().parents[1] / "docs" / "api" / "samples"
SETTINGS = load_settings()


def get(path: str, **params: Any) -> tuple[int, dict[str, str], Any]:
    """GET a path; returns status, headers and parsed JSON body."""
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{SETTINGS.va_api_base_url}{path}" + (f"?{query}" if query else "")
    request = urllib.request.Request(url, headers={"apikey": SETTINGS.va_api_key})
    time.sleep(1.1)  # stay under the 60 requests/minute limit
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, dict(response.headers), json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        return error.code, dict(error.headers), body


def save(name: str, body: Any) -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / f"{name}.json"
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")


def key_paths(value: Any, prefix: str = "") -> set[str]:
    """Flatten nested dict keys into dotted paths (lists marked with [])."""
    if isinstance(value, dict):
        paths = set()
        for key, child in value.items():
            paths |= {f"{prefix}{key}"} | key_paths(child, f"{prefix}{key}.")
        return paths
    if isinstance(value, list):
        return set().union(*(key_paths(item, f"{prefix[:-1]}[].") for item in value))
    return set()


def main() -> None:
    print("== Facility counts (/ids) ==")
    _, _, all_ids = get("/ids")
    print(f"all: {len(all_ids['data'])}")
    prefixes = Counter(facility_id.split("_")[0] for facility_id in all_ids["data"])
    print(f"by id prefix: {dict(prefixes)}")
    for facility_type in FACILITY_TYPES:
        _, _, ids = get("/ids", type=facility_type)
        print(f"{facility_type}: {len(ids['data'])}")

    print("\n== per_page limits ==")
    for per_page in (100, 500, 1000, 5000):
        status, _, body = get("/facilities", per_page=per_page)
        detail = (
            f"returned {len(body['data'])}, meta {body['meta']['pagination']}"
            if status == 200
            else str(body)[:200]
        )
        print(f"per_page={per_page}: HTTP {status} {detail}")

    print("\n== Fields per facility type (first 200 records each) ==")
    for facility_type in FACILITY_TYPES:
        _, _, body = get("/facilities", type=facility_type, per_page=200)
        save(f"facilities_{facility_type}_page1", {**body, "data": body["data"][:3]})
        counts = Counter(
            path for record in body["data"] for path in key_paths(record["attributes"])
        )
        total = len(body["data"])
        print(f"\n-- {facility_type} ({total} records sampled) --")
        for path, count in sorted(counts.items()):
            print(f"  {path}: {count}/{total}")
        statuses = Counter(
            record["attributes"].get("operatingStatus", {}).get("code")
            for record in body["data"]
        )
        print(f"  operatingStatus codes: {dict(statuses)}")

    print("\n== Other endpoints ==")
    for name, path, params in [
        ("facility_by_id", "/facilities/vha_358", {}),
        ("facility_services", "/facilities/vha_358/services", {}),
        ("facility_service_detail", "/facilities/vha_358/services/cardiology", {}),
        ("facility_not_found", "/facilities/vha_doesnotexist", {}),
        ("ids_cemetery", "/ids", {"type": "cemetery"}),
    ]:
        status, _, body = get(path, **params)
        save(name, body)
        print(f"{path}: HTTP {status}")


if __name__ == "__main__":
    main()
