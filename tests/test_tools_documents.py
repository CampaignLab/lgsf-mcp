"""Tests for the document tools, including windowed extracted text.

Driven through the real in-memory MCP client, like ``test_tools_councils``,
so the generated schemas and the error surface are what a model would see.
"""

from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from lgsf_mcp.server import MAX_TEXT_CHARS, create_server
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


#: The recorded ``include_text=true`` body, whose ``text`` is 3000 chars.
FULL_TEXT: str = load_fixture("document_text")["text"]


# -- Schemas and descriptions ----------------------------------------------


async def test_document_tools_are_listed_with_their_parameters(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    expected: dict[str, tuple[set[str], list[str]]] = {
        "list_documents": (
            {
                "meeting_id",
                "decision_id",
                "council_id",
                "is_minutes",
                "kind",
                "processing_status",
                "page",
                "per_page",
            },
            [],
        ),
        "get_document": (
            {"document_id", "include_text", "text_offset", "max_chars"},
            ["document_id"],
        ),
        "get_document_file_url": ({"document_id"}, ["document_id"]),
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
            "list_documents",
            [
                "slow",
                "up to ~15s",
                "Meeting",
                "Decision",
                "source_type",
                "is_minutes",
                "unclassified",
                "waiting",
                # ``is_minutes`` is unreliable, so the description must warn
                # against filtering on it rather than recommend it.
                "under-reports badly, so avoid filtering on",
                "silently drops real minutes",
                "not** evidence that none were published",
                "whose `extract_status` is `success`",
                'look for "minutes" in the `url` or `name`',
                'never read a false value as "not minutes"',
                # ``extract_status`` has three values, not two.
                "`success` (text is there to read), `pending`",
                "and `failed` (extraction went wrong",
            ],
        ),
        (
            "get_document",
            [
                "windows",
                "text_offset=next_offset",
                "has_more",
                "next_offset",
                "text_length",
                "clamped to 50000",
                "omits the `name` key",
                # Go by ``extract_status``, not the unreliable flag.
                "whose `extract_status` is `success`",
                "not `is_minutes`",
                "under-reports so badly",
                # ``failed`` is the third ``extract_status`` value.
                "`failed` (extraction went wrong",
            ],
        ),
        (
            "get_document_file_url",
            [
                "presigned",
                "600",
                "cannot fetch this URL yourself",
                "Document has no stored file",
                "extract_status",
            ],
        ),
    ],
)
async def test_document_descriptions_state_the_required_facts(
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


#: Wording from when the descriptions were written against fixtures, which
#: presented ``is_minutes`` / ``has_minutes`` as the reliable route to
#: readable minutes. Live data disproves it: document 1199223 is named
#: ``Printed+minutes+...pdf``, extracted successfully, and is
#: ``is_minutes: false``, as is every document on meeting 95384. A
#: description drifting back to any of these claims would send a model to
#: conclude minutes do not exist when they plainly do, so guard the *absence*
#: of the false claim — positive assertions alone would not catch the drift.
STALE_MINUTES_FLAG_PHRASES = [
    "documents with `is_minutes=true` carry extracted text",
    "**These are the documents that carry extracted text**",
    "these are the meetings with extracted text you can actually read",
    "pass the `id` of a document with `is_minutes=true`",
    "on a document with `is_minutes=true`",
    "only minutes documents.",
    "return only meetings that have a minutes document attached",
]

#: ``extract_status`` has three values; a description enumerating only two
#: would imply ``failed`` cannot happen.
STALE_EXTRACT_STATUS_PHRASES = [
    "(`success`, `pending`)",
    "`success` and `pending`",
    "either `success` or `pending`",
]


@pytest.mark.parametrize(
    "tool", ["list_documents", "get_document", "list_meetings", "get_meeting"]
)
async def test_descriptions_never_claim_the_minutes_flag_is_reliable(
    fixture_client: FixtureClient, tool: str
) -> None:
    async with Client(create_server(fixture_client)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    description = tools[tool].description
    assert description is not None
    flattened = " ".join(description.split())
    stale = [
        phrase
        for phrase in STALE_MINUTES_FLAG_PHRASES + STALE_EXTRACT_STATUS_PHRASES
        if phrase in flattened
    ]
    assert stale == []


async def test_server_instructions_do_not_claim_the_minutes_flag_is_reliable(
    fixture_client: FixtureClient,
) -> None:
    instructions = create_server(fixture_client).instructions
    assert instructions is not None
    flattened = " ".join(instructions.split())
    stale = [phrase for phrase in STALE_MINUTES_FLAG_PHRASES if phrase in flattened]
    assert stale == []
    assert "`is_minutes` under-reports badly" in flattened


# -- list_documents --------------------------------------------------------


async def test_list_documents_by_meeting_returns_fixture_page(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("list_documents", {"meeting_id": MEETING_ID})

    payload = _payload(result)
    fixture = load_fixture("documents_meeting")
    assert [document["id"] for document in payload["items"]] == [
        document["id"] for document in fixture["items"]
    ]
    assert payload["total"] == fixture["total"]


async def test_list_documents_passes_filters_upstream(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        # Unmapped route on purpose: this asserts on the outgoing query.
        await client.call_tool(
            "list_documents",
            {
                "council_id": 226,
                "decision_id": 4242,
                "is_minutes": True,
                "kind": "unclassified",
                "processing_status": "waiting",
                "page": 3,
                "per_page": 50,
            },
        )

    assert dict(fixture_client.last_request.url.params) == {
        "council_id": "226",
        "decision_id": "4242",
        "is_minutes": "true",
        "kind": "unclassified",
        "processing_status": "waiting",
        "page": "3",
        "per_page": "50",
    }


# -- get_document ----------------------------------------------------------


async def test_get_document_without_text_omits_the_text_key(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("get_document", {"document_id": DOCUMENT_ID})

    payload = _payload(result)
    assert "text" not in payload
    assert payload == load_fixture("document")
    assert dict(fixture_client.last_request.url.params) == {"include_text": "false"}


async def test_get_document_windows_the_text(fixture_client: FixtureClient) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool(
            "get_document",
            {"document_id": DOCUMENT_ID, "include_text": True, "max_chars": 100},
        )

    payload = _payload(result)
    assert payload["text"] == FULL_TEXT[:100]
    assert len(payload["text"]) == 100
    assert payload["text_length"] == len(FULL_TEXT)
    assert payload["text_offset"] == 0
    assert payload["has_more"] is True
    assert payload["next_offset"] == 100


async def test_get_document_continues_from_next_offset(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool(
            "get_document",
            {
                "document_id": DOCUMENT_ID,
                "include_text": True,
                "text_offset": 100,
                "max_chars": 100,
            },
        )

    payload = _payload(result)
    assert payload["text"] == FULL_TEXT[100:200]
    assert payload["text_offset"] == 100
    assert payload["has_more"] is True
    assert payload["next_offset"] == 200


async def test_get_document_clamps_max_chars(fixture_client: FixtureClient) -> None:
    assert len(FULL_TEXT) < MAX_TEXT_CHARS
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool(
            "get_document",
            {"document_id": DOCUMENT_ID, "include_text": True, "max_chars": 999999},
        )

    # Clamping is silent, and the whole (shorter than the clamp) text arrives.
    payload = _payload(result)
    assert payload["text"] == FULL_TEXT
    assert payload["has_more"] is False
    assert payload["next_offset"] is None


async def test_get_document_maps_upstream_404(fixture_client: FixtureClient) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool("get_document", {"document_id": 999999})

    assert result.is_error is True
    assert "Council Gateway API 404:" in _text(result)


# -- get_document_file_url -------------------------------------------------


async def test_get_document_file_url_returns_the_presigned_url(
    fixture_client: FixtureClient,
) -> None:
    async with Client(create_server(fixture_client)) as client:
        result = await client.call_tool(
            "get_document_file_url", {"document_id": DOCUMENT_ID}
        )

    payload = _payload(result)
    fixture = load_fixture("document_file")
    assert payload == fixture
    # The query string was redacted when the fixture was recorded.
    assert payload["url"].endswith("?REDACTED")
    assert payload["expires_in"] == 600
