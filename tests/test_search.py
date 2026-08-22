"""Tests for lgsf_mcp.search and the search_council_data tool.

The tool tests drive the real in-memory MCP client so the JSON schema and the
error surface are exercised exactly as a model would see them. Note that the
SDK reports *every* tool failure as ``is_error=True`` with the text
``Error executing tool <name>: <message>``, so assertions here substring-match
that text rather than trying to distinguish exception types.
"""

from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from lgsf_mcp.search import TOTAL_CAP, TextSearchBackend, normalise_response
from lgsf_mcp.server import create_server
from tests.conftest import FixtureClient, load_fixture

TOOL = "search_council_data"


def _text(result: Any) -> str:
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


# -- Backend normalisation -------------------------------------------------


async def test_backend_normalises_recorded_response(
    fixture_client: FixtureClient,
) -> None:
    result = await TextSearchBackend(fixture_client).search("housing", size=2)

    assert result["mode"] == "document"
    assert result["total"] == TOTAL_CAP
    assert result["total_is_capped"] is True

    first, second = result["hits"]
    assert [hit["id"] for hit in result["hits"]] == [
        "Document-1432934",
        "Document-1432930",
    ]
    assert first["document_id"] == 1432934
    assert first["meeting_id"] == 120138
    assert first["score"] == pytest.approx(5.108002)
    assert first["record"] == load_fixture("search_housing")["hits"][0]["record"]
    assert second["document_id"] == 1432930


async def test_backend_converts_highlights_to_markdown(
    fixture_client: FixtureClient,
) -> None:
    result = await TextSearchBackend(fixture_client).search("housing", size=2)
    highlights = result["hits"][0]["highlights"]

    # One fragment from `meeting_name` plus one from `text`, flattened.
    assert len(highlights) == 2
    assert all("<strong>" not in fragment for fragment in highlights)
    assert all("</strong>" not in fragment for fragment in highlights)
    assert highlights[0].startswith("**Housing** Overview and Scrutiny Committee")
    assert "**Housing** Review Board" in highlights[1]


async def test_backend_title_falls_back_to_meeting_name(
    fixture_client: FixtureClient,
) -> None:
    raw = load_fixture("search_housing")
    assert raw["hits"][0]["source"]["name"] is None  # guards the fallback under test

    result = await TextSearchBackend(fixture_client).search("housing", size=2)
    hit = result["hits"][0]
    assert hit["meeting_name"].startswith("Housing Overview and Scrutiny Committee")
    assert hit["title"] == hit["meeting_name"]


def test_normalise_prefers_source_name_over_meeting_name() -> None:
    payload = {
        "total": 1,
        "mode": "document",
        "hits": [
            {
                "id": "Document-1",
                "score": 1.0,
                "source": {
                    "id": 1,
                    "name": "Printed minutes",
                    "type": "Document",
                    "meeting_id": 9,
                    "meeting_name": "Cabinet",
                },
                "highlight": {},
                "record": {"id": 1},
            }
        ],
    }
    hit = normalise_response(payload, "document")["hits"][0]
    assert hit["title"] == "Printed minutes"
    assert hit["meeting_name"] == "Cabinet"
    assert hit["highlights"] == []


def test_normalise_flags_uncapped_total() -> None:
    payload = {"total": 7, "mode": "document", "hits": []}
    result = normalise_response(payload, "document")
    assert result == {
        "total": 7,
        "total_is_capped": False,
        "mode": "document",
        "hits": [],
    }


def test_normalise_ignores_document_id_for_non_document_hits() -> None:
    payload = {
        "total": 1,
        "mode": "classification",
        "hits": [
            {
                "id": "Classification-5",
                "score": 2.0,
                "source": {"id": 5, "name": "Housing", "type": "Classification"},
                "record": {"id": 5},
            }
        ],
    }
    hit = normalise_response(payload, "classification")["hits"][0]
    assert hit["document_id"] is None
    assert hit["meeting_id"] is None
    assert hit["title"] == "Housing"


# -- Tool ------------------------------------------------------------------


async def test_tool_is_listed_with_its_schema(fixture_client: FixtureClient) -> None:
    async with Client(create_server(fixture_client)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert TOOL in tools
    schema = tools[TOOL].input_schema
    properties = schema["properties"]
    assert set(properties) == {"q", "council_id", "mode", "size", "offset"}
    assert schema["required"] == ["q"]
    assert properties["mode"]["enum"] == ["document", "classification"]
    assert properties["mode"]["default"] == "document"
    description = tools[TOOL].description
    assert description is not None
    assert "not indexed" in description.lower()


async def test_tool_returns_normalised_hits(fixture_client: FixtureClient) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool(TOOL, {"q": "housing", "size": 2})

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["hits"][0]["document_id"] == 1432934
    assert result.structured_content["total_is_capped"] is True


@pytest.mark.parametrize("q", ["", "h"])
async def test_tool_rejects_short_query_without_calling_upstream(
    fixture_client: FixtureClient, q: str
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool(TOOL, {"q": q})

    assert result.is_error is True
    assert "at least 2 characters" in _text(result)
    assert fixture_client.requests == []


async def test_tool_passes_parameters_upstream(fixture_client: FixtureClient) -> None:
    async with Client(create_server(fixture_client)) as client:
        # Deliberately unmapped in the fixture routes: only the query matters,
        # and the resulting 404 also exercises the CouncilApiError mapping.
        result = await client.call_tool(
            TOOL,
            {
                "q": "housing",
                "council_id": 226,
                "mode": "classification",
                "size": 5,
                "offset": 40,
            },
        )

    assert dict(fixture_client.last_request.url.params) == {
        "q": "housing",
        "council_id": "226",
        "mode": "classification",
        "size": "5",
        "offset": "40",
    }
    assert result.is_error is True
    assert "Council Gateway API 404:" in _text(result)
