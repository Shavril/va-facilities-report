from collections.abc import Callable
from typing import Any

import httpx
import pytest

from va_facilities_report.api import VAFacilitiesClient

BASE_URL = "https://sandbox-api.va.gov/services/va_facilities/v1"

type Handler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    """Replaces time.monotonic/time.sleep so tests never actually wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_client(clock: FakeClock) -> Callable[..., VAFacilitiesClient]:
    def factory(handler: Handler, **kwargs: Any) -> VAFacilitiesClient:
        return VAFacilitiesClient(
            BASE_URL,
            "test-key",
            transport=httpx.MockTransport(handler),
            sleep=clock.sleep,
            clock=clock.time,
            **kwargs,
        )

    return factory


def page(
    records: list[dict[str, Any]], current: int, total_pages: int
) -> dict[str, Any]:
    """A /facilities-shaped page response."""
    return {
        "data": records,
        "links": {},
        "meta": {
            "pagination": {
                "currentPage": current,
                "perPage": len(records),
                "totalPages": total_pages,
                "totalEntries": 0,
            }
        },
    }


def facility(facility_id: str) -> dict[str, Any]:
    return {
        "id": facility_id,
        "type": "va_facilities",
        "attributes": {"name": facility_id},
    }
