import json
from datetime import date

import httpx
import pytest

from tests.conftest import BASE_URL, facility, page
from va_facilities_report.api import ApiError
from va_facilities_report.ingestion.extract import run_extraction
from va_facilities_report.storage import LocalStorage

DAY = date(2026, 11, 6)
PART = "facility_type=health/ingestion_date=2026-11-06"


class FakeApi:
    """Serves a tiny dataset and records every request path."""

    def __init__(self, ids: list[str], per_page: int = 2) -> None:
        self.ids = ids
        self.per_page = per_page
        self.calls: list[str] = []
        self.fail_services_for: set[str] = set()
        self.missing: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/services/va_facilities/v1")
        self.calls.append(path)
        if path == "/facilities":
            number = int(request.url.params["page"])
            chunks = [
                self.ids[i : i + self.per_page]
                for i in range(0, len(self.ids), self.per_page)
            ]
            records = [facility(i) for i in chunks[number - 1]]
            return httpx.Response(200, json=page(records, number, len(chunks)))
        if path == "/ids":
            return httpx.Response(200, json={"data": self.ids})
        facility_id = path.split("/")[2]
        if facility_id in self.fail_services_for:
            return httpx.Response(503)
        if facility_id in self.missing:
            return httpx.Response(404, json={"errors": [{"title": "not found"}]})
        services = [
            {
                "serviceInfo": {"serviceId": "primaryCare"},
                "waitTime": {"new": 10, "established": 2},
            }
        ]
        return httpx.Response(200, json={**page(services, 1, 1)})

    def service_calls(self) -> list[str]:
        return [c for c in self.calls if c.endswith("/services")]


def run(make_client, api, storage, **kwargs):
    client = make_client(api, max_retries=0, min_interval=0)
    return run_extraction(
        client,
        storage,
        facility_type="health",
        ingestion_date=DAY,
        api_base_url=BASE_URL,
        batch_size=2,
        **kwargs,
    )


def read_jsonl(storage, key):
    return [json.loads(line) for line in storage.read_bytes(key).decode().splitlines()]


def test_full_run_writes_raw_layout_and_manifest(make_client, tmp_path) -> None:
    api = FakeApi(["vha_1", "vha_2", "vha_3"])
    storage = LocalStorage(tmp_path)

    manifest = run(make_client, api, storage)

    assert storage.exists(f"raw/facilities/{PART}/page_0001.json")
    assert storage.exists(f"raw/facilities/{PART}/page_0002.json")
    assert storage.exists(f"raw/facility_ids/{PART}/ids.json")
    lines = read_jsonl(storage, f"raw/facility_services/{PART}/part_00001.jsonl")
    assert [line["facility_id"] for line in lines] == ["vha_1", "vha_2"]
    assert lines[0]["response"]["data"][0]["waitTime"]["new"] == 10
    assert storage.exists(f"raw/facility_services/{PART}/part_00002.jsonl")

    assert manifest["facility_count"] == 3
    assert manifest["environment"] == "sandbox"
    assert manifest["id_check"]["missing_from_facilities"] == []
    assert storage.exists(f"metadata/ingestion_runs/{PART}/manifest.json")


def test_raw_pages_are_stored_unchanged(make_client, tmp_path) -> None:
    api = FakeApi(["vha_1"])
    storage = LocalStorage(tmp_path)

    run(make_client, api, storage)

    stored = json.loads(storage.read_bytes(f"raw/facilities/{PART}/page_0001.json"))
    assert stored == page([facility("vha_1")], 1, 1)


def test_completed_partition_is_not_fetched_again(make_client, tmp_path) -> None:
    api = FakeApi(["vha_1", "vha_2"])
    storage = LocalStorage(tmp_path)
    run(make_client, api, storage)
    api.calls.clear()

    run(make_client, api, storage)

    assert api.calls == []


def test_interrupted_run_resumes_without_refetching(make_client, tmp_path) -> None:
    api = FakeApi(["vha_1", "vha_2", "vha_3", "vha_4"])
    storage = LocalStorage(tmp_path)
    api.fail_services_for = {"vha_3"}  # second batch fails
    with pytest.raises(ApiError):
        run(make_client, api, storage)
    assert not storage.exists(f"raw/facility_services/{PART}/part_00002.jsonl")

    api.fail_services_for = set()
    api.calls.clear()
    manifest = run(make_client, api, storage)

    assert "/facilities" not in api.calls  # pages reused from storage
    assert api.service_calls() == [
        "/facilities/vha_3/services",
        "/facilities/vha_4/services",
    ]
    assert manifest["services"]["batches_skipped"] == 1


def test_facility_missing_from_services_is_recorded(make_client, tmp_path) -> None:
    api = FakeApi(["vha_1", "vha_2"])
    api.missing = {"vha_2"}
    storage = LocalStorage(tmp_path)

    manifest = run(make_client, api, storage)

    lines = read_jsonl(storage, f"raw/facility_services/{PART}/part_00001.jsonl")
    assert lines[1]["status_code"] == 404
    assert lines[1]["response"] is None
    assert manifest["services"]["not_found"] == ["vha_2"]


def test_ids_mismatch_is_reported(make_client, tmp_path) -> None:
    api = FakeApi(["vha_1", "vha_2"])
    real_handler = api.__call__

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ids"):
            return httpx.Response(200, json={"data": ["vha_1", "vha_2", "vha_9"]})
        return real_handler(request)

    client = make_client(handler, max_retries=0, min_interval=0)
    manifest = run_extraction(
        client,
        LocalStorage(tmp_path),
        facility_type="health",
        ingestion_date=DAY,
        api_base_url=BASE_URL,
    )

    assert manifest["id_check"]["missing_from_facilities"] == ["vha_9"]


def test_limited_run_writes_no_manifest_and_full_run_completes_it(
    make_client, tmp_path
) -> None:
    api = FakeApi(["vha_1", "vha_2", "vha_3"])
    storage = LocalStorage(tmp_path)

    assert run(make_client, api, storage, limit=1) is None
    assert not storage.exists(f"metadata/ingestion_runs/{PART}/manifest.json")

    manifest = run(make_client, api, storage)

    # The limited batch held only vha_1, so it is re-fetched as a full batch.
    lines = read_jsonl(storage, f"raw/facility_services/{PART}/part_00001.jsonl")
    assert [line["facility_id"] for line in lines] == ["vha_1", "vha_2"]
    assert manifest["services"]["batches_skipped"] == 0
