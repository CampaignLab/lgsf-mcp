"""Search backends for the Council Gateway API.

Today there is one backend, ``TextSearchBackend``, which wraps upstream
``/search`` (an OpenSearch ``multi_match`` phrase query with ``slop: 10``).
The ``SearchBackend`` protocol exists so a semantic or hybrid backend can be
dropped in later without changing the tool layer or the hit shape the model
sees.

Upstream caveats baked into the normalised result:

* Only **document text and document classifications** are indexed. Meetings,
  decisions and people are not searchable.
* ``total`` is capped by OpenSearch's ``track_total_hits`` at
  ``TOTAL_CAP`` (10000), so it is a floor rather than a true count. The
  normalised result flags this as ``total_is_capped``.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Protocol

from .client import CouncilGatewayClient

SearchMode = Literal["document", "classification"]

#: OpenSearch's ``track_total_hits`` ceiling; a ``total`` at or above this is
#: a floor, not an exact count.
TOTAL_CAP = 10_000

#: Upstream wraps matched terms in ``<strong>``; the model reads Markdown.
_HIGHLIGHT_OPEN = re.compile(r"<strong>")
_HIGHLIGHT_CLOSE = re.compile(r"</strong>")


class SearchBackend(Protocol):
    """A source of normalised search hits.

    Implementations must return the shape produced by
    :func:`normalise_response` so tools stay backend-agnostic.
    """

    async def search(
        self,
        query: str,
        *,
        council_id: int | None,
        mode: SearchMode,
        size: int,
        offset: int,
    ) -> dict[str, Any]: ...


def _markdown_highlight(fragment: str) -> str:
    """Convert upstream's ``<strong>`` emphasis to Markdown ``**…**``."""
    return _HIGHLIGHT_CLOSE.sub("**", _HIGHLIGHT_OPEN.sub("**", fragment))


def _normalise_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """Flatten one upstream hit into the shape tools return.

    ``source.name`` is frequently null for documents (the scraper does not
    always capture a title), so the meeting name is the fallback label.
    """
    source: dict[str, Any] = hit.get("source") or {}
    highlight: dict[str, Any] = hit.get("highlight") or {}
    meeting_name = source.get("meeting_name")
    return {
        "id": hit.get("id"),
        "score": hit.get("score"),
        # Non-document hit types (e.g. classifications) have no document id.
        "document_id": source.get("id") if source.get("type") == "Document" else None,
        "meeting_id": source.get("meeting_id"),
        "meeting_name": meeting_name,
        "title": source.get("name") if source.get("name") is not None else meeting_name,
        "highlights": [
            _markdown_highlight(fragment)
            for fragments in highlight.values()
            for fragment in fragments
        ],
        "record": hit.get("record") or {},
    }


def normalise_response(payload: dict[str, Any], mode: SearchMode) -> dict[str, Any]:
    """Normalise a raw ``/search`` payload into the tool-facing result."""
    total = payload.get("total") or 0
    return {
        "total": total,
        "total_is_capped": total >= TOTAL_CAP,
        "mode": payload.get("mode") or mode,
        "hits": [_normalise_hit(hit) for hit in payload.get("hits") or []],
    }


class TextSearchBackend:
    """Full-text ``SearchBackend`` backed by upstream ``/search``."""

    def __init__(self, client: CouncilGatewayClient) -> None:
        self._client = client

    async def search(
        self,
        query: str,
        *,
        council_id: int | None = None,
        mode: SearchMode = "document",
        size: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Run a phrase-style query upstream and normalise the hits."""
        payload = await self._client.search(
            query,
            council_id=council_id,
            mode=mode,
            size=size,
            offset=offset,
        )
        return normalise_response(payload, mode)
