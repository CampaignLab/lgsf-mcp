"""Offline test harness: a CouncilGatewayClient backed by recorded fixtures.

The routes below mirror what ``scripts/record_fixtures.py`` recorded, keyed by
``(method, path, sorted query)`` so a test fails loudly if the client starts
sending different query parameters. Anything unmapped gets a 404, which keeps
accidental live calls impossible and typos obvious.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from lgsf_mcp.client import CouncilGatewayClient
from lgsf_mcp.server import COMMITTEE_PAGE_SIZE

FIXTURES = Path(__file__).parent / "fixtures"

BASE_PATH = "/council-api"

Query = tuple[tuple[str, str], ...]
Route = tuple[str, str, Query]


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


IDS: dict[str, Any] = load_fixture("ids")

COUNCIL_ID: int = IDS["council_id"]
MEETING_ID: int = IDS["meeting_id"]
DOCUMENT_ID: int = IDS["document_id"]
DECISION_ID: int = IDS["decision_id"]
PERSON_ID: int = IDS["person_lookup_id"]

#: The per_page the listing tools default to, which fixtures must serve.
TOOL_PER_PAGE = 25


def query(**params: object) -> Query:
    """Normalise query params the way httpx would encode them, then sort."""
    encoded = {
        key: ("true" if value is True else "false" if value is False else str(value))
        for key, value in params.items()
    }
    return tuple(sorted(encoded.items()))


def _routes() -> dict[Route, tuple[int, Any]]:
    """Map (method, path, sorted query) to (status, JSON body)."""
    paging = {"page": 1, "per_page": 2}
    # The tools default to per_page=25 and get_council fetches committees with
    # per_page=100, so those pages are served from the same fixture bodies as
    # the per_page=2 ones recorded by scripts/record_fixtures.py. The fixture
    # `per_page` field therefore does not match the request; tests must not
    # assert on it.
    tool_paging = {"page": 1, "per_page": TOOL_PER_PAGE}
    committee_paging = {"page": 1, "per_page": COMMITTEE_PAGE_SIZE}
    routes: dict[Route, tuple[int, Any]] = {
        ("GET", "/councils", query(**paging)): (200, load_fixture("councils")),
        ("GET", "/councils", query(**tool_paging)): (200, load_fixture("councils")),
        ("GET", f"/councils/{COUNCIL_ID}/committees", query(**committee_paging)): (
            200,
            load_fixture("council_226_committees"),
        ),
        ("GET", "/meetings", query(council_id=COUNCIL_ID, **tool_paging)): (
            200,
            load_fixture("meetings_council_226"),
        ),
        ("GET", f"/councils/{COUNCIL_ID}", ()): (200, load_fixture("council_226")),
        ("GET", f"/councils/{COUNCIL_ID}/committees", query(**paging)): (
            200,
            load_fixture("council_226_committees"),
        ),
        ("GET", "/meetings", query(council_id=COUNCIL_ID, **paging)): (
            200,
            load_fixture("meetings_council_226"),
        ),
        ("GET", f"/meetings/{MEETING_ID}", ()): (200, load_fixture("meeting")),
        ("GET", "/documents", query(meeting_id=MEETING_ID, **paging)): (
            200,
            load_fixture("documents_meeting"),
        ),
        ("GET", "/documents", query(meeting_id=MEETING_ID, **tool_paging)): (
            200,
            load_fixture("documents_meeting"),
        ),
        ("GET", f"/documents/{DOCUMENT_ID}", query(include_text=False)): (
            200,
            load_fixture("document"),
        ),
        ("GET", f"/documents/{DOCUMENT_ID}", query(include_text=True)): (
            200,
            load_fixture("document_text"),
        ),
        ("GET", f"/documents/{DOCUMENT_ID}/file", query(redirect=False)): (
            200,
            load_fixture("document_file"),
        ),
        ("GET", "/decisions", query(**paging)): (200, load_fixture("decisions")),
        ("GET", f"/decisions/{DECISION_ID}", ()): (200, load_fixture("decision")),
        ("GET", "/people", query(council_id=COUNCIL_ID, **paging)): (
            200,
            load_fixture("people_council_226"),
        ),
        ("GET", "/search", query(q="housing", mode="document", size=2, offset=0)): (
            200,
            load_fixture("search_housing"),
        ),
        ("GET", "/councils", query(page=1, per_page=1000)): (
            422,
            {"detail": load_fixture("validation_422")},
        ),
    }
    # /people is empty upstream, so the recorded person response is a 404 body.
    routes[("GET", f"/people/{PERSON_ID}", ())] = (
        404,
        {"detail": load_fixture("person")},
    )
    return routes


class FixtureTransport:
    """MockTransport handler that serves fixtures and records requests."""

    def __init__(self, routes: Mapping[Route, tuple[int, Any]]) -> None:
        self.routes = dict(routes)
        self.requests: list[httpx.Request] = []

    @property
    def last_request(self) -> httpx.Request:
        return self.requests[-1]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith(BASE_PATH):
            path = path[len(BASE_PATH) :]
        key: Route = (
            request.method,
            path,
            tuple(sorted(request.url.params.multi_items())),
        )
        status, body = self.routes.get(
            key, (404, {"detail": f"No fixture for {key!r}"})
        )
        return httpx.Response(status, json=body)


class FixtureClient(CouncilGatewayClient):
    """Client bound to its fixture handler, so tests can inspect requests."""

    def __init__(self, handler: FixtureTransport, token: str | None = None) -> None:
        super().__init__(token=token, transport=httpx.MockTransport(handler))
        self.handler = handler

    @property
    def requests(self) -> list[httpx.Request]:
        return self.handler.requests

    @property
    def last_request(self) -> httpx.Request:
        return self.handler.last_request


@pytest.fixture
def transport() -> FixtureTransport:
    return FixtureTransport(_routes())


@pytest.fixture
async def fixture_client(transport: FixtureTransport) -> AsyncIterator[FixtureClient]:
    """A client serving recorded fixtures, exposing the requests it made."""
    client = FixtureClient(transport)
    try:
        yield client
    finally:
        await client.aclose()
