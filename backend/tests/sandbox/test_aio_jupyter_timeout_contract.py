from unittest.mock import AsyncMock

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


async def test_jupyter_hard_timeout_is_exit_124_with_actionable_error():
    backend = _backend()
    backend._ensure_jupyter_session = AsyncMock(return_value="kernel-session")
    backend._jupyter_exec = AsyncMock(
        return_value=(
            {
                "success": False,
                "message": "Code execution timeout",
                "data": {
                    "session_id": "kernel-session",
                    "status": "timeout",
                    "outputs": [
                        {
                            "output_type": "error",
                            "ename": "HardTimeout",
                            "evalue": "hard_timeout after 3s; kernel interrupted",
                            "traceback": [],
                        }
                    ],
                },
            },
            False,
        )
    )

    result = await backend._run_jupyter(
        AsyncMock(),
        anchor="agent:conversation",
        code="while True: pass",
        cwd="/data/agents/agent",
        timeout=3,
    )

    assert result.success is False
    assert result.exit_code == 124
    assert "hard_timeout after 3s" in (result.error or "")


async def test_jupyter_non_timeout_error_keeps_exit_1():
    backend = _backend()
    backend._ensure_jupyter_session = AsyncMock(return_value="kernel-session")
    backend._jupyter_exec = AsyncMock(
        return_value=(
            {
                "success": False,
                "data": {
                    "session_id": "kernel-session",
                    "status": "error",
                    "outputs": [
                        {
                            "output_type": "error",
                            "ename": "ValueError",
                            "evalue": "bad input",
                            "traceback": [],
                        }
                    ],
                },
            },
            False,
        )
    )

    result = await backend._run_jupyter(
        AsyncMock(),
        anchor="agent:conversation",
        code="raise ValueError('bad input')",
        cwd="/data/agents/agent",
        timeout=3,
    )

    assert result.success is False
    assert result.exit_code == 1
    assert "bad input" in (result.error or "")
