"""MCP registration against an isolated hub file, plus identity and API errors."""

import asyncio
import json
import shutil
import uuid

import pytest

from app.services import sandbox_mcp_host
from app.services.sandbox_mcp_host import SandboxMcpHost, entry_name

def test_entry_name_deterministic_and_per_agent():
    a = entry_name("server", "agentA", {"command": "npx", "args": ["-y", "p"], "env": {"T": "1"}})
    b = entry_name("server", "agentA", {"command": "npx", "args": ["-y", "p"], "env": {"T": "1"}})
    c = entry_name("server", "agentB", {"command": "npx", "args": ["-y", "p"], "env": {"T": "1"}})
    assert a == b
    assert a != c


def test_entry_name_changes_with_cfg():
    cfg1 = {"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "abc"}}
    cfg2 = {"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "xyz"}}
    n1 = entry_name("srv", "agentA", cfg1)
    n2 = entry_name("srv", "agentA", cfg2)
    assert n1 != n2


@pytest.mark.asyncio
async def test_ensure_registered_raises_on_failure(monkeypatch):
    async def fake_exec(self, command: str):
        return {"success": False, "message": "permission denied"}

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", fake_exec)
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    with pytest.raises(Exception, match="hub register failed"):
        await host.ensure_registered("srv", "agent1", {"command": "npx", "args": [], "env": {}})


def test_entry_name_rejects_unsafe_server_name():
    """entry_name must raise ValueError for names containing shell-unsafe chars."""
    with pytest.raises(ValueError, match="unsafe characters"):
        entry_name('evil"; rm -rf /; echo', "agentA", {"command": "npx"})


@pytest.mark.asyncio
async def test_ensure_registered_raises_on_missing_command(monkeypatch):
    """ensure_registered must raise ValueError if cfg has no 'command' key."""
    async def fake_exec(self, command: str):
        return {"success": True}

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", fake_exec)
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    with pytest.raises(ValueError, match="command"):
        await host.ensure_registered("srv", "agent1", {})


@pytest.mark.asyncio
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

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers, timeout):
            if "sessions/create" in url:
                return FakeResp200()
            return FakeResp500()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    with pytest.raises(Exception, match="500"):
        await host._exec_admin_shell("echo hello")


@pytest.mark.asyncio
async def test_deregister_rejects_unsafe_name():
    """Unsafe names must be rejected before any shell execution."""
    host = SandboxMcpHost(base_url="http://x:8080", api_key=None)
    with pytest.raises(ValueError, match="unsafe"):
        await host.deregister('evil"; rm -rf /')


def test_entry_name_different_cwd_yields_different_name():
    """Two different cwds must produce different entry names (correct-by-construction)."""
    cfg = {"command": "npx", "args": ["-y", "pkg"], "env": {}}
    n1 = entry_name("srv", "agentA", cfg, cwd="/data/agents/agent-1")
    n2 = entry_name("srv", "agentA", cfg, cwd="/data/agents/agent-2")
    assert n1 != n2


@pytest.fixture
def local_hub(monkeypatch, tmp_path):
    for executable in ("jq", "flock"):
        if not shutil.which(executable):
            pytest.skip(f"Real MCP hub shell tests require {executable} in the Docker image")
    hub_path = tmp_path / "mcp-hub.json"
    initial = {"mcpServers": {"existing": {"command": "existing-server"}}, "version": 1}
    hub_path.write_text(json.dumps(initial))
    monkeypatch.setattr(sandbox_mcp_host, "_HUB_JSON", str(hub_path))
    monkeypatch.setattr(sandbox_mcp_host, "_LOCK_FILE", str(tmp_path / "hub.lock"))
    commands = []

    async def exec_shell(self, command):
        commands.append(command)
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        return {
            "success": True,
            "data": {"exit_code": process.returncode, "output": output.decode()},
        }

    monkeypatch.setattr(SandboxMcpHost, "_exec_admin_shell", exec_shell)
    host = SandboxMcpHost(base_url="http://unused.invalid", api_key=None)
    return host, hub_path, initial, commands


@pytest.mark.parametrize("with_cwd", [False, True])
@pytest.mark.asyncio
async def test_registration_persists_config_and_preserves_other_entries(local_hub, tmp_path, with_cwd):
    host, hub_path, initial, commands = local_hub
    cwd = tmp_path / "agent workspace's files" if with_cwd else None
    cfg = {
        "command": "npx",
        "args": ["-y", "example-mcp-server"],
        "env": {"ACCESS_TOKEN": "secret-$'quoted; token"},
    }
    invocation = uuid.uuid4().hex
    name = await host.ensure_registered(
        "server", "agent", cfg, cwd=str(cwd) if cwd else None, invocation_id=invocation
    )
    assert name == entry_name(
        "server", "agent", cfg, cwd=str(cwd) if cwd else None, invocation_id=invocation
    )
    expected_entry = {"type": "stdio", **cfg}
    if cwd:
        assert cwd.is_dir()
        expected_entry["cwd"] = str(cwd)
    expected = {**initial, "mcpServers": {**initial["mcpServers"], name: expected_entry}}
    assert json.loads(hub_path.read_text()) == expected
    # The provider command must not carry plaintext credentials.
    assert all(cfg["env"]["ACCESS_TOKEN"] not in command for command in commands)
    assert await host.ensure_registered(
        "server", "agent", cfg, cwd=str(cwd) if cwd else None, invocation_id=invocation
    ) == name
    assert json.loads(hub_path.read_text()) == expected


@pytest.mark.asyncio
async def test_concurrent_invocations_and_cleanup_preserve_other_entries(local_hub):
    host, hub_path, initial, _ = local_hub
    cfg = {"command": "npx", "args": [], "env": {}}
    names = await asyncio.gather(*(
        host.ensure_registered("server", "agent", cfg, invocation_id=uuid.uuid4().hex)
        for _ in range(6)
    ))
    assert len(set(names)) == len(names)
    entries = {name: {"type": "stdio", **cfg} for name in names}
    assert json.loads(hub_path.read_text()) == {
        **initial, "mcpServers": {**initial["mcpServers"], **entries}
    }
    await asyncio.gather(*(host.deregister(name) for name in names[:-1]))
    expected = {**initial, "mcpServers": {**initial["mcpServers"], names[-1]: entries[names[-1]]}}
    assert json.loads(hub_path.read_text()) == expected
    # Repeated cleanup of an expired invocation must not remove the live one.
    await host.deregister(names[0])
    assert json.loads(hub_path.read_text()) == expected
    await host.deregister(names[-1])
    assert json.loads(hub_path.read_text()) == initial


@pytest.mark.asyncio
async def test_registration_surfaces_failed_workspace_creation(local_hub, tmp_path):
    host, hub_path, initial, _ = local_hub
    blocked_cwd = tmp_path / "file-instead-of-directory"
    blocked_cwd.write_text("existing file")
    with pytest.raises(Exception, match="hub register failed.*exit_code=1"):
        await host.ensure_registered("server", "agent", {"command": "npx"}, cwd=str(blocked_cwd))
    assert blocked_cwd.read_text() == "existing file"
    assert json.loads(hub_path.read_text()) == initial
