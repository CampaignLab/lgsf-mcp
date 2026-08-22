"""Tests for the ChatGPT connector aliases, ``search`` and ``fetch``.

ChatGPT will only ever call tools with these exact names and shapes, so the
assertions here are deliberately about the *contract* — key sets, id parsing,
and the fact that every result arrives as both ``structuredContent`` and a
JSON ``content`` block — rather than about the data behind it.
"""

import json
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from lgsf_mcp.server import FETCH_TEXT_CHARS, create_server
from tests.conftest import DOCUMENT_ID, MEETING_ID, FixtureClient, load_fixture


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


async def test_chatgpt_aliases_are_listed_with_their_single_parameter(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    # ChatGPT matches on the exact names, and on exactly one string argument.
    for name, parameter in (("search", "query"), ("fetch", "id")):
        schema = tools[name].input_schema
        assert set(schema["properties"]) == {parameter}
        assert schema["properties"][parameter]["type"] == "string"
        assert schema.get("required", []) == [parameter]


@pytest.mark.parametrize(
    ("tool", "phrases"),
    [
        ("search", ["ChatGPT-compatible search", "prefer `search_council_data`"]),
        ("fetch", ["ChatGPT-compatible retrieval", "Document-<number>", "20000"]),
    ],
)
async def test_alias_descriptions_point_other_clients_at_the_richer_tools(
    fixture_client: FixtureClient, tool: str, phrases: list[str]
) -> None:
    async with Client(create_server(fixture_client)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    description = tools[tool].description
    assert description is not None
    # Docstrings are wrapped, so match against a single-spaced rendering.
    flattened = " ".join(description.split())
    missing = [phrase for phrase in phrases if phrase not in flattened]
    assert missing == []


# -- search ----------------------------------------------------------------


async def test_search_returns_only_the_three_openai_keys(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("search", {"query": "housing"})

    payload = _payload(result)
    assert set(payload) == {"results"}
    assert payload["results"]
    for item in payload["results"]:
        assert set(item) == {"id", "title", "url"}
        # Citations need a non-empty url; the fixture hits all have one.
        assert item["url"]


async def test_search_ids_and_titles_come_from_the_hits(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("search", {"query": "housing"})

    fixture = load_fixture("search_housing")
    results = _payload(result)["results"]
    assert [item["id"] for item in results] == [hit["id"] for hit in fixture["hits"]]
    # Document names are routinely null, so the meeting name is the label.
    assert results[0]["title"] == fixture["hits"][0]["source"]["meeting_name"]
    assert results[0]["url"] == fixture["hits"][0]["record"]["url"]


async def test_search_asks_upstream_for_twenty_unfiltered_document_hits(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        await client.call_tool("search", {"query": "housing"})

    assert dict(fixture_client.last_request.url.params) == {
        "q": "housing",
        "mode": "document",
        "size": "20",
        "offset": "0",
    }


async def test_search_rejects_a_one_character_query(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("search", {"query": "h"})

    assert result.is_error is True
    assert "at least 2 characters" in _text(result)


# -- fetch -----------------------------------------------------------------


async def test_fetch_document_returns_the_five_openai_keys(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("fetch", {"id": f"Document-{DOCUMENT_ID}"})

    payload = _payload(result)
    assert set(payload) == {"id", "title", "text", "url", "metadata"}
    fixture = load_fixture("document_text")
    assert payload["id"] == f"Document-{DOCUMENT_ID}"
    assert payload["text"] == fixture["text"]
    assert len(payload["text"]) <= FETCH_TEXT_CHARS
    assert payload["url"] == fixture["url"]
    assert payload["metadata"] == {
        "source_type": fixture["source_type"],
        "source_id": fixture["source_id"],
        "kind": fixture["kind"],
        "is_minutes": fixture["is_minutes"],
        "text_length": len(fixture["text"]),
        "has_more": False,
    }


async def test_fetch_document_titles_an_untitled_document_by_id(
    fixture_client: FixtureClient,
) -> None:
    # `/documents/{id}` omits `name` entirely, where listing records send null.
    assert "name" not in load_fixture("document_text")
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("fetch", {"id": f"Document-{DOCUMENT_ID}"})

    assert _payload(result)["title"] == f"Document {DOCUMENT_ID}"


async def test_fetch_document_truncates_long_text_and_flags_has_more(
    transport: Any, fixture_client: FixtureClient
) -> None:
    long_text = "x" * (FETCH_TEXT_CHARS + 500)
    route = ("GET", f"/documents/{DOCUMENT_ID}", (("include_text", "true"),))
    transport.routes[route] = (
        200,
        {**load_fixture("document_text"), "text": long_text},
    )

    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("fetch", {"id": f"Document-{DOCUMENT_ID}"})

    payload = _payload(result)
    assert len(payload["text"]) == FETCH_TEXT_CHARS
    assert payload["metadata"]["text_length"] == len(long_text)
    assert payload["metadata"]["has_more"] is True


async def test_fetch_meeting_renders_markdown(fixture_client: FixtureClient) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("fetch", {"id": f"Meeting-{MEETING_ID}"})

    payload = _payload(result)
    assert set(payload) == {"id", "title", "text", "url", "metadata"}
    fixture = load_fixture("meeting")
    assert payload["id"] == f"Meeting-{MEETING_ID}"
    assert payload["title"] == fixture["name"]
    assert payload["url"] == fixture["url"]
    assert payload["text"].startswith(f"# {fixture['name']}")
    assert f"**Date:** {fixture['date']}" in payload["text"]
    # Documents are listed by fetchable id so the model can follow them up.
    assert "## Documents" in payload["text"]
    for document in fixture["documents"]:
        assert f"- Document-{document['id']}: untitled" in payload["text"]
    assert payload["metadata"]["document_count"] == len(fixture["documents"])


async def test_fetch_meeting_omits_sections_that_are_null_upstream(
    fixture_client: FixtureClient,
) -> None:
    fixture = load_fixture("meeting")
    assert fixture["topline"] is None
    assert fixture["decisions"] == []
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("fetch", {"id": f"Meeting-{MEETING_ID}"})

    text = _payload(result)["text"]
    assert "## Topline" not in text
    assert "## Decisions" not in text


async def test_fetch_meeting_summaries_are_rendered_when_present(
    transport: Any, fixture_client: FixtureClient
) -> None:
    route = ("GET", f"/meetings/{MEETING_ID}", ())
    transport.routes[route] = (
        200,
        {
            **load_fixture("meeting"),
            "topline": "Rents rose.",
            "about": "A housing meeting.",
            "notes": "Some notes.",
            "agenda": "1. Apologies",
            "decisions": [{"id": 42, "topline": "Approved the budget."}],
        },
    )

    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("fetch", {"id": f"Meeting-{MEETING_ID}"})

    payload = _payload(result)
    text = payload["text"]
    for heading, body in (
        ("## Topline", "Rents rose."),
        ("## About", "A housing meeting."),
        ("## Notes", "Some notes."),
        ("## Agenda", "1. Apologies"),
    ):
        assert heading in text
        assert body in text
    assert "- Decision-42: Approved the budget." in text
    assert payload["metadata"]["decision_count"] == 1


async def test_fetch_maps_an_upstream_404(fixture_client: FixtureClient) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("fetch", {"id": "Document-999999"})

    assert result.is_error is True
    assert "Council Gateway API 404:" in _text(result)


@pytest.mark.parametrize(
    "bad_id",
    ["Person-1", "junk", "Document-", "Document-abc", "1547369", "document-1", ""],
)
async def test_fetch_rejects_unfetchable_ids(
    fixture_client: FixtureClient, bad_id: str
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("fetch", {"id": bad_id})

    assert result.is_error is True
    message = _text(result)
    assert "unknown id format" in message
    assert repr(bad_id) in message


# -- The OpenAI dual-encoding requirement ----------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("search", {"query": "housing"}),
        ("fetch", {"id": f"Document-{DOCUMENT_ID}"}),
        ("fetch", {"id": f"Meeting-{MEETING_ID}"}),
    ],
)
async def test_results_arrive_as_both_structured_and_json_content(
    fixture_client: FixtureClient, tool: str, arguments: dict[str, str]
) -> None:
    """OpenAI requires both halves; the SDK derives them from the annotation.

    ``FuncMetadata.convert_result`` JSON-dumps the returned dict into
    ``content`` *and* validates it into ``structured_content``, so this is a
    regression test on the ``-> dict[str, Any]`` annotation rather than on
    any code of ours. Note the text block is pretty-printed, so it is equal
    to the structured content only after parsing.
    """
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool(tool, arguments)

    assert result.is_error is not True
    assert result.structured_content is not None
    assert len(result.content) == 1
    assert json.loads(_text(result)) == result.structured_content
