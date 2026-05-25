"""Integration smoke tests for AioSandboxBackend.

Requires a running aio-sandbox container reachable at $SANDBOX_API_URL.
Tests are skipped when SANDBOX_API_URL is unset to keep CI green.

To run locally:
    export SANDBOX_API_URL=http://localhost:8091
    pytest backend/tests/sandbox/test_aio_sandbox_backend.py -v
"""
import os
import uuid

import pytest

from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend

pytestmark = pytest.mark.skipif(
    not os.environ.get("SANDBOX_API_URL"),
    reason="SANDBOX_API_URL not set; integration tests skipped",
)


@pytest.fixture
def backend() -> AioSandboxBackend:
    cfg = SandboxConfig(
        type=SandboxType.AIO_SANDBOX,
        api_url=os.environ["SANDBOX_API_URL"],
        default_timeout=30,
        max_timeout=60,
    )
    return AioSandboxBackend(cfg)


@pytest.fixture
def agent_id() -> str:
    """Stable per-test agent id so re-runs don't pile up sessions."""
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_health_check_returns_true_for_running_sandbox(backend):
    assert await backend.health_check() is True


@pytest.mark.asyncio
async def test_bash_hello_world_with_agent_id(backend, agent_id):
    result = await backend.execute(
        code="echo hello-from-$(whoami)",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert result.success is True
    assert "hello-from-gem" in result.stdout
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_python_uses_jupyter_session_state(backend, agent_id):
    """Variables set in one call persist to the next via session_id."""
    r1 = await backend.execute(
        code="x = 42",
        language="python",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert r1.success is True

    r2 = await backend.execute(
        code="print(x * 2)",
        language="python",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert r2.success is True
    assert "84" in r2.stdout


@pytest.mark.asyncio
async def test_shell_session_preserves_cwd_across_calls(backend, agent_id):
    """exec_dir routes the session, and `cd` persists within the session."""
    r1 = await backend.execute(
        code="cd /tmp && pwd",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert "/tmp" in r1.stdout

    r2 = await backend.execute(
        code="pwd",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert "/tmp" in r2.stdout


@pytest.mark.asyncio
async def test_two_agents_have_isolated_shell_sessions(backend):
    a1 = f"agentA-{uuid.uuid4().hex[:8]}"
    a2 = f"agentB-{uuid.uuid4().hex[:8]}"

    await backend.execute(
        code="export MARKER=from-A",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=a1,
    )
    r = await backend.execute(
        code='echo "marker=$MARKER"',
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=a2,
    )
    assert "marker=" in r.stdout
    assert "from-A" not in r.stdout  # B must NOT see A's env


@pytest.mark.asyncio
async def test_unknown_session_id_auto_recreates(backend, agent_id):
    """If sandbox restarts and our session vanishes, execute() auto-recreates."""
    await backend.execute(
        code="echo first",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )

    import httpx
    session_id = f"clawith-{agent_id}"
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{os.environ['SANDBOX_API_URL']}/v1/shell/sessions/{session_id}",
            timeout=5.0,
        )

    r = await backend.execute(
        code="echo second",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert r.success is True
    assert "second" in r.stdout
