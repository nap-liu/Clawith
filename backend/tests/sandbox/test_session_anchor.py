"""Unit tests for the session-anchor computation.

Pure function — no sandbox required. The anchor is the key that isolates
shell/jupyter sessions inside aio-sandbox. Historically it was per-agent
(`anchor = agent_id`); these tests pin the per-session semantics:
conversation-scoped isolation with a safe agent-level fallback.
"""
from unittest.mock import AsyncMock

from app.services.sandbox.base import ExecutionResult
from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.sandbox.remote.aio_sandbox_backend import (
    AioSandboxBackend,
    compute_session_anchor,
)

_OK = ExecutionResult(
    success=True, stdout="", stderr="", exit_code=0, duration_ms=0, error=None
)


def test_anchor_combines_agent_and_conversation():
    assert compute_session_anchor("agentA", "conv1") == "agentA:conv1"


def test_anchor_falls_back_to_agent_when_no_conversation():
    assert compute_session_anchor("agentA", None) == "agentA"


def test_anchor_defaults_when_nothing():
    assert compute_session_anchor(None, None) == "default"


def test_anchor_conversation_without_agent_uses_default_agent_part():
    assert compute_session_anchor(None, "conv1") == "default:conv1"


def test_same_agent_different_conversations_are_isolated():
    assert compute_session_anchor("agentA", "conv1") != compute_session_anchor("agentA", "conv2")


def test_different_agents_same_conversation_are_isolated():
    assert compute_session_anchor("agentA", "conv1") != compute_session_anchor("agentB", "conv1")


def test_per_conversation_differs_from_agent_fallback():
    # An agent-level (no conversation) session must not collide with a
    # conversation-scoped one for the same agent — otherwise a triggered
    # run (no conversation_id) and a web chat would share a session.
    assert compute_session_anchor("agentA", None) != compute_session_anchor("agentA", "conv1")


# --------------------------------------------------------------- execute() threading


def _backend() -> AioSandboxBackend:
    cfg = SandboxConfig(
        type=SandboxType.AIO_SANDBOX,
        api_url="http://fake:8080",
        default_timeout=30,
        max_timeout=60,
    )
    return AioSandboxBackend(cfg)


async def test_execute_threads_conversation_id_into_shell_anchor():
    backend = _backend()
    backend._run_shell = AsyncMock(return_value=_OK)
    await backend.execute(
        code="echo hi", language="bash", agent_id="agentA", conversation_id="conv1"
    )
    assert backend._run_shell.call_args.kwargs["anchor"] == "agentA:conv1"


async def test_execute_threads_conversation_id_into_jupyter_anchor():
    backend = _backend()
    backend._run_jupyter = AsyncMock(return_value=_OK)
    await backend.execute(
        code="print(1)", language="python", agent_id="agentA", conversation_id="conv1"
    )
    assert backend._run_jupyter.call_args.kwargs["anchor"] == "agentA:conv1"


async def test_execute_without_conversation_keeps_agent_anchor():
    backend = _backend()
    backend._run_shell = AsyncMock(return_value=_OK)
    await backend.execute(code="echo hi", language="bash", agent_id="agentA")
    assert backend._run_shell.call_args.kwargs["anchor"] == "agentA"
