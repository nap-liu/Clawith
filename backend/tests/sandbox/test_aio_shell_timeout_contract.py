import json
from unittest.mock import AsyncMock

import httpx

from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend


def _backend() -> AioSandboxBackend:
    return AioSandboxBackend(
        SandboxConfig(
            type=SandboxType.AIO_SANDBOX,
            api_url="http://sandbox.test",
            default_timeout=30,
            max_timeout=60,
        )
    )


async def test_shell_exec_keeps_single_existing_timeout_contract():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "status": "completed",
                    "output": "ok",
                    "exit_code": 0,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, ok = await _backend()._shell_exec(client, "session", "echo ok", 3)

    assert ok is True
    assert body["data"]["status"] == "completed"
    assert captured == {
        "id": "session",
        "command": "echo ok",
        "timeout": 3.0,
    }


async def test_hard_timeout_is_exit_124_without_deleting_stateful_session():
    backend = _backend()
    backend._shell_exec = AsyncMock(
        return_value=(
            {
                "success": True,
                "message": "Command executed",
                "data": {
                    "status": "hard_timeout",
                    "output": "partial output",
                    "exit_code": -1,
                },
            },
            True,
        )
    )
    client = AsyncMock()

    result = await backend._run_shell(
        client,
        anchor="agent:conversation",
        code="sleep 60",
        language="bash",
        cwd="/data/agents/agent",
        timeout=3,
    )

    assert result.success is False
    assert result.exit_code == 124
    assert result.stdout == "partial output"
    assert "timed out after 3s" in (result.error or "")
    client.delete.assert_not_awaited()


async def test_running_busy_fallback_preserves_stateful_session():
    backend = _backend()
    backend._shell_exec = AsyncMock(
        return_value=(
            {
                "success": True,
                "message": "Command still running",
                "data": {
                    "status": "running",
                    "output": "",
                    "exit_code": None,
                },
            },
            True,
        )
    )
    backend._create_shell_session = AsyncMock()
    client = AsyncMock()

    result = await backend._run_shell(
        client,
        anchor="agent:conversation",
        code="sleep 60",
        language="bash",
        cwd="/data/agents/agent",
        timeout=3,
    )

    assert result.success is False
    assert result.exit_code == 124
    assert "SESSION_BUSY" in (result.error or "")
    client.delete.assert_not_awaited()
    backend._create_shell_session.assert_not_awaited()
