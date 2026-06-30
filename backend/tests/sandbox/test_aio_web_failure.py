"""web_* methods honor the never-raise contract and the guard blocks pre-connect."""
from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend


def _backend():
    # Unreachable URL: any attempt to connect fails. Proves never-raise.
    return AioSandboxBackend(SandboxConfig(api_url="http://127.0.0.1:1", api_key=""))


async def test_web_eval_never_raises_on_unreachable_sandbox():
    out = await _backend().web_eval(
        agent_id="a", conversation_id="c", expression="1+1", timeout=3
    )
    assert out["success"] is False
    assert out["result"] is None
    assert "web_eval" in out["error"]


async def test_web_open_never_raises_on_unreachable_sandbox():
    out = await _backend().web_open(
        agent_id="a", conversation_id="c", url="https://example.com", timeout=3
    )
    assert out["success"] is False


async def test_web_screenshot_never_raises_on_unreachable_sandbox():
    out = await _backend().web_screenshot(agent_id="a", conversation_id="c", timeout=3)
    assert out["success"] is False
    assert out["screenshot_b64"] is None


async def test_web_cdp_blocks_browser_global_without_connecting():
    # Target.* must be rejected by the guard BEFORE any network call, so even
    # against an unreachable sandbox this returns the *blocked* error, not a
    # connection error.
    out = await _backend().web_cdp(
        agent_id="a", conversation_id="c", method="Target.getTargets", timeout=3
    )
    assert out["success"] is False
    assert "blocked" in out["error"]
    assert "Target.getTargets" in out["error"]


async def test_web_cdp_rejects_non_dict_params():
    out = await _backend().web_cdp(
        agent_id="a", conversation_id="c", method="Page.navigate", params=["x"], timeout=3
    )
    assert out["success"] is False
