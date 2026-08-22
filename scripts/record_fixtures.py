#!/usr/bin/env python3
"""Record Council Gateway API responses as test fixtures.

Run with ``uv run python scripts/record_fixtures.py``. Hits the live API and
writes pretty-printed JSON to ``tests/fixtures/``, plus ``ids.json`` recording
the record ids chosen so that the tests and this script agree.

The ids are discovered from listings rather than hard-coded, because upstream
data changes; ``ids.json`` is the record of what a given fixture set used.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lgsf_mcp.client import CouncilApiError, CouncilGatewayClient  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

ABERDEEN_CITY_ID = 226

# Enough to prove the text field round-trips without committing a whole PDF.
TEXT_LIMIT = 3000

# A whole run is ~16 sequential live calls, so one transient 5xx should not
# abort it. Other 4xx are never retried: the recorder deliberately captures a
# 422 as a fixture and treats a 404 on /documents/{id}/file as "try the next
# document", and retrying either would only waste time.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 1.0

# Upstream is unthrottled today, but ~16 sequential calls is exactly the shape
# of client that would trip a rate limit if one were added, so honour 429.
TOO_MANY_REQUESTS = 429

# A server may ask us to wait for hours; that is not worth blocking a fixture
# run for, so treat anything longer as "give up and let the caller see it".
MAX_RETRY_AFTER = 60.0


def is_transient(exc: BaseException) -> bool:
    """True for errors worth retrying: 429, upstream 5xx, transport failures."""
    if isinstance(exc, CouncilApiError):
        return exc.status >= 500 or exc.status == TOO_MANY_REQUESTS
    return isinstance(exc, httpx.TransportError)


def backoff_delay(exc: BaseException, attempt: int) -> float:
    """How long to wait before ``attempt + 1``.

    A server-supplied ``Retry-After`` beats our guesswork, but is capped so a
    hostile or mistaken header cannot hang the run.
    """
    if isinstance(exc, CouncilApiError):
        retry_after = exc.retry_after
        if retry_after is not None:
            return min(retry_after, MAX_RETRY_AFTER)
    return RETRY_BACKOFF * float(2 ** (attempt - 1))


async def retry[T](call: Callable[[], Awaitable[T]]) -> T:
    """Await ``call()``, retrying transient failures with exponential backoff.

    Takes a factory rather than an awaitable because a coroutine can only be
    awaited once, so each attempt needs a fresh one.
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await call()
        except Exception as exc:
            if attempt == RETRY_ATTEMPTS or not is_transient(exc):
                raise
            delay = backoff_delay(exc, attempt)
            print(
                f"attempt {attempt}/{RETRY_ATTEMPTS} failed ({exc}); "
                f"retrying in {delay:g}s",
                file=sys.stderr,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def write(name: str, payload: object) -> None:
    path = FIXTURES / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    print(f"wrote {path.relative_to(Path.cwd())}")


def first_id(listing: dict[str, Any]) -> int | None:
    items = listing.get("items") or []
    return int(items[0]["id"]) if items else None


async def error_detail(
    call: Callable[[], Awaitable[object]], expect: int = 404
) -> object:
    """Await a call expected to fail and return the error detail it raised."""
    try:
        await retry(call)
    except CouncilApiError as exc:
        if exc.status != expect:
            raise SystemExit(
                f"expected {expect}, got {exc.status}: {exc.detail!r}"
            ) from exc
        return exc.detail
    raise SystemExit(f"expected a {expect} error, but the call succeeded")


def redact_url(url: str) -> str:
    """Strip a presigned URL's query string; it carries signed credentials."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "REDACTED", ""))


async def usable_document(client: CouncilGatewayClient, document_id: int) -> bool:
    """True if the document has both extracted text and a stored file.

    Documents whose ``extract_status`` is still ``pending`` have ``text: null``
    and make ``/documents/{id}/file`` return 404 "Document has no stored file",
    which would make the ``document_text``/``document_file`` fixtures useless.
    """
    try:
        await retry(lambda: client.get_document_file(document_id))
    except CouncilApiError as exc:
        if exc.status == 404:
            return False
        raise
    with_text = await retry(lambda: client.get_document(document_id, include_text=True))
    return bool(with_text.get("text"))


async def find_meeting_and_document(
    client: CouncilGatewayClient, meetings: dict[str, Any]
) -> tuple[int, dict[str, Any], int]:
    """Find a meeting whose document listing contains a usable document.

    Not every meeting has documents and not every document has been processed;
    fabricating fixture content would defeat the point, so widen the search
    rather than record empty or 404-ing responses.
    """
    seen: set[int] = set()

    async def scan(meeting_id: int) -> tuple[int, dict[str, Any], int] | None:
        seen.add(meeting_id)
        documents = await retry(
            lambda: client.list_documents(meeting_id=meeting_id, per_page=2)
        )
        for item in documents.get("items", []):
            document_id = int(item["id"])
            if await usable_document(client, document_id):
                return meeting_id, documents, document_id
        return None

    for item in meetings.get("items", []):
        found = await scan(int(item["id"]))
        if found:
            return found

    # Fall back to a wider page of meetings for the same council.
    wider = await retry(
        lambda: client.list_meetings(council_id=ABERDEEN_CITY_ID, per_page=50)
    )
    for item in wider.get("items", []):
        meeting_id = int(item["id"])
        if meeting_id in seen:
            continue
        found = await scan(meeting_id)
        if found:
            return found
    raise SystemExit("no meeting with a usable document found for council 226")


async def record() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    ids: dict[str, Any] = {
        "council_id": ABERDEEN_CITY_ID,
        "note": (
            "Ids used when these fixtures were recorded. The document is the "
            "first one of the chosen meeting that has both extracted text and "
            "a stored file; unprocessed documents 404 on /file."
        ),
    }

    async with CouncilGatewayClient() as client:
        write("councils", await retry(lambda: client.list_councils(per_page=2)))
        write("council_226", await retry(lambda: client.get_council(ABERDEEN_CITY_ID)))
        write(
            "council_226_committees",
            await retry(
                lambda: client.list_council_committees(ABERDEEN_CITY_ID, per_page=2)
            ),
        )

        meetings = await retry(
            lambda: client.list_meetings(council_id=ABERDEEN_CITY_ID, per_page=2)
        )
        write("meetings_council_226", meetings)

        meeting_id, documents, document_id = await find_meeting_and_document(
            client, meetings
        )
        ids["meeting_id"] = meeting_id
        ids["meeting_is_first_listed"] = meeting_id == first_id(meetings)
        write("meeting", await retry(lambda: client.get_meeting(meeting_id)))
        write("documents_meeting", documents)

        ids["document_id"] = document_id
        ids["document_is_first_listed"] = document_id == first_id(documents)
        write("document", await retry(lambda: client.get_document(document_id)))

        with_text = await retry(
            lambda: client.get_document(document_id, include_text=True)
        )
        text = with_text.get("text")
        if isinstance(text, str):
            with_text["text"] = text[:TEXT_LIMIT]
        write("document_text", with_text)

        file_info = await retry(lambda: client.get_document_file(document_id))
        if isinstance(file_info.get("url"), str):
            file_info["url"] = redact_url(file_info["url"])
        write("document_file", file_info)

        decisions = await retry(lambda: client.list_decisions(per_page=2))
        write("decisions", decisions)
        decision_id = first_id(decisions)
        if decision_id is None:
            raise SystemExit("no decisions returned")
        ids["decision_id"] = decision_id
        write("decision", await retry(lambda: client.get_decision(decision_id)))

        people = await retry(
            lambda: client.list_people(council_id=ABERDEEN_CITY_ID, per_page=2)
        )
        write("people_council_226", people)
        person_id = first_id(people)
        if person_id is None:
            raise SystemExit(f"no people returned for council {ABERDEEN_CITY_ID}")
        ids["person_id"] = person_id
        ids["person_available"] = True
        write("person", await retry(lambda: client.get_person(person_id)))

        write("search_housing", await retry(lambda: client.search("housing", size=2)))

        write(
            "validation_422",
            await error_detail(lambda: client.list_councils(per_page=1000), expect=422),
        )

    write("ids", ids)


if __name__ == "__main__":
    asyncio.run(record())
