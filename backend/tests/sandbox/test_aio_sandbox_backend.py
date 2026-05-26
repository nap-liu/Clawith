"""Integration smoke tests for AioSandboxBackend.

Requires a running aio-sandbox container reachable at $SANDBOX_API_URL.
Tests are skipped when SANDBOX_API_URL is unset to keep CI green.

To run locally:
    export SANDBOX_API_URL=http://localhost:8091
    pytest backend/tests/sandbox/test_aio_sandbox_backend.py -v
"""
import os
import uuid

import httpx
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
    """Fresh UUID per test execution to prevent cross-test session collisions."""
    return f"test-{uuid.uuid4().hex[:8]}"


async def test_health_check_returns_true_for_running_sandbox(backend):
    assert await backend.health_check() is True


async def test_bash_hello_world_with_agent_id(backend, agent_id):
    result = await backend.execute(
        code="echo hello-from-$(whoami)",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert result.success is True
    # Match the prefix only — the sandbox image's default user may change.
    assert "hello-from-" in result.stdout
    assert result.exit_code == 0


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


async def test_shell_cwd_resets_to_work_dir_each_call(backend, agent_id):
    """cwd is statelessly reset to work_dir on every call (matches
    execute_code/subprocess semantics so the LLM has a single mental model)."""
    # Within a single call, `cd` still takes effect for chained commands.
    r1 = await backend.execute(
        code="cd /tmp && pwd",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert "/tmp" in r1.stdout, "cd should work within a single call"

    # Next call: cwd is back to work_dir, NOT carried over from the previous call.
    r2 = await backend.execute(
        code="pwd",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert r2.stdout.strip() == "/data/agents", (
        f"each call must reset cwd to work_dir; got {r2.stdout!r}"
    )


async def test_shell_env_vars_persist_across_calls(backend, agent_id):
    """Exported env vars DO persist across calls — the shell session itself
    is reused, only cwd is reset. Background processes also survive."""
    await backend.execute(
        code="export AIOSB_MARKER=persisted-value",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    r = await backend.execute(
        code='echo "marker=$AIOSB_MARKER"',
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert "marker=persisted-value" in r.stdout, (
        f"env vars should survive across calls; got {r.stdout!r}"
    )


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


async def test_unknown_session_id_auto_recreates(backend, agent_id):
    """If sandbox restarts and our session vanishes, execute() auto-recreates.

    Step 1: first execute() must create a named shell session
            `clawith-{agent_id}` (server-side identity tied to agent).
    Step 2: we manually delete that session via HTTP to simulate sandbox
            restart / GC.
    Step 3: next execute() must transparently recreate the session and
            succeed.

    The pre-DELETE existence check is what makes this test a meaningful
    TDD signal: a stub that doesn't create named sessions will fail at
    step 1, not vacuously pass through step 3.
    """
    await backend.execute(
        code="echo first",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )

    session_id = f"clawith-{agent_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{os.environ['SANDBOX_API_URL']}/v1/shell/sessions",
            timeout=5.0,
        )
        sessions = resp.json().get("data", {}).get("sessions", {})
        assert session_id in sessions, (
            f"Backend must create named session {session_id!r}; "
            f"sandbox only knows: {list(sessions.keys())[:10]}"
        )
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
