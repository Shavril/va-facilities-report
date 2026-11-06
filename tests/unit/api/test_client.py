import httpx
import pytest

from tests.conftest import facility, page
from va_facilities_report.api import ApiError, UnexpectedResponseError


def test_sends_api_key_header(make_client) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["apikey"] = request.headers.get("apikey")
        return httpx.Response(200, json={"data": ["vha_1"]})

    make_client(handler).get_facility_ids("health")

    assert seen["apikey"] == "test-key"


def test_iter_facility_pages_follows_all_pages(make_client) -> None:
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params["page"])
        requested.append(dict(request.url.params))
        return httpx.Response(200, json=page([facility(f"vha_{number}")], number, 3))

    pages = list(make_client(handler).iter_facility_pages("health"))

    assert [p["data"][0]["id"] for p in pages] == ["vha_1", "vha_2", "vha_3"]
    assert requested[0] == {"per_page": "1000", "type": "health", "page": "1"}


def test_iter_facility_pages_stops_on_empty_page(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([], 1, 99))

    assert len(list(make_client(handler).iter_facility_pages())) == 1


def test_missing_pagination_is_unexpected(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    with pytest.raises(UnexpectedResponseError, match="pagination"):
        list(make_client(handler).iter_facility_pages())


def test_non_json_response_is_unexpected(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(UnexpectedResponseError, match="not JSON"):
        make_client(handler).get_facility_ids()


def test_services_without_data_key_is_returned_as_is(make_client) -> None:
    body = {"links": {}, "meta": {"pagination": {"totalPages": 1, "totalEntries": 0}}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/facilities/vc_1/services")
        return httpx.Response(200, json=body)

    assert make_client(handler).get_facility_services("vc_1") == body


def test_rate_limit_waits_for_reset_header(make_client, clock) -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"ratelimit-reset": "7"}),
            httpx.Response(200, json={"data": []}),
        ]
    )

    make_client(lambda request: next(responses)).get_facility_ids()

    assert 8.0 in clock.sleeps  # reset + 1s margin


def test_retries_server_errors_with_backoff(make_client, clock) -> None:
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(500),
            httpx.Response(200, json={"data": []}),
        ]
    )

    make_client(lambda request: next(responses), min_interval=0).get_facility_ids()

    assert clock.sleeps == [1.0, 2.0]


def test_retries_timeouts(make_client) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"data": []})

    make_client(handler).get_facility_ids()

    assert len(calls) == 2


def test_gives_up_after_max_retries(make_client) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(502)

    with pytest.raises(ApiError, match="after 3 attempts") as error:
        make_client(handler, max_retries=2).get_facility_ids()

    assert error.value.status_code == 502
    assert len(calls) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_not_retried(make_client, status) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"errors": [{"title": "nope"}]})

    with pytest.raises(ApiError) as error:
        make_client(handler).get_facility_services("vha_x")

    assert error.value.status_code == status
    assert "nope" in error.value.body
    assert len(calls) == 1


def test_throttle_spaces_requests(make_client, clock) -> None:
    client = make_client(
        lambda request: httpx.Response(200, json={"data": []}), min_interval=1.0
    )

    for _ in range(3):
        client.get_facility_ids()

    assert clock.sleeps == [1.0, 1.0]
