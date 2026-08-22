"""Tests for the fixture recorder's retry helper.

The recorder makes ~16 sequential live calls, so a single transient upstream
5xx must not abort the run. These tests drive the retry against a
``MockTransport`` that fails a set number of times, because the real upstream
cannot be made to fail on demand.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncIterator, Callable
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
    """Returns ``statuses`` in order, then 200, counting every request."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        index = self.calls
        self.calls += 1
        status = self.statuses[index] if index < len(self.statuses) else 200
        body: Any = {"ok": True} if status == 200 else {"detail": f"boom {status}"}
        return httpx.Response(status, json=body)


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
        (CouncilApiError(404, "missing"), False),
        (CouncilApiError(422, "invalid"), False),
        (httpx.ConnectTimeout("slow"), True),
        (httpx.ReadTimeout("slow"), True),
        (ValueError("unrelated"), False),
    ],
)
def test_is_transient(exc: BaseException, expected: bool) -> None:
    assert recorder.is_transient(exc) is expected
