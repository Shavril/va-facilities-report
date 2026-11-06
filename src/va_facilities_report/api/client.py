"""HTTP client for the VA Facilities API.

Returns raw API responses (parsed JSON) unchanged, so the raw layer can store
exactly what the API sent. Interpreting the content is the job of processing.

Handles:
- throttling to stay under the rate limit (60 requests/minute in sandbox)
- 429 responses: waits for the `ratelimit-reset` / `Retry-After` header
- retries with exponential backoff on 5xx, timeouts and connection errors
"""

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any, Self

import httpx

log = logging.getLogger(__name__)

FACILITY_TYPES = ("health", "benefits", "cemetery", "vet_center")
MAX_PER_PAGE = 1000  # API rejects larger values with HTTP 400

type JsonDict = dict[str, Any]


class ApiError(Exception):
    """Non-retryable HTTP error, or retries exhausted."""

    def __init__(self, message: str, status_code: int | None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class UnexpectedResponseError(Exception):
    """The response is not shaped the way the API documents it."""


class VAFacilitiesClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        min_interval: float = 1.05,
        max_retries: int = 5,
        max_backoff: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={"apikey": api_key, "accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._max_backoff = max_backoff
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # --- public endpoints -------------------------------------------------

    def iter_facility_pages(
        self, facility_type: str | None = None, per_page: int = MAX_PER_PAGE
    ) -> Iterator[JsonDict]:
        """Yield each raw page of GET /facilities until the last page."""
        params: JsonDict = {"per_page": per_page}
        if facility_type:
            params["type"] = facility_type
        page = 1
        while True:
            body = self.get_json("/facilities", {**params, "page": page})
            pagination = _pagination(body)  # also validates the response shape
            yield body
            # Stop after the last page. The empty-page check guards against an
            # endless loop if totalPages were ever wrong.
            if page >= pagination["totalPages"] or not body["data"]:
                return
            page += 1

    def get_facility_ids(self, facility_type: str | None = None) -> JsonDict:
        """Raw GET /ids response: {"data": [id, ...]}."""
        params = {"type": facility_type} if facility_type else {}
        body = self.get_json("/ids", params)
        if not isinstance(body.get("data"), list):
            raise UnexpectedResponseError("/ids response has no 'data' list")
        return body

    def get_facility_services(self, facility_id: str) -> JsonDict:
        """Raw GET /facilities/{id}/services response (all services, one page).

        Note: for a facility with no services the API omits the 'data' key.
        """
        body = self.get_json(
            f"/facilities/{facility_id}/services", {"per_page": MAX_PER_PAGE}
        )
        if _pagination(body)["totalPages"] > 1:
            # per_page=1000 has always been enough; fail loudly if not.
            raise UnexpectedResponseError(f"{facility_id}: services span >1 page")
        return body

    # --- transport --------------------------------------------------------

    def get_json(self, path: str, params: JsonDict | None = None) -> JsonDict:
        """GET with throttling and retries; returns the parsed JSON object."""
        # Attempt 0 is the first try; up to max_retries more follow.
        for attempt in range(self._max_retries + 1):
            # Wait if the previous request was too recent (rate limit).
            self._throttle()

            # Send the request. Each branch below either returns, raises, or
            # sets `reason` and `wait` for a retry.
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as error:  # timeouts, connection errors
                # No response at all: retry with backoff.
                reason = f"{type(error).__name__}: {error}"
                wait = self._backoff(attempt)
                status = None
            else:
                # Success.
                if response.status_code == 200:
                    return _parse_json_object(response)
                status = response.status_code
                reason = f"HTTP {status}"
                if status == 429:
                    # Rate limited: wait as long as the API tells us to.
                    wait = _rate_limit_wait(response) or self._backoff(attempt)
                elif status >= 500:
                    # Server error, often temporary: retry with backoff.
                    wait = self._backoff(attempt)
                else:
                    # Other 4xx (bad request, auth, not found): retrying would
                    # give the same answer, so fail immediately.
                    raise ApiError(
                        f"GET {path} failed: {reason}", status, response.text[:1000]
                    )

            # Retryable failure: give up if this was the last attempt...
            if attempt == self._max_retries:
                raise ApiError(
                    f"GET {path} failed after {attempt + 1} attempts: {reason}", status
                )
            # ...otherwise log, wait and loop for the next attempt.
            log.warning(
                "request failed, retrying",
                extra={
                    "path": path,
                    "reason": reason,
                    "attempt": attempt + 1,
                    "wait_seconds": round(wait, 2),
                },
            )
            self._sleep(wait)
        raise AssertionError("unreachable")

    def _throttle(self) -> None:
        """Keep at least `min_interval` seconds between request starts."""
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self._min_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
                now += remaining
        self._last_request_at = now

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff: 1, 2, 4, 8 ... seconds, capped at max_backoff."""
        return min(2.0**attempt, self._max_backoff)


def _rate_limit_wait(response: httpx.Response) -> float | None:
    """Seconds to wait according to the API's rate-limit headers."""
    for header in ("ratelimit-reset", "retry-after"):
        value = response.headers.get(header)
        if value and value.strip().isdigit():
            return float(value) + 1.0  # +1s margin for clock skew
    return None


def _parse_json_object(response: httpx.Response) -> JsonDict:
    try:
        body = response.json()
    except ValueError as error:
        raise UnexpectedResponseError(f"response is not JSON: {error}") from error
    if not isinstance(body, dict):
        raise UnexpectedResponseError("response JSON is not an object")
    return body


def _pagination(body: JsonDict) -> JsonDict:
    try:
        pagination = body["meta"]["pagination"]
        int(pagination["totalPages"])
    except (KeyError, TypeError, ValueError) as error:
        raise UnexpectedResponseError(
            "response has no valid meta.pagination"
        ) from error
    if "data" in body and not isinstance(body["data"], list):
        raise UnexpectedResponseError("response 'data' is not a list")
    return pagination
