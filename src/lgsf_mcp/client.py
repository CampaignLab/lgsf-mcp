"""Async HTTP client for the Council Gateway API.

Thin wrapper over httpx: one method per upstream endpoint, returning the
parsed JSON as plain dicts (no models — KISS). Parameter names and types
mirror the upstream OpenAPI spec at
https://councilgateway.poteris.co.uk/council-api/openapi.json.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Literal, Self

import httpx

# Upstream council-wide listings can take ~15s, hence the generous read timeout.
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class CouncilApiError(Exception):
    """Non-2xx response from the Council Gateway API.

    ``detail`` is the parsed JSON error detail when available (for 422 this
    is the FastAPI validation error list), otherwise the raw response text.
    ``headers`` are the response headers, kept so callers can act on things
    like ``Retry-After`` without the client itself having a retry policy.
    """

    def __init__(
        self,
        status: int,
        detail: object,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(f"Council API error {status}: {detail!r}")
        self.status = status
        self.detail = detail
        self.headers = httpx.Headers(headers)

    @property
    def retry_after(self) -> float | None:
        """``Retry-After`` in seconds, or None if absent or unparseable.

        RFC 9110 allows either delta-seconds or an HTTP-date. A date already
        in the past yields 0.0, not a negative wait. Deciding whether to
        honour the value, and any upper bound on it, is the caller's job.
        """
        return _parse_retry_after(self.headers.get("Retry-After"))


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value into a non-negative seconds delay."""
    if value is None:
        return None
    value = value.strip()
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    # parsedate_to_datetime returns naive for "-0000"; RFC 9110 dates are UTC.
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    now = datetime.datetime.now(datetime.UTC)
    return max(0.0, (when - now).total_seconds())


def _clean_params(
    params: Mapping[str, object] | None,
) -> dict[str, str | int | float | bool]:
    """Drop None values and convert dates to ISO strings; pass the rest as-is
    (httpx encodes bools as true/false)."""
    cleaned: dict[str, str | int | float | bool] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, datetime.date):
            cleaned[key] = value.isoformat()
        elif isinstance(value, str | int | float):  # bool is a subclass of int
            cleaned[key] = value
        else:
            raise TypeError(f"Unsupported query parameter type for {key!r}: {value!r}")
    return cleaned


class CouncilGatewayClient:
    """Async client for the Council Gateway API (UK local government data)."""

    DEFAULT_BASE_URL = "https://councilgateway.poteris.co.uk/council-api"

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(
            base_url=(base_url or self.DEFAULT_BASE_URL).rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
        )

    @classmethod
    def from_env(cls) -> CouncilGatewayClient:
        """Build a client from COUNCIL_API_URL and COUNCIL_API_TOKEN."""
        return cls(
            base_url=os.environ.get("COUNCIL_API_URL"),
            token=os.environ.get("COUNCIL_API_TOKEN"),
        )

    @property
    def is_closed(self) -> bool:
        """True once aclose() has run; the underlying httpx client is unusable."""
        return self._client.is_closed

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(
        self, path: str, params: Mapping[str, object] | None = None
    ) -> Any:
        response = await self._client.get(path, params=_clean_params(params))
        if not response.is_success:
            try:
                body: object = response.json()
            except ValueError:
                body = response.text
            # FastAPI wraps error details as {"detail": ...}; unwrap it.
            if isinstance(body, dict) and "detail" in body:
                body = body["detail"]
            raise CouncilApiError(response.status_code, body, response.headers)
        return response.json()

    # -- Councils ---------------------------------------------------------

    async def list_councils(
        self,
        council_type: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """GET /councils — paginated list of councils."""
        result: dict[str, Any] = await self._get(
            "/councils",
            {"council_type": council_type, "page": page, "per_page": per_page},
        )
        return result

    async def get_council(self, council_id: int) -> dict[str, Any]:
        """GET /councils/{id} — one council."""
        result: dict[str, Any] = await self._get(f"/councils/{council_id}")
        return result

    async def list_council_committees(
        self,
        council_id: int,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """GET /councils/{id}/committees — paginated committees of a council."""
        result: dict[str, Any] = await self._get(
            f"/councils/{council_id}/committees",
            {"page": page, "per_page": per_page},
        )
        return result

    # -- Meetings ---------------------------------------------------------

    async def list_meetings(
        self,
        council_id: int | None = None,
        committee_id: int | None = None,
        date_from: datetime.date | str | None = None,
        date_to: datetime.date | str | None = None,
        has_minutes: bool | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """GET /meetings — paginated meetings, filterable by council/committee/date."""
        result: dict[str, Any] = await self._get(
            "/meetings",
            {
                "council_id": council_id,
                "committee_id": committee_id,
                "date_from": date_from,
                "date_to": date_to,
                "has_minutes": has_minutes,
                "page": page,
                "per_page": per_page,
            },
        )
        return result

    async def get_meeting(self, meeting_id: int) -> dict[str, Any]:
        """GET /meetings/{id} — one meeting with embedded related records."""
        result: dict[str, Any] = await self._get(f"/meetings/{meeting_id}")
        return result

    # -- Documents --------------------------------------------------------

    async def list_documents(
        self,
        meeting_id: int | None = None,
        decision_id: int | None = None,
        council_id: int | None = None,
        is_minutes: bool | None = None,
        kind: str | None = None,
        processing_status: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """GET /documents — paginated documents with filters."""
        result: dict[str, Any] = await self._get(
            "/documents",
            {
                "meeting_id": meeting_id,
                "decision_id": decision_id,
                "council_id": council_id,
                "is_minutes": is_minutes,
                "kind": kind,
                "processing_status": processing_status,
                "page": page,
                "per_page": per_page,
            },
        )
        return result

    async def get_document(
        self, document_id: int, include_text: bool = False
    ) -> dict[str, Any]:
        """GET /documents/{id} — one document, optionally with extracted text."""
        result: dict[str, Any] = await self._get(
            f"/documents/{document_id}", {"include_text": include_text}
        )
        return result

    async def list_document_classifications(
        self,
        document_id: int,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """GET /documents/{id}/classifications — paginated classifications."""
        result: dict[str, Any] = await self._get(
            f"/documents/{document_id}/classifications",
            {"page": page, "per_page": per_page},
        )
        return result

    async def get_document_file(self, document_id: int) -> dict[str, Any]:
        """GET /documents/{id}/file?redirect=false — a short-lived download URL.

        Always requests ``redirect=false`` so upstream returns JSON
        (``url``, ``filename``, ``content_type``, ``byte_size``,
        ``expires_in``) instead of a 302 to the file bytes.
        """
        result: dict[str, Any] = await self._get(
            f"/documents/{document_id}/file", {"redirect": False}
        )
        return result

    # -- Decisions --------------------------------------------------------

    async def list_decisions(
        self,
        council_id: int | None = None,
        date_from: datetime.date | str | None = None,
        date_to: datetime.date | str | None = None,
        is_key: bool | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """GET /decisions — paginated decisions with filters."""
        result: dict[str, Any] = await self._get(
            "/decisions",
            {
                "council_id": council_id,
                "date_from": date_from,
                "date_to": date_to,
                "is_key": is_key,
                "page": page,
                "per_page": per_page,
            },
        )
        return result

    async def get_decision(self, decision_id: int) -> dict[str, Any]:
        """GET /decisions/{id} — one decision."""
        result: dict[str, Any] = await self._get(f"/decisions/{decision_id}")
        return result

    # -- People -----------------------------------------------------------

    async def list_people(
        self,
        council_id: int | None = None,
        is_councillor: bool | None = None,
        party: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """GET /people — paginated people with filters."""
        result: dict[str, Any] = await self._get(
            "/people",
            {
                "council_id": council_id,
                "is_councillor": is_councillor,
                "party": party,
                "page": page,
                "per_page": per_page,
            },
        )
        return result

    async def get_person(self, person_id: int) -> dict[str, Any]:
        """GET /people/{id} — one person."""
        result: dict[str, Any] = await self._get(f"/people/{person_id}")
        return result

    # -- Search and health --------------------------------------------------

    async def search(
        self,
        q: str,
        council_id: int | None = None,
        mode: Literal["document", "classification"] = "document",
        size: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /search — full-text search over documents or classifications."""
        result: dict[str, Any] = await self._get(
            "/search",
            {
                "q": q,
                "council_id": council_id,
                "mode": mode,
                "size": size,
                "offset": offset,
            },
        )
        return result

    async def healthz(self) -> dict[str, Any]:
        """GET /healthz — upstream health check."""
        result: dict[str, Any] = await self._get("/healthz")
        return result
