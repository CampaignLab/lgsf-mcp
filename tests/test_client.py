"""Offline tests for CouncilGatewayClient against recorded fixtures.

No network: every request goes through the MockTransport in ``conftest.py``,
so an unmapped route (or a changed query string) surfaces as a 404 rather than
a live call.
"""

from __future__ import annotations

import datetime
from email.utils import format_datetime

import httpx
import pytest

from lgsf_mcp.client import CouncilApiError, CouncilGatewayClient
from tests.conftest import (
    COUNCIL_ID,
    DECISION_ID,
    DOCUMENT_ID,
    MEETING_ID,
    PERSON_ID,
    FixtureClient,
    FixtureTransport,
    load_fixture,
)


def params(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)


# -- Every method returns the recorded payload ----------------------------


async def test_list_councils(fixture_client: FixtureClient) -> None:
    assert await fixture_client.list_councils(per_page=2) == load_fixture("councils")


async def test_get_council(fixture_client: FixtureClient) -> None:
    assert await fixture_client.get_council(COUNCIL_ID) == load_fixture("council_226")


async def test_list_council_committees(fixture_client: FixtureClient) -> None:
    result = await fixture_client.list_council_committees(COUNCIL_ID, per_page=2)
    assert result == load_fixture("council_226_committees")


async def test_list_meetings(fixture_client: FixtureClient) -> None:
    result = await fixture_client.list_meetings(council_id=COUNCIL_ID, per_page=2)
    assert result == load_fixture("meetings_council_226")


async def test_get_meeting(fixture_client: FixtureClient) -> None:
    assert await fixture_client.get_meeting(MEETING_ID) == load_fixture("meeting")


async def test_list_documents(fixture_client: FixtureClient) -> None:
    result = await fixture_client.list_documents(meeting_id=MEETING_ID, per_page=2)
    assert result == load_fixture("documents_meeting")


async def test_get_document_without_text(fixture_client: FixtureClient) -> None:
    document = await fixture_client.get_document(DOCUMENT_ID)
    assert document == load_fixture("document")
    assert "text" not in document


async def test_get_document_with_text(fixture_client: FixtureClient) -> None:
    document = await fixture_client.get_document(DOCUMENT_ID, include_text=True)
    assert document == load_fixture("document_text")
    assert isinstance(document["text"], str) and document["text"]


async def test_get_document_file(fixture_client: FixtureClient) -> None:
    assert await fixture_client.get_document_file(DOCUMENT_ID) == load_fixture(
        "document_file"
    )


async def test_list_decisions(fixture_client: FixtureClient) -> None:
    assert await fixture_client.list_decisions(per_page=2) == load_fixture("decisions")


async def test_get_decision(fixture_client: FixtureClient) -> None:
    assert await fixture_client.get_decision(DECISION_ID) == load_fixture("decision")


async def test_list_people(fixture_client: FixtureClient) -> None:
    result = await fixture_client.list_people(council_id=COUNCIL_ID, per_page=2)
    assert result == load_fixture("people_council_226")


async def test_get_person_404(fixture_client: FixtureClient) -> None:
    """Upstream /people is empty, so the recorded response is a genuine 404."""
    with pytest.raises(CouncilApiError) as excinfo:
        await fixture_client.get_person(PERSON_ID)
    assert excinfo.value.status == 404
    assert excinfo.value.detail == load_fixture("person")


async def test_search(fixture_client: FixtureClient) -> None:
    assert await fixture_client.search("housing", size=2) == load_fixture(
        "search_housing"
    )


# -- Query parameter serialisation ----------------------------------------


async def test_none_params_are_omitted(fixture_client: FixtureClient) -> None:
    await fixture_client.list_meetings(council_id=COUNCIL_ID, per_page=2)
    sent = params(fixture_client.last_request)
    assert sent == {"council_id": str(COUNCIL_ID), "page": "1", "per_page": "2"}
    for omitted in ("committee_id", "date_from", "date_to", "has_minutes"):
        assert omitted not in sent


async def test_dates_serialise_as_iso(fixture_client: FixtureClient) -> None:
    with pytest.raises(CouncilApiError):  # unmapped route: only the query matters
        await fixture_client.list_meetings(
            date_from=datetime.date(2026, 1, 31),
            date_to=datetime.date(2026, 12, 1),
        )
    sent = params(fixture_client.last_request)
    assert sent["date_from"] == "2026-01-31"
    assert sent["date_to"] == "2026-12-01"


async def test_date_strings_pass_through(fixture_client: FixtureClient) -> None:
    with pytest.raises(CouncilApiError):
        await fixture_client.list_meetings(date_from="2026-01-31")
    assert params(fixture_client.last_request)["date_from"] == "2026-01-31"


async def test_has_minutes_serialises_as_true(fixture_client: FixtureClient) -> None:
    with pytest.raises(CouncilApiError):
        await fixture_client.list_meetings(has_minutes=True)
    assert params(fixture_client.last_request)["has_minutes"] == "true"


async def test_get_document_file_sends_redirect_false(
    fixture_client: FixtureClient,
) -> None:
    await fixture_client.get_document_file(DOCUMENT_ID)
    request = fixture_client.last_request
    assert request.url.path.endswith(f"/documents/{DOCUMENT_ID}/file")
    assert params(request) == {"redirect": "false"}


async def test_include_text_serialises_as_bool(fixture_client: FixtureClient) -> None:
    await fixture_client.get_document(DOCUMENT_ID)
    assert params(fixture_client.last_request) == {"include_text": "false"}
    await fixture_client.get_document(DOCUMENT_ID, include_text=True)
    assert params(fixture_client.last_request) == {"include_text": "true"}


async def test_unsupported_param_type_is_rejected(
    fixture_client: FixtureClient,
) -> None:
    with pytest.raises(TypeError):
        await fixture_client.list_meetings(date_from=datetime.timedelta(days=1))  # type: ignore[arg-type]


# -- Authorization header --------------------------------------------------


async def test_no_authorization_header_without_token(
    fixture_client: FixtureClient,
) -> None:
    await fixture_client.list_councils(per_page=2)
    assert "authorization" not in fixture_client.last_request.headers


async def test_authorization_header_with_token(transport: FixtureTransport) -> None:
    client = FixtureClient(transport, token="s3cret")
    try:
        await client.list_councils(per_page=2)
    finally:
        await client.aclose()
    assert client.last_request.headers["authorization"] == "Bearer s3cret"


# -- Errors ----------------------------------------------------------------


async def test_422_raises_council_api_error(fixture_client: FixtureClient) -> None:
    with pytest.raises(CouncilApiError) as excinfo:
        await fixture_client.list_councils(per_page=1000)
    error = excinfo.value
    assert error.status == 422
    assert error.detail == load_fixture("validation_422")
    assert "422" in str(error)


async def test_unmapped_route_raises_404(fixture_client: FixtureClient) -> None:
    with pytest.raises(CouncilApiError) as excinfo:
        await fixture_client.healthz()
    assert excinfo.value.status == 404


async def test_non_json_error_body_uses_text(transport: FixtureTransport) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream is down")

    client = CouncilGatewayClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CouncilApiError) as excinfo:
            await client.healthz()
    finally:
        await client.aclose()
    assert excinfo.value.status == 503
    assert excinfo.value.detail == "upstream is down"


# -- Base URL handling -----------------------------------------------------


async def test_base_url_trailing_slash_is_stripped(
    transport: FixtureTransport,
) -> None:
    client = CouncilGatewayClient(
        base_url="https://example.test/council-api/",
        transport=httpx.MockTransport(transport),
    )
    try:
        await client.list_councils(per_page=2)
    finally:
        await client.aclose()
    assert str(transport.last_request.url).startswith(
        "https://example.test/council-api/councils?"
    )


# -- Lifecycle -------------------------------------------------------------


async def test_is_closed_tracks_aclose(transport: FixtureTransport) -> None:
    client = FixtureClient(transport)
    assert not client.is_closed
    await client.aclose()
    assert client.is_closed


async def test_is_closed_after_context_manager_exit(
    transport: FixtureTransport,
) -> None:
    async with FixtureClient(transport) as client:
        assert not client.is_closed
    assert client.is_closed


# -- Error headers and Retry-After -----------------------------------------


def _erroring_client(status: int, headers: dict[str, str]) -> CouncilGatewayClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "slow down"}, headers=headers)

    return CouncilGatewayClient(transport=httpx.MockTransport(handler))


async def _raised(status: int, headers: dict[str, str]) -> CouncilApiError:
    client = _erroring_client(status, headers)
    try:
        with pytest.raises(CouncilApiError) as excinfo:
            await client.get_council(226)
    finally:
        await client.aclose()
    return excinfo.value


async def test_error_carries_response_headers() -> None:
    error = await _raised(429, {"Retry-After": "5", "X-Request-Id": "abc"})
    assert error.headers["retry-after"] == "5"  # httpx.Headers is case-insensitive
    assert error.headers["X-Request-Id"] == "abc"


async def test_retry_after_delta_seconds() -> None:
    assert (await _raised(429, {"Retry-After": "5"})).retry_after == 5.0


async def test_retry_after_absent_is_none() -> None:
    assert (await _raised(429, {})).retry_after is None


@pytest.mark.parametrize("value", ["soon", "", "5.5", "1,2"])
async def test_retry_after_unparseable_is_none(value: str) -> None:
    assert (await _raised(429, {"Retry-After": value})).retry_after is None


async def test_retry_after_negative_delta_clamps_to_zero() -> None:
    assert (await _raised(429, {"Retry-After": "-5"})).retry_after == 0.0


async def test_retry_after_http_date_is_seconds_from_now() -> None:
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=30)
    header = format_datetime(when, usegmt=True)
    retry_after = (await _raised(503, {"Retry-After": header})).retry_after
    assert retry_after == pytest.approx(30.0, abs=2.0)


async def test_retry_after_http_date_in_the_past_is_zero() -> None:
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
    header = format_datetime(when, usegmt=True)
    assert (await _raised(503, {"Retry-After": header})).retry_after == 0.0


async def test_retry_after_naive_http_date_is_treated_as_utc() -> None:
    """A "-0000" date parses naive; assuming local time could be hours out."""
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=30)
    # format_datetime emits "-0000" for a naive input, which parses back naive.
    header = format_datetime(when.replace(tzinfo=None))
    assert header.endswith("-0000")
    retry_after = (await _raised(503, {"Retry-After": header})).retry_after
    assert retry_after == pytest.approx(30.0, abs=2.0)


def test_error_without_headers_still_constructs() -> None:
    """The two-argument form stays valid; headers are optional."""
    error = CouncilApiError(500, "boom")
    assert error.status == 500
    assert error.retry_after is None
    assert not error.headers
