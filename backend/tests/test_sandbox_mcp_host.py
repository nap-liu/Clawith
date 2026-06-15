"""Tests for SandboxMcpHost and entry_name — Task 6.

All sandbox shell calls are mocked; no real sandbox needed.
"""
import pytest

from app.services.sandbox_mcp_host import SandboxMcpHost, entry_name

pytestmark = pytest.mark.asyncio


def test_entry_name_deterministic_and_per_agent():
    a = entry_name("yunxiao", "agentA", {"command": "npx", "args": ["-y", "p"], "env": {"T": "1"}})
    b = entry_name("yunxiao", "agentA", {"command": "npx", "args": ["-y", "p"], "env": {"T": "1"}})
    c = entry_name("yunxiao", "agentB", {"command": "npx", "args": ["-y", "p"], "env": {"T": "1"}})
    assert a == b
    assert a != c
    assert a.startswith("yunxiao__")


def test_entry_name_starts_with_server_name():
    name = entry_name("my_server", "agent1", {"command": "uvx", "args": [], "env": {}})
    assert name.startswith("my_server__")
    # fingerprint portion is 12 hex chars
    suffix = name[len("my_server__"):]
    assert len(suffix) == 12
    assert all(c in "0123456789abcdef" for c in suffix)


def test_entry_name_changes_with_cfg():
    cfg1 = {"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "abc"}}
    cfg2 = {"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "xyz"}}
    n1 = entry_name("srv", "agentA", cfg1)
    n2 = entry_name("srv", "agentA", cfg2)
    assert n1 != n2


async def test_ensure_registered_writes_merge(monkeypatch):
    calls = []

    async def fake_exec(self, command: str):
        calls.append(command)
        return {"success": True}

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", fake_exec)
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    name = await host.ensure_registered(
        "yunxiao",
        "agentA",
        {
            "command": "npx",
            "args": ["-y", "alibabacloud-devops-mcp-server"],
            "env": {"YUNXIAO_ACCESS_TOKEN": "tok"},
        },
    )
    assert name.startswith("yunxiao__")
    joined = " ".join(calls)
    assert "base64 -d" in joined
    assert "jq" in joined
    assert "flock" in joined
    assert "mcp-hub.json" in joined
    # Secret must NOT appear plaintext in the shell command
    assert "tok" not in joined


async def test_ensure_registered_returns_entry_name(monkeypatch):
    async def fake_exec(self, command: str):
        return {"success": True}

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", fake_exec)
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    cfg = {"command": "npx", "args": ["-y", "pkg"], "env": {"K": "v"}}
    name = await host.ensure_registered("my_mcp", "agentX", cfg)
    expected = entry_name("my_mcp", "agentX", cfg)
    assert name == expected


async def test_ensure_registered_raises_on_failure(monkeypatch):
    async def fake_exec(self, command: str):
        return {"success": False, "message": "permission denied"}

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", fake_exec)
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    with pytest.raises(Exception, match="hub register failed"):
        await host.ensure_registered("srv", "agent1", {"command": "npx", "args": [], "env": {}})


async def test_ensure_registered_no_restart(monkeypatch):
    """Registration must NOT call any restart/reload endpoint — hot-reload is via property patch."""
    calls = []

    async def fake_exec(self, command: str):
        calls.append(command)
        return {"success": True}

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", fake_exec)
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    await host.ensure_registered("srv", "agent", {"command": "npx", "args": [], "env": {}})
    joined = " ".join(calls)
    # Must not call any reload/restart command
    assert "restart" not in joined
    assert "reload" not in joined
    assert "pkill" not in joined
    assert "kill" not in joined


# ---------------------------------------------------------------------------
# New tests for code-review fixes
# ---------------------------------------------------------------------------


def test_entry_name_rejects_unsafe_server_name():
    """entry_name must raise ValueError for names containing shell-unsafe chars."""
    with pytest.raises(ValueError, match="unsafe characters"):
        entry_name('evil"; rm -rf /; echo', "agentA", {"command": "npx"})


async def test_ensure_registered_raises_on_missing_command(monkeypatch):
    """ensure_registered must raise ValueError if cfg has no 'command' key."""
    async def fake_exec(self, command: str):
        return {"success": True}

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", fake_exec)
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    with pytest.raises(ValueError, match="command"):
        await host.ensure_registered("srv", "agent1", {})


async def test_exec_admin_shell_raises_on_exec_500(monkeypatch):
    """_exec_admin_shell must raise if the exec POST returns HTTP 500."""
    import httpx

    class FakeResp200:
        status_code = 200

        def json(self):
            return {"success": True}

        @property
        def text(self):
            return "ok"

    class FakeResp500:
        status_code = 500

        def json(self):
            return {}

        @property
        def text(self):
            return "Internal Server Error"

    call_count = 0

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers, timeout):
            nonlocal call_count
            call_count += 1
            if "sessions/create" in url:
                return FakeResp200()
            return FakeResp500()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    with pytest.raises(Exception, match="500"):
        await host._exec_admin_shell("echo hello")
