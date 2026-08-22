"""Tests for lgsf_mcp.server.create_server."""

import httpx
import pytest
from mcp import Client

from lgsf_mcp.client import CouncilGatewayClient
from lgsf_mcp.server import create_server


# Inline stub so this bead does not collide with the shared tests/conftest.py
# being added alongside it; swap for the shared `fixture_client` fixture once
# that lands.
@pytest.fixture
def stub_client() -> CouncilGatewayClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    return CouncilGatewayClient(transport=httpx.MockTransport(handler))


async def test_initialize_and_instructions(stub_client: CouncilGatewayClient) -> None:
    async with Client(create_server(stub_client)) as client:
        assert client.server_info is not None
        assert client.server_info.name == "lgsf-mcp"
        assert client.instructions is not None
        assert "Councils" in client.instructions


async def test_tools_are_registered(stub_client: CouncilGatewayClient) -> None:
    async with Client(create_server(stub_client)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "search_council_data" in names


async def test_injected_client_is_not_closed(
    stub_client: CouncilGatewayClient,
) -> None:
    async with Client(create_server(stub_client)) as client:
        await client.list_tools()
    assert not stub_client.is_closed
