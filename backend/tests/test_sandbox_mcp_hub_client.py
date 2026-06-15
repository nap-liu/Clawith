"""Tests for SandboxMcpHubClient — Task 5.

All HTTP calls are mocked; no real sandbox needed.
"""
import pytest
import httpx

from app.services.sandbox_mcp_hub_client import SandboxMcpHubClient

pytestmark = pytest.mark.asyncio


async def test_call_tool_parses_envelope(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "message": "ok",
                "data": {"content": [{"type": "text", "text": "hi"}]},
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers, timeout):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    c = SandboxMcpHubClient(base_url="http://aio-sandbox:8080", api_key=None)
    out = await c.call_tool("yx__abc", "get_current_user", {})
    assert "hi" in out
    assert captured["url"].endswith("/v1/mcp/yx__abc/tools/get_current_user")
    assert captured["json"] == {}


async def test_call_tool_surfaces_failure(monkeypatch):
    class FakeResp:
        status_code = 200

        def json(self):
            return {"success": False, "message": "boom", "data": None}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    out = await SandboxMcpHubClient("http://x:8080", None).call_tool("n", "t", {})
    assert "boom" in out


async def test_call_tool_with_api_key_sends_auth_header(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"success": True, "message": "ok", "data": {"content": []}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers, timeout):
            captured["headers"] = headers
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    c = SandboxMcpHubClient(base_url="http://aio-sandbox:8080", api_key="secret-key")
    await c.call_tool("srv", "tool", {"arg": "val"})
    assert captured["headers"].get("Authorization") == "Bearer secret-key"


async def test_call_tool_multiple_text_blocks_joined(monkeypatch):
    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "message": "ok",
                "data": {
                    "content": [
                        {"type": "text", "text": "part1"},
                        {"type": "image", "url": "..."},  # non-text block ignored
                        {"type": "text", "text": "part2"},
                    ]
                },
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    out = await SandboxMcpHubClient("http://x:8080", None).call_tool("s", "t", {})
    assert out == "part1\npart2"


async def test_list_tools_returns_normalized(monkeypatch):
    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "message": "ok",
                "data": {
                    "tools": [
                        {
                            "name": "get_user",
                            "description": "Gets a user",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers, timeout):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    tools = await SandboxMcpHubClient("http://x:8080", None).list_tools("my_server")
    assert len(tools) == 1
    assert tools[0]["name"] == "get_user"
    assert tools[0]["description"] == "Gets a user"
    assert "inputSchema" in tools[0]


async def test_list_tools_raises_on_failure(monkeypatch):
    class FakeResp:
        status_code = 200

        def json(self):
            return {"success": False, "message": "server not found", "data": None}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    with pytest.raises(Exception, match="server not found"):
        await SandboxMcpHubClient("http://x:8080", None).list_tools("bad_server")


# ---------------------------------------------------------------------------
# New tests for code-review fixes
# ---------------------------------------------------------------------------


async def test_call_tool_returns_error_string_on_network_error(monkeypatch):
    """call_tool must NOT raise on httpx.ConnectError — return an ❌ string."""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    out = await SandboxMcpHubClient("http://x:8080", None).call_tool("srv", "tool", {})
    assert out.startswith("❌")


async def test_call_tool_returns_error_string_on_502(monkeypatch):
    """call_tool must return an ❌ string with the HTTP status when response is 502."""

    class FakeResp:
        status_code = 502

        def json(self):
            raise ValueError("not JSON")

        @property
        def text(self):
            return "Bad Gateway"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    out = await SandboxMcpHubClient("http://x:8080", None).call_tool("srv", "tool", {})
    assert "❌" in out
    assert "502" in out


async def test_list_tools_raises_on_http_500(monkeypatch):
    """list_tools must raise an Exception containing '500' when response status is 500."""

    class FakeResp:
        status_code = 500

        def json(self):
            raise ValueError("not JSON")

        @property
        def text(self):
            return "Internal Server Error"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    with pytest.raises(Exception, match="500"):
        await SandboxMcpHubClient("http://x:8080", None).list_tools("my_server")
