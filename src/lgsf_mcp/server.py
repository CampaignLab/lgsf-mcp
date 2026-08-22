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

## ChatGPT's `structuredContent` + `content` requirement

OpenAI's connector spec wants `search`/`fetch` results in *both*
``structuredContent`` and a JSON-encoded ``content`` text block. The SDK
already does this for any tool annotated to return a structured type such as
``dict[str, Any]``: ``FuncMetadata.convert_result`` builds the ``content``
blocks with ``_convert_to_content`` (which JSON-dumps a dict via
``pydantic_core.to_json``) *and* the ``structured_content`` from the derived
output model. So no serialisation code is needed here — only the annotation.
Dropping the return annotation, or widening it to ``Any``, would silently
lose ``structuredContent``.

Reference: https://py.sdk.modelcontextprotocol.io/handlers/lifespan/
"""

from __future__ import annotations

import datetime
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from .auth import StaticTokenVerifier, auth_settings
from .client import CouncilApiError, CouncilGatewayClient
from .search import SearchBackend, SearchMode, TextSearchBackend
from .text import window

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

#: Hard ceiling on one ``get_document`` text window, to bound the response.
MAX_TEXT_CHARS = 50_000

#: ``fetch`` cannot page, so it returns one fixed window of a document's text.
FETCH_TEXT_CHARS = 20_000

#: Hits ChatGPT's ``search`` asks for in one go; it has no paging either.
FETCH_SEARCH_SIZE = 20

#: Bind address and port the CLI serves ``--transport streamable-http`` on.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

#: Origin advertised in the OAuth resource metadata when the operator does not
#: override it. Correct for a local server; behind a Tailscale Funnel the
#: ``https://….ts.net`` URL must be passed explicitly, or clients are told to
#: talk to a loopback address they cannot reach.
DEFAULT_PUBLIC_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

#: The ``<Type>-<int>`` ids that upstream search emits and ``fetch`` accepts.
_FETCH_ID = re.compile(r"\A(?P<kind>[A-Za-z]+)-(?P<id>\d+)\Z")


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


#: The ``fetch`` id prefixes that resolve to something fetchable.
FETCHABLE_KINDS = ("Document", "Meeting")


def _parse_fetch_id(value: str) -> tuple[str, int]:
    """Split a ``fetch`` id such as ``Document-1432934`` into kind and id."""
    match = _FETCH_ID.match(value)
    if match is None or match["kind"] not in FETCHABLE_KINDS:
        raise ToolError(
            f"unknown id format: {value!r}. Expected "
            f"{' or '.join(f'{kind}-<number>' for kind in FETCHABLE_KINDS)}, "
            f"for example Document-1432934."
        )
    return match["kind"], int(match["id"])


def _meeting_markdown(meeting: dict[str, Any]) -> str:
    """Render a meeting as Markdown, since ``fetch`` returns prose, not JSON.

    Everything here is routinely null or empty upstream, so each section is
    emitted only when it has content; a meeting with no summaries and no
    papers legitimately renders as just its heading.
    """
    name = meeting.get("name") or f"Meeting {meeting.get('id')}"
    date = meeting.get("date")
    lines = [f"# {name}", ""]
    if date:
        lines += [f"**Date:** {date}", ""]
    for label, key in (("Topline", "topline"), ("About", "about"), ("Notes", "notes")):
        if meeting.get(key):
            lines += [f"## {label}", "", str(meeting[key]), ""]
    if meeting.get("agenda"):
        lines += ["## Agenda", "", str(meeting["agenda"]), ""]
    decisions = meeting.get("decisions") or []
    if decisions:
        lines += ["## Decisions", ""]
        lines += [
            f"- Decision-{decision.get('id')}: "
            f"{decision.get('topline') or decision.get('outcome') or 'no summary'}"
            for decision in decisions
        ]
        lines += [""]
    documents = meeting.get("documents") or []
    if documents:
        lines += ["## Documents", ""]
        lines += [
            f"- Document-{document.get('id')}: {document.get('name') or 'untitled'}"
            for document in documents
        ]
        lines += [""]
    return "\n".join(lines).rstrip() + "\n"


@dataclass
class AppContext:
    """Lifespan state shared by every tool invocation."""

    client: CouncilGatewayClient
    search: SearchBackend


def create_server(
    client: CouncilGatewayClient | None = None,
    *,
    token: str | None = None,
    public_url: str | None = None,
) -> MCPServer[AppContext]:
    """Build the lgsf-mcp server.

    An injected ``client`` (tests) is used as-is and left open for the caller
    to close; otherwise the lifespan owns a ``from_env()`` client and closes it
    on shutdown.

    Passing ``token`` requires every HTTP request to carry
    ``Authorization: Bearer <token>``; ``public_url`` is the externally
    reachable origin advertised in the resource metadata and defaults to
    ``http://127.0.0.1:8000``. Without ``token`` the server is unauthenticated.
    See :mod:`lgsf_mcp.auth` — notably, this has no effect on stdio.
    """
    auth_kwargs: dict[str, Any] = {}
    if token is not None:
        auth_kwargs = {
            "token_verifier": StaticTokenVerifier(token),
            "auth": auth_settings(public_url or DEFAULT_PUBLIC_URL),
        }

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
        **auth_kwargs,
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

        Upstream currently holds about 320 councils (the exact count for your
        filter is in the `total` field; not every UK council is covered
        yet). **There is no name search**: to find a council by
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

    @server.tool()
    async def list_documents(
        ctx: Context[AppContext],
        meeting_id: int | None = None,
        decision_id: int | None = None,
        council_id: int | None = None,
        is_minutes: bool | None = None,
        kind: str | None = None,
        processing_status: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """List the papers — agendas, reports, minutes, usually PDFs — published
        for meetings and decisions.

        Documents are polymorphic: each hangs off *either* a meeting or a
        decision, identified by `source_type` (`Meeting` | `Decision`) and
        `source_id`. Filter by the parent you care about.

        **Performance warning: filtering by `council_id` alone is slow
        upstream (up to ~15s)** because a council has tens of thousands of
        documents. Prefer `meeting_id` or `decision_id`, or use
        `search_council_data` when you only know the topic. `get_meeting`
        already returns a meeting's documents, so you rarely need
        `meeting_id` here at all.

        Args:
            meeting_id: Only documents published for this meeting.
            decision_id: Only documents attached to this decision.
            council_id: Only documents belonging to this council. Slow on its
                own — combine with another filter or avoid.
            is_minutes: When true, only minutes documents. **These are the
                documents that carry extracted text** you can read with
                `get_document(include_text=true)`; agendas and reports
                usually do not.
            kind: Free-text upstream classification of the document. Values
                are not enumerated by the API; the value seen most often is
                `unclassified`.
            processing_status: Free-text upstream pipeline state, not
                enumerated by the API; `waiting` is what most records show.
                Note this is *not* the same field as `extract_status` on the
                returned records, which is the one that tells you whether
                text extraction succeeded (`success`, `pending`).
            page: One-based page number.
            per_page: Documents per page (default 25; upstream caps at 100).

        Returns:
            `items` — the documents on this page, each with `id`,
            `source_type`, `source_id`, `name` (often null), `url` (the
            council's own link, which may be behind a redirect — use
            `get_document_file_url` for a direct download), `kind`,
            `extract_status`, `processing_status`, `is_minutes`, `is_media`,
            the `contains_agenda` / `contains_attendees` /
            `contains_decisions` flags and `created_at`. Plus `page`,
            `per_page` and `total`.
        """
        client = ctx.request_context.lifespan_context.client
        try:
            return await client.list_documents(
                meeting_id=meeting_id,
                decision_id=decision_id,
                council_id=council_id,
                is_minutes=is_minutes,
                kind=kind,
                processing_status=processing_status,
                page=page,
                per_page=per_page,
            )
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e

    @server.tool()
    async def get_document(
        ctx: Context[AppContext],
        document_id: int,
        include_text: bool = False,
        text_offset: int = 0,
        max_chars: int = 20000,
    ) -> dict[str, Any]:
        """Get one document, optionally with a window of its extracted text.

        This is how you read what a council actually said: set
        `include_text=true` on a document with `is_minutes=true` (or any
        document whose `extract_status` is `success`) to get the text
        extracted from the PDF.

        **Text is returned in windows, not all at once.** Extracted minutes
        can run to hundreds of thousands of characters, so a single call
        returns at most `max_chars` characters starting at `text_offset`.
        The response tells you where you are: `text_length` is the size of
        the whole text, `has_more` says whether anything follows this window,
        and `next_offset` is where the next window starts. **While `has_more`
        is true, call this tool again with `text_offset=next_offset`** to
        read on. Check `text_length` first: if it is only a little more than
        what you received, one more call finishes the document; if it is
        enormous, consider using `search_council_data` to find the relevant
        part instead of reading the whole thing.

        Args:
            document_id: The document's `id`, from `list_documents`, from a
                meeting's `documents`, or from a `search_council_data` hit's
                `document_id`.
            include_text: When false (the default) the `text` field is
                omitted entirely and you get metadata only, which is cheap.
                Set it to true only when you intend to read the text.
            text_offset: Zero-based character offset to start the window at.
                Pass the `next_offset` from the previous call.
            max_chars: Maximum characters in this window (default 20000).
                Values above 50000 are silently clamped to 50000.

        Returns:
            The document record (`id`, `source_type`, `source_id`, `url`,
            `kind`, `extract_status`, `processing_status`, `is_minutes`,
            `is_media`, the `contains_*` flags, `created_at`). When
            `include_text` is true it also has `text` (this window only),
            `text_length`, `text_offset`, `has_more` and `next_offset`
            (null once you have reached the end). When `include_text` is
            false there is no `text` key at all. An empty `text` with
            `text_length` 0 means extraction has not produced any text —
            check `extract_status`. Note this record **omits the `name` key
            entirely**, whereas `list_documents` items and search hits carry
            `name` (usually null); do not treat its absence here as a
            difference in the document.
        """
        client = ctx.request_context.lifespan_context.client
        try:
            document = await client.get_document(document_id, include_text)
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e
        if not include_text:
            document.pop("text", None)
            return document
        chunk = window(
            document.get("text"), text_offset, min(max_chars, MAX_TEXT_CHARS)
        )
        return {
            **document,
            "text": chunk.text,
            "text_length": chunk.text_length,
            "text_offset": chunk.offset,
            "has_more": chunk.has_more,
            "next_offset": chunk.next_offset,
        }

    @server.tool()
    async def get_document_file_url(
        ctx: Context[AppContext], document_id: int
    ) -> dict[str, Any]:
        """Get a direct download URL for a document's original PDF.

        **You cannot fetch this URL yourself** — return it to the user so
        they can open it. It is a **short-lived presigned URL** (see
        `expires_in`, typically 600 seconds), so present it immediately
        rather than saving it for later; a stale link will simply fail.

        Prefer `get_document(include_text=true)` when you want to *read* the
        contents. Use this tool when the user wants the source PDF itself.

        Not every document has a stored file: this legitimately **404s with
        "Document has no stored file"** when the scraper has not yet
        downloaded the PDF. Check the document's `extract_status` first — a
        `pending` document has nothing to download yet, whereas `success`
        means the file is there.

        Args:
            document_id: The document's `id`.

        Returns:
            `url` — the presigned download link; `filename`; `content_type`
            (usually `application/pdf`); `byte_size`; and `expires_in`, the
            lifetime of the link in seconds.
        """
        client = ctx.request_context.lifespan_context.client
        try:
            return await client.get_document_file(document_id)
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e

    @server.tool()
    async def list_decisions(
        ctx: Context[AppContext],
        council_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        is_key: bool | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """List officer and delegated decisions taken outside of meetings.

        A decision is a single act by a named decision maker — a director,
        a cabinet member, an officer under delegated powers — recorded with
        what was decided and why. Decisions taken *at* a meeting also appear
        in that meeting's `decisions` list (see `get_meeting`).

        **Decisions are not full-text searchable.** `search_council_data`
        indexes document text and document classifications only, so the only
        ways to reach a decision are the filters below, the `decisions` on a
        meeting, or a document whose `source_type` is `Decision` (its
        `source_id` is the decision's `id`).

        Args:
            council_id: Restrict to one council (see `list_councils`).
            date_from: Earliest decision date, inclusive, as `YYYY-MM-DD`.
            date_to: Latest decision date, inclusive, as `YYYY-MM-DD`.
                Note that upstream dates are sometimes in the future or
                otherwise implausible, so do not assume a tight date filter
                excludes everything odd.
            is_key: When true, only **key decisions** — the ones with
                significant financial or community impact, which carry extra
                publication and call-in requirements. When false, only
                non-key decisions. Omit for both.
            page: One-based page number.
            per_page: Decisions per page (default 25; upstream caps at 100).

        Returns:
            `items` — the decisions on this page, each with `id`,
            `council_id`, `url` (the council's own decision page),
            `decision_maker` (the person or post that decided),
            `outcome` (e.g. `Recommendations Approved`), `is_key`,
            `is_callable_in` (whether the decision can still be called in for
            scrutiny), `purpose`, `content` (the body text, often empty),
            `date` and `topline` (a one-line LLM summary). Plus `page`,
            `per_page` and `total`. Call `get_decision(id)` for the same
            record with its documents attached.
        """
        parsed_from = _parse_date("date_from", date_from)
        parsed_to = _parse_date("date_to", date_to)
        client = ctx.request_context.lifespan_context.client
        try:
            return await client.list_decisions(
                council_id=council_id,
                date_from=parsed_from,
                date_to=parsed_to,
                is_key=is_key,
                page=page,
                per_page=per_page,
            )
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e

    @server.tool()
    async def get_decision(
        ctx: Context[AppContext], decision_id: int
    ) -> dict[str, Any]:
        """Get one officer or delegated decision, with its papers attached.

        Reach a decision id from `list_decisions`, from a meeting's
        `decisions` (see `get_meeting`), or from the `source_id` of a
        document whose `source_type` is `Decision`.

        Args:
            decision_id: The decision's `id`.

        Returns:
            The decision record (`id`, `council_id`, `url`, `decision_maker`,
            `outcome`, `is_key`, `is_callable_in`, `purpose`, `content`,
            `date`, `topline`) plus `documents` — the reports published for
            the decision. Pass a document's `id` to
            `get_document(include_text=true)` to read it, or to
            `get_document_file_url` for the original PDF. `documents` is
            often empty, in which case `content` and `topline` are all there
            is.
        """
        client = ctx.request_context.lifespan_context.client
        try:
            return await client.get_decision(decision_id)
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e

    @server.tool()
    async def list_people(
        ctx: Context[AppContext],
        council_id: int | None = None,
        is_councillor: bool | None = None,
        party: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """List councillors. The dataset is populated — about 19700 people
        across all councils, every one of them a councillor.

        **`party` is unnormalised free text and is matched exactly**, so a
        single filter value always under-counts. One page of 100 records
        contained `Conservative`, `Conservative Party`, `The Conservative
        Party` and `Local Conservatives`; `Labour`, `Labour Party` and
        `Labour and Co-operative Party`; `Liberal Democrat`, `Liberal
        Democrats` and `(Liberal Democrats)`. `party=Conservative` matches
        3089 people, but that is **not** the number of Conservative
        councillors — never report a filtered `total` as a party's true
        count. To count or gather a party, page through the unfiltered
        results (`per_page=100`) and group the `party` values yourself.

        **`is_councillor=false` currently matches nothing** — there are no
        officers in the dataset, and `is_councillor=true` returns exactly
        the same people as no filter at all, so the flag is not worth
        setting. Do not conclude from an empty officer result that a council
        employs no officers.

        Do not confuse `party` here with the short codes (`CON`, `NOC`) used
        by the unrelated `majority_party` field on **council** records; those
        codes match no person.

        **People are not full-text searchable**; `search_council_data`
        indexes document text and document classifications only. For who
        attended a particular meeting, use `get_meeting`, whose `attendees`
        and `additional_attendees` are populated, or read the minutes.

        Args:
            council_id: Restrict to one council (see `list_councils`).
            is_councillor: When true, only elected councillors; when false,
                only non-councillors (officers) — which currently matches
                nothing. Omit for both.
            party: Match the party exactly as stored upstream, e.g.
                `Conservative` or `Scottish National Party`. No
                normalisation, no substring matching, and the vocabulary is
                inconsistent — see the warning above.
            page: One-based page number.
            per_page: People per page (default 25; upstream caps at 100).

        Returns:
            `items` — the people on this page, each with `id`, `council_id`,
            `name` (including the honorific, e.g. "Councillor Alexander
            McLellan"), `role`, `party` (sometimes null), `is_councillor`,
            `modern_gov_id` and `ocd_id` (both null throughout); plus `page`,
            `per_page` and `total`. `role` has the form
            `"Councillor, <ward>"` — **the ward is embedded in `role`**;
            there is no separate ward field.
        """
        client = ctx.request_context.lifespan_context.client
        try:
            return await client.list_people(
                council_id=council_id,
                is_councillor=is_councillor,
                party=party,
                page=page,
                per_page=per_page,
            )
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e

    @server.tool()
    async def get_person(ctx: Context[AppContext], person_id: int) -> dict[str, Any]:
        """Get one councillor by id.

        Ids come from `list_people`; there is no lookup by name, so find the
        person there first. An unknown id 404s with "Person not found",
        which means that id is not in the dataset — it is **not** evidence
        that the person does not exist or does not hold office. The dataset
        is councillors only, so anyone who is not an elected member (council
        officers, for instance) will simply be absent; use `get_meeting`
        (its `attendees` and `additional_attendees`) or the minutes
        documents to find them.

        Args:
            person_id: The person's `id`, from `list_people`.

        Returns:
            The person record: `id`, `council_id`, `name` (including the
            honorific), `role` (of the form `"Councillor, <ward>"`, which is
            where the ward is recorded), `party` (unnormalised free text,
            sometimes null), `is_councillor`, `modern_gov_id` and `ocd_id`
            (both null throughout).
        """
        client = ctx.request_context.lifespan_context.client
        try:
            return await client.get_person(person_id)
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e

    # -- ChatGPT connector aliases ------------------------------------------
    #
    # ChatGPT only calls tools named exactly `search` and `fetch`, with these
    # exact argument and result shapes; everything above is invisible to it.
    # Other clients see all fourteen and should use the richer tools, which
    # the descriptions say outright.

    @server.tool()
    async def search(ctx: Context[AppContext], query: str) -> dict[str, Any]:
        """ChatGPT-compatible search over UK council documents; other clients
        should prefer `search_council_data`, which exposes council filters,
        paging, classification mode, match highlights and the full records.

        Args:
            query: A short topical phrase, e.g. `council tax reduction`.
                Matching is phrase-style, so phrases beat questions.

        Returns:
            `results` — up to 20 matches, each with `id` (pass to `fetch`),
            `title` and `url` (the council's own link, empty when unknown).
        """
        if len(query) < MIN_QUERY_LENGTH:
            raise ToolError(
                f"query must be at least {MIN_QUERY_LENGTH} characters, "
                f"got {len(query)}"
            )
        backend = ctx.request_context.lifespan_context.search
        try:
            found = await backend.search(
                query,
                council_id=None,
                mode="document",
                size=FETCH_SEARCH_SIZE,
                offset=0,
            )
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e
        return {
            "results": [
                {
                    "id": hit["id"],
                    "title": hit["title"] or hit["id"],
                    "url": hit["record"].get("url") or "",
                }
                for hit in found["hits"]
            ]
        }

    @server.tool()
    async def fetch(ctx: Context[AppContext], id: str) -> dict[str, Any]:  # noqa: A002
        """ChatGPT-compatible retrieval of one record by id; other clients
        should prefer `get_document` (which pages through long text) or
        `get_meeting` (which returns the structured record).

        Args:
            id: `Document-<number>` or `Meeting-<number>`, as returned by
                `search`. Any other prefix is an error — people, decisions
                and councils are not fetchable this way.

        Returns:
            `id`, `title`, `text` (document text or a Markdown rendering of
            the meeting), `url` and `metadata`. **Document text is truncated
            at 20000 characters**; `metadata.has_more` says whether more
            follows, and `get_document(text_offset=...)` is the only way to
            read it.
        """
        kind, numeric_id = _parse_fetch_id(id)
        client = ctx.request_context.lifespan_context.client
        try:
            if kind == "Document":
                document = await client.get_document(numeric_id, include_text=True)
                chunk = window(document.get("text"), 0, FETCH_TEXT_CHARS)
                return {
                    "id": id,
                    "title": document.get("name") or f"Document {numeric_id}",
                    "text": chunk.text,
                    "url": document.get("url") or "",
                    "metadata": {
                        "source_type": document.get("source_type"),
                        "source_id": document.get("source_id"),
                        "kind": document.get("kind"),
                        "is_minutes": document.get("is_minutes"),
                        "text_length": chunk.text_length,
                        "has_more": chunk.has_more,
                    },
                }
            meeting = await client.get_meeting(numeric_id)
        except CouncilApiError as e:
            raise ToolError(_api_error(e)) from e
        return {
            "id": id,
            "title": meeting.get("name") or f"Meeting {numeric_id}",
            "text": _meeting_markdown(meeting),
            "url": meeting.get("url") or "",
            "metadata": {
                "council_id": meeting.get("council_id"),
                "committee_id": meeting.get("committee_id"),
                "date": meeting.get("date"),
                "document_count": len(meeting.get("documents") or []),
                "decision_count": len(meeting.get("decisions") or []),
            },
        }
