"""Tests for the decision and people tools.

Driven through the real in-memory MCP client, like the other tool tests, so
the generated schemas and the error surface are what a model would see.

The people tools are exercised against the reality upstream records today:
``/people`` returns an empty page for every filter and ``/people/{id}`` 404s.
"""

from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from lgsf_mcp.server import create_server
from tests.conftest import (
    COUNCIL_ID,
    DECISION_ID,
    PERSON_ID,
    FixtureClient,
    load_fixture,
)

#: Every tool the server registers, so a forgotten registration fails loudly.
EXPECTED_TOOL_COUNT = 14


def _text(result: Any) -> str:
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


def _payload(result: Any) -> dict[str, Any]:
    assert result.is_error is not True
    payload = result.structured_content
    assert payload is not None
    assert isinstance(payload, dict)
    return payload


# -- Schemas and descriptions ----------------------------------------------


async def test_decision_and_people_tools_are_listed_with_their_parameters(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert len(tools) == EXPECTED_TOOL_COUNT

    expected: dict[str, tuple[set[str], list[str]]] = {
        "list_decisions": (
            {"council_id", "date_from", "date_to", "is_key", "page", "per_page"},
            [],
        ),
        "get_decision": ({"decision_id"}, ["decision_id"]),
        "list_people": (
            {"council_id", "is_councillor", "party", "page", "per_page"},
            [],
        ),
        "get_person": ({"person_id"}, ["person_id"]),
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
            "list_decisions",
            [
                "not full-text searchable",
                "decision_maker",
                "outcome",
                "is_key",
                "is_callable_in",
                "purpose",
                "content",
                "topline",
                "key decisions",
                "Decision",
                "source_id",
            ],
        ),
        (
            "get_decision",
            ["documents", "get_document", "decision_maker", "topline"],
        ),
        (
            "list_people",
            [
                "not populated yet",
                "upstream currently returns no people at all",
                'not** "this council has no councillors"',
                "not full-text searchable",
                "is_councillor",
                "party",
                "role",
                "CON",
                "NOC",
                "attendees",
            ],
        ),
        (
            "get_person",
            ["not populated yet", "Person not found", "attendees", "is_councillor"],
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


# -- list_decisions --------------------------------------------------------


async def test_list_decisions_returns_fixture_page(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("list_decisions", {})

    payload = _payload(result)
    fixture = load_fixture("decisions")
    assert [decision["id"] for decision in payload["items"]] == [
        decision["id"] for decision in fixture["items"]
    ]
    assert payload["total"] == fixture["total"]
    assert dict(fixture_client.last_request.url.params) == {
        "page": "1",
        "per_page": "25",
    }


async def test_list_decisions_passes_filters_upstream(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        # Unmapped route on purpose: this asserts on the outgoing query.
        await client.call_tool(
            "list_decisions",
            {
                "council_id": COUNCIL_ID,
                "date_from": "2025-01-01",
                "date_to": "2025-12-31",
                "is_key": True,
                "page": 2,
                "per_page": 50,
            },
        )

    assert dict(fixture_client.last_request.url.params) == {
        "council_id": str(COUNCIL_ID),
        "date_from": "2025-01-01",
        "date_to": "2025-12-31",
        "is_key": "true",
        "page": "2",
        "per_page": "50",
    }


@pytest.mark.parametrize("field", ["date_from", "date_to"])
async def test_list_decisions_rejects_bad_dates_without_calling_upstream(
    fixture_client: FixtureClient, field: str
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("list_decisions", {field: "2025-13-01"})

    assert result.is_error is True
    assert f"{field} must be a date in YYYY-MM-DD format" in _text(result)
    assert fixture_client.requests == []


# -- get_decision ----------------------------------------------------------


async def test_get_decision_returns_full_record_with_documents(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("get_decision", {"decision_id": DECISION_ID})

    payload = _payload(result)
    assert payload == load_fixture("decision")
    assert "documents" in payload


async def test_get_decision_maps_upstream_404(fixture_client: FixtureClient) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("get_decision", {"decision_id": 999999})

    assert result.is_error is True
    assert "Council Gateway API 404:" in _text(result)


# -- list_people -----------------------------------------------------------


async def test_list_people_returns_the_empty_upstream_page(
    fixture_client: FixtureClient,
) -> None:
    """Empty is the correct, non-error answer while the scrape is unpopulated."""
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("list_people", {"council_id": COUNCIL_ID})

    payload = _payload(result)
    assert payload["items"] == []
    assert payload["total"] == 0


async def test_list_people_without_filters_is_also_empty(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("list_people", {})

    assert _payload(result)["items"] == []
    assert dict(fixture_client.last_request.url.params) == {
        "page": "1",
        "per_page": "25",
    }


async def test_list_people_passes_filters_upstream(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        # Unmapped route on purpose: this asserts on the outgoing query.
        await client.call_tool(
            "list_people",
            {
                "council_id": COUNCIL_ID,
                "is_councillor": False,
                "party": "CON",
                "page": 3,
                "per_page": 100,
            },
        )

    assert dict(fixture_client.last_request.url.params) == {
        "council_id": str(COUNCIL_ID),
        "is_councillor": "false",
        "party": "CON",
        "page": "3",
        "per_page": "100",
    }


# -- get_person ------------------------------------------------------------


async def test_get_person_surfaces_the_upstream_404(
    fixture_client: FixtureClient,
) -> None:
    """No person exists upstream yet, so every lookup 404s; say so clearly."""
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("get_person", {"person_id": PERSON_ID})

    assert result.is_error is True
    message = _text(result)
    assert "Council Gateway API 404:" in message
    detail = load_fixture("person")
    assert isinstance(detail, str)
    assert detail in message
