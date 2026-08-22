"""Tests for the static bearer token guarding the HTTP transport.

The integration tests drive the server's Starlette app in-process through
``httpx.ASGITransport`` rather than spawning a subprocess, which keeps them
fast and deterministic. Three things that recipe needs and that are easy to
get wrong:

* ``ASGITransport`` does not run lifespan events, so the app's lifespan has to
  be entered by hand. Without it the StreamableHTTP session manager never
  starts and every ``POST /mcp`` fails for reasons unrelated to auth.
* The transport requires ``Accept: application/json, text/event-stream`` and
  rejects the request before auth otherwise, which would make an unauthorized
  request look authorized-but-malformed.
* httpx refuses to *send* a non-ASCII header, so the one test that needs to
  builds an ASGI scope by hand instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

import lgsf_mcp.__main__ as main_module
from lgsf_mcp.auth import STATIC_CLIENT_ID, StaticTokenVerifier, auth_settings
from lgsf_mcp.server import create_server

from .conftest import FixtureClient

TOKEN = "s3cret"
PUBLIC_URL = "http://127.0.0.1:8000"

#: Both are mandatory: the StreamableHTTP transport 406s a request that does
#: not accept an SSE response.
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


async def test_verifier_accepts_the_configured_token() -> None:
    access = await StaticTokenVerifier(TOKEN).verify_token(TOKEN)
    assert access is not None
    assert access.token == TOKEN
    assert access.client_id == STATIC_CLIENT_ID
    assert access.scopes == []


@pytest.mark.parametrize(
    "wrong",
    [
        "",
        "wrong",
        TOKEN + "x",
        TOKEN.upper(),
        # Non-ASCII must be rejected, not crash: `secrets.compare_digest`
        # raises TypeError on `str` holding non-ASCII, and header values reach
        # the verifier Latin-1-decoded, so a caller can put one there. Before
        # the bytes comparison this surfaced as a 500 rather than a 401.
        "café",
        "ÿ" * len(TOKEN),
    ],
)
async def test_verifier_rejects_anything_else(wrong: str) -> None:
    assert await StaticTokenVerifier(TOKEN).verify_token(wrong) is None


async def test_verifier_handles_a_non_ascii_configured_token() -> None:
    """A non-ASCII secret must still compare, in both directions."""
    verifier = StaticTokenVerifier("pässwörd")
    assert await verifier.verify_token("pässwörd") is not None
    assert await verifier.verify_token("password") is None


def test_auth_settings_advertise_the_public_url() -> None:
    settings = auth_settings(PUBLIC_URL)
    assert str(settings.issuer_url) == PUBLIC_URL
    assert str(settings.resource_server_url) == f"{PUBLIC_URL}/mcp"


def test_auth_settings_reject_a_non_url() -> None:
    with pytest.raises(ValueError):
        auth_settings("not a url")


@asynccontextmanager
async def _serve(
    client: FixtureClient, **auth: Any
) -> AsyncIterator[httpx.AsyncClient]:
    """Drive a freshly built server in-process, with its lifespan running.

    This is a context manager entered inside each test rather than a pytest
    fixture on purpose. ``StreamableHTTPSessionManager.run`` holds an anyio
    task group open across its yield, and pytest-asyncio runs async-fixture
    setup and teardown in *different* tasks, so a fixture would blow up on
    teardown with "Attempted to exit cancel scope in a different task".

    ``httpx.ASGITransport`` also never emits lifespan events, so the app's
    lifespan — where the session manager starts and our own ``AppContext`` is
    built — has to be entered explicitly.
    """
    app = create_server(client, **auth).streamable_http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as http:
            yield http


def _authed(client: FixtureClient) -> Any:
    return _serve(client, token=TOKEN, public_url=PUBLIC_URL)


async def test_missing_header_is_rejected(fixture_client: FixtureClient) -> None:
    async with _authed(fixture_client) as http:
        response = await http.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == 401
    assert "Bearer" in response.headers["WWW-Authenticate"]


async def test_wrong_token_is_rejected(fixture_client: FixtureClient) -> None:
    async with _authed(fixture_client) as http:
        response = await http.post(
            "/mcp",
            json=INITIALIZE,
            headers={**MCP_HEADERS, "Authorization": "Bearer wrong"},
        )
    assert response.status_code == 401
    assert "Bearer" in response.headers["WWW-Authenticate"]


async def test_correct_token_can_initialize(fixture_client: FixtureClient) -> None:
    async with _authed(fixture_client) as http:
        response = await http.post(
            "/mcp",
            json=INITIALIZE,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 200
    assert '"serverInfo"' in response.text
    assert '"lgsf-mcp"' in response.text


async def test_resource_metadata_points_at_the_public_url(
    fixture_client: FixtureClient,
) -> None:
    async with _authed(fixture_client) as http:
        response = await http.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    assert response.json()["resource"] == f"{PUBLIC_URL}/mcp"


async def test_without_a_token_the_server_stays_open(
    fixture_client: FixtureClient,
) -> None:
    """No regression: omitting the token leaves the endpoint unauthenticated."""
    async with _serve(fixture_client) as http:
        response = await http.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        metadata = await http.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    assert '"serverInfo"' in response.text
    # No auth configured means no protected-resource metadata to advertise.
    assert metadata.status_code == 404


async def test_non_ascii_header_is_rejected_not_crashed(
    fixture_client: FixtureClient,
) -> None:
    """A Latin-1 ``Authorization`` header must 401, not 500.

    Driven through raw ASGI because httpx refuses to *send* a non-ASCII header,
    while a socket-level client can: HTTP header bytes are Latin-1 and Starlette
    decodes them as such.
    """
    app = create_server(
        fixture_client, token=TOKEN, public_url=PUBLIC_URL
    ).streamable_http_app()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"accept", b"application/json, text/event-stream"),
            (b"content-type", b"application/json"),
            (b"authorization", "Bearer café".encode("latin-1")),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
    }
    messages: list[MutableMapping[str, Any]] = []

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        messages.append(message)

    async with app.router.lifespan_context(app):
        await app(scope, receive, send)

    starts = [m for m in messages if m["type"] == "http.response.start"]
    assert [m["status"] for m in starts] == [401]


class _RecordingServer:
    """Stands in for MCPServer, capturing what ``main`` asked it to run."""

    def __init__(self) -> None:
        self.run_kwargs: dict[str, Any] | None = None

    def run(self, **kwargs: Any) -> None:
        self.run_kwargs = kwargs


@pytest.fixture
def cli(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], _RecordingServer]:
    """Run ``main`` without serving: record create_server/run arguments."""
    calls: list[dict[str, Any]] = []
    server = _RecordingServer()

    def fake_create_server(client: Any = None, **kwargs: Any) -> _RecordingServer:
        calls.append(kwargs)
        return server

    monkeypatch.setattr(main_module, "create_server", fake_create_server)
    return calls, server


def test_http_transport_applies_the_token(
    cli: tuple[list[dict[str, Any]], _RecordingServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, server = cli
    monkeypatch.setenv(main_module.TOKEN_ENV_VAR, TOKEN)
    main_module.main(["--transport", "streamable-http", "--port", "9123"])
    assert calls == [{"token": TOKEN, "public_url": "http://127.0.0.1:9123"}]
    assert server.run_kwargs is not None
    assert server.run_kwargs["transport"] == "streamable-http"


def test_public_url_overrides_the_derived_default(
    cli: tuple[list[dict[str, Any]], _RecordingServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = cli
    monkeypatch.setenv(main_module.TOKEN_ENV_VAR, TOKEN)
    main_module.main(
        ["--transport", "streamable-http", "--public-url", "https://host.ts.net"]
    )
    assert calls[0]["public_url"] == "https://host.ts.net"


def test_http_transport_without_the_env_var_stays_open(
    cli: tuple[list[dict[str, Any]], _RecordingServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = cli
    monkeypatch.delenv(main_module.TOKEN_ENV_VAR, raising=False)
    main_module.main(["--transport", "streamable-http"])
    assert calls[0]["token"] is None


def test_stdio_ignores_the_token_and_warns(
    cli: tuple[list[dict[str, Any]], _RecordingServer],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls, server = cli
    monkeypatch.setenv(main_module.TOKEN_ENV_VAR, TOKEN)
    main_module.main([])
    assert calls == [{}]
    assert server.run_kwargs == {"transport": "stdio"}
    captured = capsys.readouterr()
    assert main_module.TOKEN_ENV_VAR in captured.err
    assert captured.out == ""


def test_stdio_is_quiet_without_the_env_var(
    cli: tuple[list[dict[str, Any]], _RecordingServer],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(main_module.TOKEN_ENV_VAR, raising=False)
    main_module.main([])
    assert capsys.readouterr().err == ""
