"""Tests for the council and meeting tools.

Driven through the real in-memory MCP client so the generated JSON schema and
the error surface are exactly what a model would see. The SDK reports every
tool failure as ``is_error=True`` with the text
``Error executing tool <name>: <message>``, so error assertions substring-match
that text instead of trying to distinguish exception types.
"""

import datetime
import re
from email.utils import format_datetime
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from lgsf_mcp.server import create_server
from tests.conftest import COUNCIL_ID, MEETING_ID, FixtureClient, load_fixture


def _text(result: Any) -> str:
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


# -- Schemas and descriptions ----------------------------------------------


async def test_tools_are_listed_with_their_parameters(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    expected: dict[str, tuple[set[str], list[str]]] = {
        "list_councils": ({"council_type", "page", "per_page"}, []),
        "get_council": ({"council_id"}, ["council_id"]),
        "list_meetings": (
            {
                "council_id",
                "committee_id",
                "date_from",
                "date_to",
                "has_minutes",
                "page",
                "per_page",
            },
            [],
        ),
        "get_meeting": ({"meeting_id"}, ["meeting_id"]),
    }
    for name, (properties, required) in expected.items():
        assert name in tools
        schema = tools[name].input_schema
        assert set(schema["properties"]) == properties
        assert schema.get("required", []) == required
        description = tools[name].description
        assert description is not None
        assert description.strip()


@pytest.mark.parametrize(
    ("tool", "phrases"),
    [
        (
            "list_councils",
            [
                "320",
                "modern_gov",
                "cmis",
                "council_type_label",
                "per_page=100",
                "items",
                "total",
            ],
        ),
        ("get_council", ["committees", "never need a second call"]),
        (
            "list_meetings",
            [
                "summaries",
                "topline",
                "get_meeting",
                "has_minutes",
                "minutes",
                # ``has_minutes`` is derived from the unreliable per-document
                # ``is_minutes`` flag, so the description must warn rather
                # than recommend it.
                "under-reports badly, so avoid it",
                "silently drops real minutes",
                "not** evidence that a council published none",
                'extract_status == "success"',
                "Omit for both, which is the safe default",
            ],
        ),
        (
            "get_meeting",
            [
                "decisions",
                "documents",
                "attendees",
                "tags",
                "committee",
                "topline",
                "about",
                "notes",
                "is_minutes",
                "get_document",
                # ``documents`` spans the committee's wider set, not just
                # this sitting's papers.
                "not reliably scoped to this one",
                "created_at",
                "wider document set",
                "even papers belonging to other committees",
                "10 Nov to 8 Dec 2025",
                # and the flag is not how to pick the readable ones.
                "extract_status` is `success`",
                "`is_minutes` flag to pick them",
                "under-reports badly",
            ],
        ),
    ],
)
async def test_descriptions_state_the_required_facts(
    fixture_client: FixtureClient, tool: str, phrases: list[str]
) -> None:
    async with Client(create_server(fixture_client)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    description = tools[tool].description
    assert description is not None
    # Docstrings are wrapped at 88 chars, so match a single-spaced rendering.
    flattened = " ".join(description.split())
    missing = [phrase for phrase in phrases if phrase not in flattened]
    assert missing == []


# -- Happy paths -----------------------------------------------------------


async def test_list_councils_returns_fixture_page(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("list_councils", {})

    assert result.is_error is not True
    payload = result.structured_content
    assert payload is not None
    assert [council["name"] for council in payload["items"]] == [
        "Aberdeen City",
        "Aberdeenshire",
    ]
    assert payload["total"] == load_fixture("councils")["total"]
    assert dict(fixture_client.last_request.url.params) == {
        "page": "1",
        "per_page": "25",
    }


async def test_list_councils_passes_council_type_upstream(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        # Unmapped in the fixture routes on purpose: the query is what matters.
        await client.call_tool("list_councils", {"council_type": "cmis"})

    assert dict(fixture_client.last_request.url.params) == {
        "council_type": "cmis",
        "page": "1",
        "per_page": "25",
    }


async def test_get_council_merges_committees(fixture_client: FixtureClient) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("get_council", {"council_id": COUNCIL_ID})

    assert result.is_error is not True
    payload = result.structured_content
    assert payload is not None
    committees = load_fixture("council_226_committees")
    assert payload["name"] == "Aberdeen City"
    assert payload["committees"] == committees["items"]
    assert payload["committees_total"] == committees["total"]
    # One call for the council, one for its committees — nothing else.
    assert [request.url.path for request in fixture_client.requests] == [
        f"/council-api/councils/{COUNCIL_ID}",
        f"/council-api/councils/{COUNCIL_ID}/committees",
    ]


async def test_list_meetings_returns_fixture_page(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("list_meetings", {"council_id": COUNCIL_ID})

    assert result.is_error is not True
    payload = result.structured_content
    assert payload is not None
    assert [meeting["id"] for meeting in payload["items"]] == [130462, 116785]
    assert payload["total"] == load_fixture("meetings_council_226")["total"]


async def test_list_meetings_passes_filters_upstream(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        # Unmapped route: this asserts on the outgoing query, not the body.
        await client.call_tool(
            "list_meetings",
            {
                "council_id": COUNCIL_ID,
                "committee_id": 3146,
                "date_from": "2025-01-01",
                "date_to": "2025-12-31",
                "has_minutes": True,
                "page": 2,
                "per_page": 50,
            },
        )

    assert dict(fixture_client.last_request.url.params) == {
        "council_id": str(COUNCIL_ID),
        "committee_id": "3146",
        "date_from": "2025-01-01",
        "date_to": "2025-12-31",
        "has_minutes": "true",
        "page": "2",
        "per_page": "50",
    }


async def test_get_meeting_returns_full_record(fixture_client: FixtureClient) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("get_meeting", {"meeting_id": MEETING_ID})

    assert result.is_error is not True
    payload = result.structured_content
    assert payload is not None
    assert payload == load_fixture("meeting")


# -- Errors ----------------------------------------------------------------


@pytest.mark.parametrize("field", ["date_from", "date_to"])
async def test_list_meetings_rejects_bad_dates_without_calling_upstream(
    fixture_client: FixtureClient, field: str
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("list_meetings", {field: "2025-13-01"})

    assert result.is_error is True
    assert f"{field} must be a date in YYYY-MM-DD format" in _text(result)
    assert fixture_client.requests == []


async def test_upstream_422_surfaces_as_a_tool_error(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("list_councils", {"per_page": 1000})

    assert result.is_error is True
    message = _text(result)
    assert "Council Gateway API 422:" in message
    assert "less than or equal to 100" in message


async def test_get_council_maps_upstream_404(fixture_client: FixtureClient) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("get_council", {"council_id": 999999})

    assert result.is_error is True
    assert "Council Gateway API 404:" in _text(result)


# -- Upstream rate limiting (429) ------------------------------------------

#: The route `list_councils` hits with its default arguments, overridden below
#: to synthesise the 429 an unthrottled upstream never sends today.
_DEFAULT_COUNCILS_ROUTE = ("GET", "/councils", (("page", "1"), ("per_page", "25")))


async def test_upstream_429_tells_the_model_to_wait_the_stated_time(
    transport: Any, fixture_client: FixtureClient
) -> None:
    transport.routes[_DEFAULT_COUNCILS_ROUTE] = (
        429,
        {"detail": "Rate limit exceeded"},
        {"Retry-After": "30"},
    )

    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("list_councils", {})

    assert result.is_error is True
    message = _text(result)
    assert "Council Gateway API 429: Rate limit exceeded" in message
    assert "upstream rate limit, not a bug or a bad request" in message
    assert "retry the same call with the same arguments after waiting" in message
    assert "30 seconds" in message


async def test_upstream_429_without_retry_after_still_reads_as_a_rate_limit(
    transport: Any, fixture_client: FixtureClient
) -> None:
    transport.routes[_DEFAULT_COUNCILS_ROUTE] = (429, {"detail": "Slow down"})

    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("list_councils", {})

    assert result.is_error is True
    message = _text(result)
    assert "Council Gateway API 429: Slow down" in message
    assert "upstream rate limit, not a bug or a bad request" in message
    assert "No Retry-After was given" in message
    assert "retry the same call with the same arguments" in message
    # No duration is invented when upstream did not name one.
    assert "seconds" in message
    assert "-second wait" not in message


async def test_upstream_429_is_not_retried_inside_the_tool(
    transport: Any, fixture_client: FixtureClient
) -> None:
    transport.routes[_DEFAULT_COUNCILS_ROUTE] = (
        429,
        {"detail": "Rate limit exceeded"},
        {"Retry-After": "1"},
    )

    async with Client(create_server(fixture_client)) as client:
        await client.call_tool("list_councils", {})

    assert len(fixture_client.requests) == 1


async def test_an_http_date_retry_after_is_reported_in_seconds(
    transport: Any, fixture_client: FixtureClient
) -> None:
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=45)
    transport.routes[_DEFAULT_COUNCILS_ROUTE] = (
        429,
        {"detail": "Rate limit exceeded"},
        {"Retry-After": format_datetime(when, usegmt=True)},
    )

    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("list_councils", {})

    message = _text(result)
    assert "upstream rate limit" in message
    # Rounded by the header's one-second resolution, so allow 44 as well as 45.
    assert re.search(r"a 4[45](\.\d+)?-second wait", message), message
