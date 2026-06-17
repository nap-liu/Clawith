"""Tests for _import_mcp_server stdio routing — verifies stdio configs route to
import_mcp_stdio_direct, not to the generic http error path."""
import uuid
import pytest
import app.services.agent_tools as at
from unittest.mock import AsyncMock, patch

pytestmark = pytest.mark.asyncio


async def test_import_mcp_server_routes_stdio():
    """stdio config in mcpServers format routes to import_mcp_stdio_direct."""
    called = {}

    async def fake_stdio(agent_id, parsed):
        called["parsed"] = parsed
        return "✅ stdio installed"

    with patch("app.services.resource_discovery.import_mcp_stdio_direct", fake_stdio):
        out = await at._import_mcp_server(uuid.uuid4(), {
            "mcp_config": {"mcpServers": {"yx": {"command": "npx",
                "args": ["-y", "alibabacloud-devops-mcp-server"], "env": {"T": "x"}}}}})

    assert "stdio installed" in out
    assert called.get("parsed", {}).get("transport") == "stdio"


async def test_import_mcp_server_http_still_works():
    """http URL config still routes through import_mcp_direct (no regression)."""
    async def fake_http(**kw):
        return "✅ http installed"

    with patch("app.services.resource_discovery.import_mcp_direct", fake_http):
        out = await at._import_mcp_server(uuid.uuid4(), {"mcp_url": "https://example.com/mcp"})

    assert "http installed" in out


async def test_import_mcp_server_stdio_no_url_does_not_fall_through():
    """A stdio config must NOT fall through to the 'provide mcp_url' error."""
    async def fake_stdio(agent_id, parsed):
        return "✅ stdio ok"

    with patch("app.services.resource_discovery.import_mcp_stdio_direct", fake_stdio):
        out = await at._import_mcp_server(uuid.uuid4(), {
            "mcp_config": {"command": "npx", "args": ["-y", "some-pkg"]}
        })

    # Should not get the generic "Provide one of: mcp_url..." error
    assert "Provide one of" not in out
    assert "✅" in out
