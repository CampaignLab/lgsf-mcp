"""MCP server factory for the Council Gateway API.

## How tools reach the HTTP client (mcp 2.0 pattern — copy this)

The SDK injects a per-request context into any tool parameter annotated with
``mcp.server.mcpserver.Context``. The parameter name is free; only the
annotation matters. The object yielded by the server's lifespan is reachable
as ``ctx.request_context.lifespan_context``::

    from mcp.server.mcpserver import Context

    @server.tool()
    async def list_councils(ctx: Context[AppContext]) -> dict[str, Any]:
        "..."
        return await ctx.request_context.lifespan_context.client.list_councils()

Two traps, both verified against mcp 2.0.0's source rather than assumed:

* Import ``Context`` from ``mcp.server.mcpserver``. There is a *different*
  ``mcp.server.context.Context`` which exposes a ``.lifespan`` property, but
  its own docstring notes it is never constructed by the request runner.
* ``mcp.server.fastmcp`` is gone; ``FastMCP`` is now ``MCPServer``.

## Error mapping (mcp 2.0 pattern — copy this)

Tools map upstream failures onto ``ToolError``, imported from
``mcp.server.mcpserver.exceptions`` (it is *not* re-exported from
``mcp.server.mcpserver``). ``_api_error`` holds the shared wording::

    except CouncilApiError as e:
        raise ToolError(_api_error(e)) from e

The SDK wraps every exception a tool body raises as
``ToolError(f"Error executing tool {name}: {e}")`` and returns it as
``CallToolResult(is_error=True)`` with one ``TextContent``. Raising
``ToolError`` is therefore about intent, not about changing the wire format —
a plain ``ValueError`` would look identical to the client. Do *not* raise
``MCPError`` subclasses for ordinary tool failures: those bypass the wrapping
and become top-level JSON-RPC errors.

Reference: https://py.sdk.modelcontextprotocol.io/handlers/lifespan/
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from .client import CouncilApiError, CouncilGatewayClient
from .search import SearchBackend, SearchMode, TextSearchBackend

SERVER_NAME = "lgsf-mcp"

INSTRUCTIONS = (
    "UK local-government data scraped from council websites (currently "
    "ModernGov sites via Poteris's `council-scraper`, being superseded by "
    "Democracy Club's LGSF). **Councils** have **committees**. **Meetings** "
    "belong to a council and optionally a committee, have a date, and carry "
    "LLM-generated summaries (`topline`, `about`, `notes`). **Documents** "
    "(agendas, minutes, reports — usually PDFs) hang off either a meeting or "
    "a decision via `source_type` (`Meeting` | `Decision`) and `source_id`; "
    "documents with `is_minutes=true` carry extracted text. **Decisions** are "
    "officer/delegated decisions with `is_key` flags. **People** are "
    "councillors and officers with party and role. Full-text search covers "
    "**document text and document classifications only** — meetings, "
    "decisions and people are not searchable; reach them through filters or "
    "through the documents that mention them. Council-wide document listings "
    "are slow upstream (up to ~15s); prefer searching, or listing by "
    "`meeting_id` / `decision_id`."
)


#: Upstream 422s on a one-character query, so reject it before the round trip.
MIN_QUERY_LENGTH = 2

#: Committees are fetched eagerly by ``get_council``; 100 is the upstream cap.
COMMITTEE_PAGE_SIZE = 100


def _api_error(e: CouncilApiError) -> str:
    """The single wording every tool uses for an upstream failure."""
    return f"Council Gateway API {e.status}: {e.detail}"


def _parse_date(name: str, value: str | None) -> datetime.date | None:
    """Parse a model-supplied ``YYYY-MM-DD`` filter, or fail loudly.

    Left to the client this would become an opaque upstream 422, which tells
    the model nothing about which argument it got wrong.
    """
    if value is None:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as e:
        raise ToolError(
            f"{name} must be a date in YYYY-MM-DD format, got {value!r}"
        ) from e


@dataclass
class AppContext:
    """Lifespan state shared by every tool invocation."""

    client: CouncilGatewayClient
    search: SearchBackend


def create_server(client: CouncilGatewayClient | None = None) -> MCPServer[AppContext]:
    """Build the lgsf-mcp server.

    An injected ``client`` (tests) is used as-is and left open for the caller
    to close; otherwise the lifespan owns a ``from_env()`` client and closes it
    on shutdown.
    """

    @asynccontextmanager
    async def lifespan(_server: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
        if client is not None:
            yield _app_context(client)
            return
        owned = CouncilGatewayClient.from_env()
        try:
            yield _app_context(owned)
        finally:
            await owned.aclose()

    server = MCPServer(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )
    _register_tools(server)
    return server


def _app_context(client: CouncilGatewayClient) -> AppContext:
    return AppContext(client=client, search=TextSearchBackend(client))


def _register_tools(server: MCPServer[AppContext]) -> None:
    """Register every tool on ``server``."""

    @server.tool()
    async def search_council_data(
        ctx: Context[AppContext],
        q: str,
        council_id: int | None = None,
        mode: SearchMode = "document",
        size: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Full-text search across UK local-government council data. Start here
        for open-ended questions.

        This is a **phrase-style** search (matched words must appear near each
        other, in order, with a little slack), so short topical phrases such as
        `"council tax reduction"` work better than long natural-language
        questions or single very common words.

        **What is searchable:** the extracted text of documents (agendas,
        minutes, reports) and, in `classification` mode, the LLM-generated
        topic classifications attached to those documents. **Meetings,
        decisions and people are NOT indexed** — reach them via the documents
        that mention them, or via the filtered listing tools.

        Args:
            q: The phrase to search for. Must be at least 2 characters.
            council_id: Restrict to one council. Omit to search all councils.
            mode: `document` searches document text (the default and usually
                what you want); `classification` searches the LLM-generated
                classifications instead, which is useful for topic-level
                browsing.
            size: Hits per page (default 20).
            offset: Zero-based index of the first hit; page by adding `size`.

        Returns:
            `total` — the number of matching documents, **capped at 10000**;
            when `total_is_capped` is true treat `total` as "at least this
            many", not an exact count. `mode` echoes the mode used. `hits` is
            a list of matches, each with:

            - `document_id` — pass to `get_document(include_text=true)` to
              read the surrounding text of the match.
            - `meeting_id` / `meeting_name` — pass the id to `get_meeting`
              for the meeting the document belongs to.
            - `title` — the document name, falling back to the meeting name
              when the document is untitled (which is common).
            - `highlights` — matched snippets with the matched terms in
              `**bold**`. Read these first; they are usually enough to decide
              whether to fetch the full document.
            - `record` — the full upstream document record (url, kind,
              `is_minutes`, timestamps).
        """
        if len(q) < MIN_QUERY_LENGTH:
            raise ToolError(
                f"q must be at least {MIN_QUERY_LENGTH} characters, got {len(q)}"
            )
        backend = ctx.request_context.lifespan_context.search
        try:
            return await backend.search(
                q, council_id=council_id, mode=mode, size=size, offset=offset
            )
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e

    @server.tool()
    async def list_councils(
        ctx: Context[AppContext],
        council_type: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """List the UK councils covered by this dataset — the top of the data
        model that meetings, committees, decisions, documents and people all
        hang off.

        There are roughly 440 councils (the exact count for your filter is in
        the `total` field). **There is no name search**: to find a council by
        name, page through with `per_page=100` and match the `name` field
        client-side. Once you have the id, use `get_council` for its
        committees, or `list_meetings(council_id=...)` for its meetings.

        Args:
            council_type: Restrict to one scraper family. The values seen
                upstream are `modern_gov` and `cmis` — these are the values
                of each council's `council_type_label` field. Omit to list
                every council.
            page: One-based page number.
            per_page: Councils per page (default 25; upstream caps this at
                100, and 100 is what you want when scanning for a name).

        Returns:
            `items` — the councils on this page, each with `id`, `name`,
            `council_type_label`, `majority_party` and `base_scrape_url`;
            plus `page`, `per_page` and `total` (the number of councils
            matching the filter, across all pages).
        """
        client = ctx.request_context.lifespan_context.client
        try:
            return await client.list_councils(
                council_type=council_type, page=page, per_page=per_page
            )
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e

    @server.tool()
    async def get_council(ctx: Context[AppContext], council_id: int) -> dict[str, Any]:
        """Get one council together with its committees.

        A council's committees are the bodies that actually hold meetings,
        so they are fetched and merged in here: you never need a second call
        to list them. Pass a committee's `id` to
        `list_meetings(committee_id=...)` to see what that committee has met
        about.

        Args:
            council_id: The council's `id`, as returned by `list_councils`
                or found on a meeting, decision or person record.

        Returns:
            The council record (`id`, `name`, `council_type_label`,
            `majority_party`, `base_scrape_url`) plus two added fields:
            `committees`, a list of committee records (`id`, `council_id`,
            `name`, `url`), and `committees_total`, the total number of
            committees upstream reports for this council. Only the first 100
            committees are included; if `committees_total` exceeds
            `len(committees)` the rest are not shown here.
        """
        client = ctx.request_context.lifespan_context.client
        try:
            council = await client.get_council(council_id)
            committees = await client.list_council_committees(
                council_id, per_page=COMMITTEE_PAGE_SIZE
            )
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e
        return {
            **council,
            "committees": committees.get("items", []),
            "committees_total": committees.get("total", 0),
        }

    @server.tool()
    async def list_meetings(
        ctx: Context[AppContext],
        council_id: int | None = None,
        committee_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_minutes: bool | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """List council meetings, filtered by council, committee and/or date.

        Meetings belong to a council and usually to one of its committees,
        and are what agendas, minutes and other documents hang off. Use this
        when you know *which* body or *when* you care about; use
        `search_council_data` instead when you only know *what* you are
        looking for, since meetings themselves are not full-text indexed.

        Args:
            council_id: Restrict to one council (see `list_councils`).
            committee_id: Restrict to one committee (see `get_council`).
            date_from: Earliest meeting date, inclusive, as `YYYY-MM-DD`.
            date_to: Latest meeting date, inclusive, as `YYYY-MM-DD`.
            has_minutes: When true, return only meetings that have a minutes
                document attached — these are the meetings with extracted
                text you can actually read. When false, only meetings
                without one. Omit for both.
            page: One-based page number.
            per_page: Meetings per page (default 25; upstream caps at 100).

        Returns:
            `items` — meeting **summaries** only: `id`, `council_id`,
            `committee_id`, `name`, `url`, `date` and `topline` (a one-line
            LLM summary, often null). Call `get_meeting(id)` for the full
            record with documents, decisions, attendees and the longer
            summaries. Also `page`, `per_page` and `total`.
        """
        parsed_from = _parse_date("date_from", date_from)
        parsed_to = _parse_date("date_to", date_to)
        client = ctx.request_context.lifespan_context.client
        try:
            return await client.list_meetings(
                council_id=council_id,
                committee_id=committee_id,
                date_from=parsed_from,
                date_to=parsed_to,
                has_minutes=has_minutes,
                page=page,
                per_page=per_page,
            )
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e

    @server.tool()
    async def get_meeting(ctx: Context[AppContext], meeting_id: int) -> dict[str, Any]:
        """Get one meeting in full, with everything attached to it.

        This is the hub record: a meeting pulls together the committee that
        held it, the papers published for it, the decisions taken at it and
        who was there. Reach a meeting id from `list_meetings`, or from the
        `meeting_id` on a `search_council_data` hit.

        Args:
            meeting_id: The meeting's `id`.

        Returns:
            The meeting (`id`, `council_id`, `committee_id`, `name`, `url`,
            `date`) plus:

            - `topline`, `about`, `notes` — LLM-generated summaries of the
              meeting. Often null; when present, read these first.
            - `committee` — the full committee record, so no extra call.
            - `documents` — the agendas, reports and minutes published for
              the meeting. Each has `is_minutes`: pass the `id` of a document
              with `is_minutes=true` to `get_document(include_text=true)` to
              read what was actually said. `extract_status` tells you whether
              text extraction succeeded.
            - `decisions` — decisions recorded against this meeting.
            - `attendees` and `additional_attendees` — the councillors and
              officers present.
            - `tags` — topic tags for the meeting.
        """
        client = ctx.request_context.lifespan_context.client
        try:
            return await client.get_meeting(meeting_id)
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e
