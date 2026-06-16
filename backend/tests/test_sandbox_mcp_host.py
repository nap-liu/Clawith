"""Tests for SandboxMcpHost and entry_name — Task 6.

All sandbox shell calls are mocked; no real sandbox needed.
"""
import base64
import json
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


async def test_ensure_registered_mkdirs_cwd_on_sandbox(monkeypatch):
    """When a cwd is given, ensure_registered must create it on the sandbox.

    The per-agent cwd (e.g. /data/agents/{agent_id}) must exist before the stdio
    process is spawned there. On deployments where backend and sandbox do not
    share the /data/agents volume mount, the directory would otherwise be missing
    and npx would fail to start. mkdir -p is idempotent (no-op with shared mount).
    """
    calls = []

    async def fake_exec(self, command: str):
        calls.append(command)
        return {"success": True}

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", fake_exec)
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    cwd = "/data/agents/8cc7693d-7bf7-4d0b-a739-238ea32e08bd"
    await host.ensure_registered(
        "yunxiao", "agentA",
        {"command": "npx", "args": ["-y", "pkg"], "env": {}},
        cwd=cwd,
    )
    joined = " ".join(calls)
    assert f"mkdir -p {cwd}" in joined, f"Expected mkdir -p of cwd in: {joined}"
    # mkdir must precede the jq merge (dir exists before the entry references it).
    assert joined.index("mkdir -p") < joined.index("jq")


async def test_ensure_registered_no_mkdir_without_cwd(monkeypatch):
    """Without a cwd, ensure_registered must NOT emit a mkdir (nothing to create)."""
    calls = []

    async def fake_exec(self, command: str):
        calls.append(command)
        return {"success": True}

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", fake_exec)
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    await host.ensure_registered("srv", "agent1", {"command": "npx", "args": [], "env": {}})
    assert "mkdir" not in " ".join(calls)


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


# ---------------------------------------------------------------------------
# FIX 4: deregister — hub entry cleanup
# ---------------------------------------------------------------------------


async def test_deregister_emits_jq_del_command(monkeypatch):
    """deregister must emit a shell command containing jq del(.mcpServers) + flock.

    FAILS before FIX 4 because deregister does not exist."""
    calls = []

    async def fake_exec(self, command: str):
        calls.append(command)
        return {"success": True}

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", fake_exec)
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    await host.deregister("yunxiao__abc123456789")

    assert len(calls) == 1, "deregister should emit exactly one shell command"
    cmd = calls[0]
    assert "jq" in cmd, f"Expected 'jq' in command: {cmd}"
    assert "del(.mcpServers" in cmd, f"Expected 'del(.mcpServers' in command: {cmd}"
    assert "flock" in cmd, f"Expected 'flock' in command: {cmd}"
    assert "mcp-hub.json" in cmd, f"Expected 'mcp-hub.json' in command: {cmd}"
    assert "yunxiao__abc123456789" in cmd, "Entry name must appear in delete command"


async def test_deregister_rejects_unsafe_name(monkeypatch):
    """deregister must raise ValueError for names containing unsafe characters.

    FAILS before FIX 4."""
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    with pytest.raises(ValueError, match="unsafe"):
        await host.deregister('evil"; rm -rf /')


# ---------------------------------------------------------------------------
# Per-agent working-path isolation tests
# ---------------------------------------------------------------------------


def test_entry_name_different_cwd_yields_different_name():
    """Two different cwds must produce different entry names (correct-by-construction)."""
    cfg = {"command": "npx", "args": ["-y", "pkg"], "env": {}}
    n1 = entry_name("srv", "agentA", cfg, cwd="/data/agents/agent-1")
    n2 = entry_name("srv", "agentA", cfg, cwd="/data/agents/agent-2")
    assert n1 != n2


def test_entry_name_no_cwd_matches_empty_cwd():
    """Omitting cwd is the same as cwd=None (backward-compatible fingerprint)."""
    cfg = {"command": "npx", "args": [], "env": {}}
    n_none = entry_name("srv", "agentA", cfg, cwd=None)
    n_default = entry_name("srv", "agentA", cfg)
    assert n_none == n_default


async def test_ensure_registered_includes_cwd_in_entry(monkeypatch):
    """When cwd is provided, the base64-encoded entry JSON must contain the cwd field."""
    captured_b64: list[str] = []

    async def fake_exec(self, command: str):
        # Extract the base64 blob from the shell command (first token after 'echo ')
        for token in command.split():
            try:
                decoded = base64.b64decode(token).decode()
                captured_b64.append(decoded)
            except Exception:
                pass
        return {"success": True}

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", fake_exec)
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    await host.ensure_registered(
        "yunxiao",
        "agent-xyz",
        {"command": "npx", "args": ["-y", "pkg"], "env": {"K": "v"}},
        cwd="/data/agents/agent-xyz",
    )
    assert captured_b64, "No base64 blob found in shell command"
    entry_obj = json.loads(captured_b64[0])
    assert "cwd" in entry_obj, f"Entry JSON missing 'cwd' key: {entry_obj}"
    assert entry_obj["cwd"] == "/data/agents/agent-xyz"


async def test_ensure_registered_no_cwd_key_when_cwd_is_none(monkeypatch):
    """When cwd is None (default), the entry JSON must NOT contain the 'cwd' key."""
    captured_b64: list[str] = []

    async def fake_exec(self, command: str):
        for token in command.split():
            try:
                decoded = base64.b64decode(token).decode()
                captured_b64.append(decoded)
            except Exception:
                pass
        return {"success": True}

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", fake_exec)
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    await host.ensure_registered(
        "yunxiao",
        "agent-xyz",
        {"command": "npx", "args": [], "env": {}},
    )
    assert captured_b64, "No base64 blob found in shell command"
    entry_obj = json.loads(captured_b64[0])
    assert "cwd" not in entry_obj, f"Entry JSON must not contain 'cwd' when None: {entry_obj}"
