"""browse() never raises — failures come back as {"success": False, ...}.

No sandbox required: an unreachable api_url forces a real connection error
inside browse(), which must be caught and returned as a failure dict.
"""
from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend


def _unreachable_backend() -> AioSandboxBackend:
    return AioSandboxBackend(
        SandboxConfig(
            type=SandboxType.AIO_SANDBOX,
            api_url="http://127.0.0.1:1",  # nothing listens here -> connection refused
            default_timeout=5,
            max_timeout=5,
        )
    )


async def test_browse_returns_failure_dict_instead_of_raising():
    backend = _unreachable_backend()
    out = await backend.browse(
        agent_id="a", conversation_id="c",
        url="https://example.com", extract=True, screenshot=False, timeout=5,
    )
    assert out["success"] is False
    assert out["error"]  # non-empty error string
    assert out["url"] == "https://example.com"
    assert out["title"] == "" and out["text"] == "" and out["screenshot_b64"] is None
    # no browser context should have been cached for a failed connection
    assert backend._browser_contexts.get("a:c") is None
