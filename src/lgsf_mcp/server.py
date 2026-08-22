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

Reference: https://py.sdk.modelcontextprotocol.io/handlers/lifespan/
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server import MCPServer

from .client import CouncilGatewayClient

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


@dataclass
class AppContext:
    """Lifespan state shared by every tool invocation."""

    client: CouncilGatewayClient


def create_server(client: CouncilGatewayClient | None = None) -> MCPServer[AppContext]:
    """Build the lgsf-mcp server.

    An injected ``client`` (tests) is used as-is and left open for the caller
    to close; otherwise the lifespan owns a ``from_env()`` client and closes it
    on shutdown.
    """

    @asynccontextmanager
    async def lifespan(_server: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
        if client is not None:
            yield AppContext(client=client)
            return
        owned = CouncilGatewayClient.from_env()
        try:
            yield AppContext(client=owned)
        finally:
            await owned.aclose()

    return MCPServer(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )
