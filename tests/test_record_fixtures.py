"""Tests for the fixture recorder's retry helper.

The recorder makes ~16 sequential live calls, so a single transient upstream
5xx or 429 must not abort the run. These tests drive the retry against a
``MockTransport`` that fails a set number of times, because the real upstream
cannot be made to fail on demand. Nothing here really sleeps: ``no_sleep``
replaces ``asyncio.sleep`` and records the delays instead.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from collections.abc import AsyncIterator, Callable
from email.utils import format_datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from lgsf_mcp.client import CouncilApiError, CouncilGatewayClient


def _load_recorder() -> ModuleType:
    """Import ``scripts/record_fixtures.py``, which is not an installed package."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "record_fixtures.py"
    spec = importlib.util.spec_from_file_location("record_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


recorder = _load_recorder()


class CountingTransport:
    """Returns ``statuses`` in order, then 200, counting every request.

    ``headers`` are attached to every non-200 response, which is enough for
    the ``Retry-After`` cases since each of those fails identically.
    """

    def __init__(self, *statuses: int, headers: dict[str, str] | None = None) -> None:
        self.statuses = list(statuses)
        self.headers = headers or {}
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        index = self.calls
        self.calls += 1
        status = self.statuses[index] if index < len(self.statuses) else 200
        body: Any = {"ok": True} if status == 200 else {"detail": f"boom {status}"}
        headers = {} if status == 200 else self.headers
        return httpx.Response(status, json=body, headers=headers)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the backoff from actually sleeping, but record the delays."""
    monkeypatch.setattr(recorder.asyncio, "sleep", _record_sleep)
    SLEPT.clear()


SLEPT: list[float] = []


async def _record_sleep(delay: float) -> None:
    SLEPT.append(delay)


@pytest.fixture
async def client_for() -> AsyncIterator[
    Callable[[CountingTransport], CouncilGatewayClient]
]:
    """Build clients bound to a counting transport, closing them afterwards."""
    clients: list[CouncilGatewayClient] = []

    def build(transport: CountingTransport) -> CouncilGatewayClient:
        client = CouncilGatewayClient(transport=httpx.MockTransport(transport))
        clients.append(client)
        return client

    yield build
    for client in clients:
        await client.aclose()


# -- Transient failures are retried ---------------------------------------


async def test_500_is_retried_then_succeeds(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    transport = CountingTransport(500)
    client = client_for(transport)
    assert await recorder.retry(lambda: client.get_council(226)) == {"ok": True}
    assert transport.calls == 2
    assert SLEPT == [1.0]


async def test_backoff_is_exponential_and_bounded(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    """Three attempts total: 500, 500, then success after 1s and 2s waits."""
    transport = CountingTransport(500, 503)
    client = client_for(transport)
    assert await recorder.retry(lambda: client.get_council(226)) == {"ok": True}
    assert transport.calls == 3
    assert SLEPT == [1.0, 2.0]


async def test_gives_up_after_three_attempts(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    transport = CountingTransport(500, 500, 500)
    client = client_for(transport)
    with pytest.raises(CouncilApiError) as excinfo:
        await recorder.retry(lambda: client.get_council(226))
    assert excinfo.value.status == 500
    assert transport.calls == recorder.RETRY_ATTEMPTS == 3


async def test_transport_error_is_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("no route to host", request=request)
        return httpx.Response(200, json={"ok": True})

    client = CouncilGatewayClient(transport=httpx.MockTransport(handler))
    try:
        assert await recorder.retry(lambda: client.get_council(226)) == {"ok": True}
    finally:
        await client.aclose()
    assert calls == 2


# -- Client errors are not retried ----------------------------------------


@pytest.mark.parametrize("status", [404, 422])
async def test_4xx_is_not_retried(
    status: int,
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    """The recorder captures a 404 and a 422 as fixtures; retrying them would
    waste time and could change what gets recorded."""
    transport = CountingTransport(status)
    client = client_for(transport)
    with pytest.raises(CouncilApiError) as excinfo:
        await recorder.retry(lambda: client.get_council(226))
    assert excinfo.value.status == status
    assert transport.calls == 1
    assert SLEPT == []


async def test_error_detail_does_not_retry_expected_4xx(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    """error_detail goes through retry, so guard the 4xx path end to end."""
    transport = CountingTransport(422)
    client = client_for(transport)
    detail = await recorder.error_detail(lambda: client.get_council(226), expect=422)
    assert detail == "boom 422"
    assert transport.calls == 1


async def test_error_detail_retries_5xx_before_the_expected_error(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    """A blip on the way to a deliberately-failing call still yields the 404."""
    transport = CountingTransport(500, 404)
    client = client_for(transport)
    detail = await recorder.error_detail(lambda: client.get_person(1))
    assert detail == "boom 404"
    assert transport.calls == 2


# -- The retry predicate itself -------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (CouncilApiError(500, "boom"), True),
        (CouncilApiError(503, "boom"), True),
        (CouncilApiError(429, "slow down"), True),
        (CouncilApiError(404, "missing"), False),
        (CouncilApiError(422, "invalid"), False),
        (CouncilApiError(400, "bad"), False),
        (httpx.ConnectTimeout("slow"), True),
        (httpx.ReadTimeout("slow"), True),
        (ValueError("unrelated"), False),
    ],
)
def test_is_transient(exc: BaseException, expected: bool) -> None:
    assert recorder.is_transient(exc) is expected


# -- 429 and Retry-After ---------------------------------------------------


def _http_date(offset_seconds: float) -> str:
    """An HTTP-date ``offset_seconds`` from now, as a server would send it."""
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        seconds=offset_seconds
    )
    return format_datetime(when, usegmt=True)


async def test_429_is_retried_then_succeeds(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    """429 is transient now, where previously only 5xx was."""
    transport = CountingTransport(429)
    client = client_for(transport)
    assert await recorder.retry(lambda: client.get_council(226)) == {"ok": True}
    assert transport.calls == 2


async def test_retry_after_seconds_beats_exponential_backoff(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    """``Retry-After: 2`` on the first failure waits 2s, not the 1s backoff."""
    transport = CountingTransport(429, headers={"Retry-After": "2"})
    client = client_for(transport)
    assert await recorder.retry(lambda: client.get_council(226)) == {"ok": True}
    assert SLEPT == [2.0]


async def test_retry_after_is_honoured_on_every_attempt(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    """The header wins each time, so the waits do not grow exponentially."""
    transport = CountingTransport(429, 429, headers={"Retry-After": "3"})
    client = client_for(transport)
    assert await recorder.retry(lambda: client.get_council(226)) == {"ok": True}
    assert transport.calls == 3
    assert SLEPT == [3.0, 3.0]


async def test_absurd_retry_after_is_capped(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    """A day-long Retry-After must not hang the run."""
    transport = CountingTransport(429, headers={"Retry-After": "86400"})
    client = client_for(transport)
    assert await recorder.retry(lambda: client.get_council(226)) == {"ok": True}
    assert SLEPT == [recorder.MAX_RETRY_AFTER]
    assert recorder.MAX_RETRY_AFTER == 60.0


async def test_429_without_retry_after_uses_exponential_backoff(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    transport = CountingTransport(429, 429)
    client = client_for(transport)
    assert await recorder.retry(lambda: client.get_council(226)) == {"ok": True}
    assert SLEPT == [1.0, 2.0]


async def test_retry_after_http_date_is_parsed(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    """The HTTP-date form yields roughly the right delay, not the backoff."""
    transport = CountingTransport(429, headers={"Retry-After": _http_date(5)})
    client = client_for(transport)
    assert await recorder.retry(lambda: client.get_council(226)) == {"ok": True}
    assert SLEPT == pytest.approx([5.0], abs=2.0)


async def test_retry_after_http_date_in_the_past_waits_zero(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    """A stale date must not produce a negative wait."""
    transport = CountingTransport(429, headers={"Retry-After": _http_date(-3600)})
    client = client_for(transport)
    assert await recorder.retry(lambda: client.get_council(226)) == {"ok": True}
    assert SLEPT == [0.0]


async def test_unparseable_retry_after_falls_back_to_backoff(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    transport = CountingTransport(429, headers={"Retry-After": "soon"})
    client = client_for(transport)
    assert await recorder.retry(lambda: client.get_council(226)) == {"ok": True}
    assert SLEPT == [1.0]


async def test_retry_after_on_a_503_is_also_honoured(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    """Retry-After is not 429-specific; 503 may carry it too (RFC 9110)."""
    transport = CountingTransport(503, headers={"Retry-After": "7"})
    client = client_for(transport)
    assert await recorder.retry(lambda: client.get_council(226)) == {"ok": True}
    assert SLEPT == [7.0]


async def test_429_still_gives_up_after_three_attempts(
    client_for: Callable[[CountingTransport], CouncilGatewayClient],
) -> None:
    transport = CountingTransport(429, 429, 429, headers={"Retry-After": "1"})
    client = client_for(transport)
    with pytest.raises(CouncilApiError) as excinfo:
        await recorder.retry(lambda: client.get_council(226))
    assert excinfo.value.status == 429
    assert transport.calls == recorder.RETRY_ATTEMPTS == 3


def test_backoff_delay_ignores_retry_after_on_transport_errors() -> None:
    """Non-CouncilApiError failures have no headers, so backoff applies."""
    exc = httpx.ConnectError("no route to host")
    assert recorder.backoff_delay(exc, 1) == 1.0
    assert recorder.backoff_delay(exc, 2) == 2.0
