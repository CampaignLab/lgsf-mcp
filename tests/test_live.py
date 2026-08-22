"""Opt-in smoke tests against the real Council Gateway API.

The rest of the suite runs against recorded fixtures, which prove the wiring
but say nothing about whether upstream still behaves. These tests are the
counterpart: they hit the live API through the same in-memory MCP client, so a
break in upstream's shape surfaces as a failing tool rather than a stale
fixture that still passes.

Run them with::

    LGSF_LIVE=1 uv run pytest tests/test_live.py

Two rules keep them honest against data that changes under us:

* **Chain ids, never hardcode them.** Every detail test takes its id from a
  listing made in the same run. The one exception is council 226 (Aberdeen
  City), which is a stable fixture of the dataset.
* **Assert on structure, not on values.** Counts drift and the scraped data is
  dirty (some people's ``name`` is a bare number), so the assertions check
  fields, types and relationships rather than particular strings.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from lgsf_mcp.server import create_server

#: Council-wide listings take up to ~15s upstream, and a chained test makes
#: several calls, so the per-test budget is deliberately generous.
LIVE_TIMEOUT_SECONDS = 120

#: Aberdeen City. Stable enough to hardcode; everything else is chained.
COUNCIL_ID = 226

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("LGSF_LIVE") != "1",
        reason="set LGSF_LIVE=1 to run live tests",
    ),
    pytest.mark.timeout(LIVE_TIMEOUT_SECONDS),
]


@asynccontextmanager
async def live_client() -> AsyncIterator[Client]:
    """An in-memory MCP client wired to a server that talks to the real API.

    ``create_server()`` with no argument makes the lifespan own a
    ``CouncilGatewayClient.from_env()``, so this is the same code path the
    installed server runs.

    This is deliberately *not* a pytest fixture. pytest-asyncio sets a
    fixture's teardown running in a different task from its setup, and the
    client's internal anyio task group refuses to be exited from another task
    ("Attempted to exit cancel scope in a different task than it was entered
    in"), which turns every test into a pass plus a teardown error. Entering
    the context inside the test body, as the fixture-backed tests do, keeps
    setup and teardown in one task.
    """
    async with Client(create_server()) as client:
        yield client


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    """Call ``tool`` and return its structured payload, failing loudly on error."""
    result = await client.call_tool(tool, arguments)
    if result.is_error:
        content = result.content[0]
        detail = content.text if isinstance(content, TextContent) else content
        pytest.fail(f"{tool}({arguments}) failed: {detail}")
    payload = result.structured_content
    assert isinstance(payload, dict), f"{tool} returned no structured content"
    return payload


def first_item(payload: dict[str, Any], tool: str) -> dict[str, Any]:
    """Return the first item of a listing, failing if the listing is empty.

    Empty listings are the failure mode worth shouting about: a test that
    silently passes on no data proves nothing about upstream.
    """
    items = payload.get("items")
    assert isinstance(items, list), f"{tool} returned no items list"
    assert items, f"{tool} returned an empty page (total={payload.get('total')})"
    item = items[0]
    assert isinstance(item, dict)
    return item


# -- Councils --------------------------------------------------------------


async def test_list_councils_covers_the_whole_dataset() -> None:
    async with live_client() as client:
        payload = await call(client, "list_councils", per_page=1)

        # Upstream reported 320 when this was written; the bound guards against a
        # truncated or half-loaded dataset without breaking as councils are added.
        assert payload["total"] > 300
        council = first_item(payload, "list_councils")
        assert isinstance(council["id"], int)
        assert council["name"]


async def test_get_council_merges_in_its_committees() -> None:
    async with live_client() as client:
        payload = await call(client, "get_council", council_id=COUNCIL_ID)

        assert payload["id"] == COUNCIL_ID
        assert payload["name"]
        # `committees` is added by the tool from a second upstream call; the bare
        # /councils/{id} response has no such key.
        committees = payload["committees"]
        assert isinstance(committees, list)
        assert committees, "Aberdeen City should have committees"
        assert payload["committees_total"] >= len(committees)
        assert all(committee["council_id"] == COUNCIL_ID for committee in committees)


# -- Meetings --------------------------------------------------------------


async def test_get_meeting_of_a_listed_meeting() -> None:
    async with live_client() as client:
        listing = await call(client, "list_meetings", council_id=COUNCIL_ID, per_page=1)
        listed = first_item(listing, "list_meetings")

        meeting = await call(client, "get_meeting", meeting_id=listed["id"])

        assert meeting["id"] == listed["id"]
        assert meeting["council_id"] == COUNCIL_ID
        assert meeting["date"]


# -- Documents -------------------------------------------------------------


async def documents_of_a_recent_meeting(
    client: Client,
) -> list[dict[str, Any]]:
    """Documents of the most recently listed meeting for our council."""
    listing = await call(client, "list_meetings", council_id=COUNCIL_ID, per_page=1)
    meeting = first_item(listing, "list_meetings")
    documents = await call(
        client, "list_documents", meeting_id=meeting["id"], per_page=25
    )
    items = documents["items"]
    assert isinstance(items, list)
    assert items, f"meeting {meeting['id']} has no documents"
    return [item for item in items if isinstance(item, dict)]


def extracted_document(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick a document whose text has actually been extracted.

    Documents are scraped before they are processed, so a meeting's newest
    papers routinely sit at ``extract_status: "pending"`` with no text and no
    stored file. Selecting on status is what keeps these tests from failing on
    upstream's backlog rather than on a real regression.
    """
    extracted = [
        document
        for document in documents
        if document.get("extract_status") == "success"
    ]
    if not extracted:
        pytest.skip("no document of this meeting has been text-extracted yet")
    return extracted[0]


async def test_list_documents_then_get_a_window_of_its_text() -> None:
    async with live_client() as client:
        documents = await documents_of_a_recent_meeting(client)
        listed = extracted_document(documents)

        document = await call(
            client,
            "get_document",
            document_id=listed["id"],
            include_text=True,
            max_chars=200,
        )

        assert document["id"] == listed["id"]
        text = document["text"]
        assert isinstance(text, str)
        assert text, "an extract_status=success document should have text"
        assert len(text) <= 200
        assert document["text_offset"] == 0
        assert document["text_length"] >= len(text)
        # The window is a prefix of a longer text exactly when more remains.
        assert document["has_more"] is (document["text_length"] > len(text))


async def test_get_document_file_url_returns_a_presigned_url() -> None:
    async with live_client() as client:
        documents = await documents_of_a_recent_meeting(client)
        listed = extracted_document(documents)

        payload = await call(client, "get_document_file_url", document_id=listed["id"])

        assert payload["url"].startswith("https://")
        assert payload["expires_in"] > 0


async def test_get_document_file_url_404s_for_an_unprocessed_document() -> None:
    """The documented 404 is a real upstream behaviour, so pin it down."""
    async with live_client() as client:
        documents = await documents_of_a_recent_meeting(client)
        pending = [
            document
            for document in documents
            if document.get("extract_status") == "pending"
        ]
        if not pending:
            pytest.skip("no unprocessed document available to check the 404 path")

        result = await client.call_tool(
            "get_document_file_url", {"document_id": pending[0]["id"]}
        )

        assert result.is_error is True
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert "Document has no stored file" in content.text


# -- Decisions -------------------------------------------------------------


async def test_get_decision_of_a_listed_decision() -> None:
    async with live_client() as client:
        listing = await call(client, "list_decisions", per_page=1)
        listed = first_item(listing, "list_decisions")

        decision = await call(client, "get_decision", decision_id=listed["id"])

        assert decision["id"] == listed["id"]
        assert decision["council_id"] == listed["council_id"]
        assert decision["decision_maker"]


# -- People ----------------------------------------------------------------


async def test_get_person_of_a_listed_person() -> None:
    """Upstream /people was empty when the fixtures were recorded and is now
    populated, so this must fail rather than pass quietly on an empty page."""
    async with live_client() as client:
        listing = await call(client, "list_people", council_id=COUNCIL_ID, per_page=1)
        assert listing["total"] > 0, "upstream /people is empty again"
        listed = first_item(listing, "list_people")

        person = await call(client, "get_person", person_id=listed["id"])

        assert person["id"] == listed["id"]
        assert person["council_id"] == COUNCIL_ID
        # Scraped names are dirty (some are bare numbers), so only check presence.
        assert person["name"] is not None


# -- Search ----------------------------------------------------------------


async def test_search_council_data_returns_hits_with_document_ids() -> None:
    async with live_client() as client:
        payload = await call(client, "search_council_data", q="housing", size=1)

        assert payload["mode"] == "document"
        assert payload["total"] >= 1
        hits = payload["hits"]
        assert len(hits) >= 1
        hit = hits[0]
        assert isinstance(hit["document_id"], int)
        assert hit["id"] == f"Document-{hit['document_id']}"


# -- ChatGPT connector tools -----------------------------------------------


async def test_chatgpt_search_then_fetch_round_trips() -> None:
    async with live_client() as client:
        found = await call(client, "search", query="housing")

        results = found["results"]
        assert results, "ChatGPT search returned no results for 'housing'"
        first = results[0]
        assert first["title"]

        fetched = await call(client, "fetch", id=first["id"])

        assert fetched["id"] == first["id"]
        assert isinstance(fetched["text"], str)
        assert isinstance(fetched["metadata"], dict)
