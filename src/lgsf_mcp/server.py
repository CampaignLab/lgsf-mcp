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
``mcp.server.mcpserver``)::

    except CouncilApiError as e:
        raise ToolError(f"Council Gateway API {e.status}: {e.detail}") from e

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
            raise ToolError(f"Council Gateway API {e.status}: {e.detail}") from e
