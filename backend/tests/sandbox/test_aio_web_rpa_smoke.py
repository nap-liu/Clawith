"""Live aio-sandbox integration for the persistent-page RPA tools.

Requires a reachable sandbox with the bundled Chrome. Set:
    SANDBOX_API_URL=http://aio-sandbox:8080
Run on network clawith_default so the host:port resolves.
"""
import os

import pytest

from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend

_URL = os.environ.get("SANDBOX_API_URL")
pytestmark = pytest.mark.skipif(not _URL, reason="needs live SANDBOX_API_URL")


def _backend():
    return AioSandboxBackend(SandboxConfig(api_url=_URL, api_key=os.environ.get("SANDBOX_API_KEY", "")))


async def test_page_state_survives_across_calls():
    b = _backend()
    opened = await b.web_open(agent_id="a", conversation_id="rpa-1", url="https://example.com", timeout=30)
    assert opened["success"], opened
    # A SEPARATE tool call (new websocket) must see the SAME page — the title
    # is readable only if the persistent target survived between calls.
    out = await b.web_eval(agent_id="a", conversation_id="rpa-1", expression="document.title", timeout=30)
    assert out["success"], out
    assert "Example" in (out["result"] or "")


async def test_web_eval_can_set_and_read_dom_state():
    b = _backend()
    await b.web_open(agent_id="a", conversation_id="rpa-2", url="https://example.com", timeout=30)
    await b.web_eval(agent_id="a", conversation_id="rpa-2", expression="window.__clawith = 42", timeout=30)
    # Persisted JS global is still there on the next, separate call.
    out = await b.web_eval(agent_id="a", conversation_id="rpa-2", expression="window.__clawith", timeout=30)
    assert out["result"] == 42


async def test_conversations_are_isolated():
    b = _backend()
    await b.web_open(agent_id="a", conversation_id="iso-A", url="https://example.com", timeout=30)
    set_a = await b.web_eval(agent_id="a", conversation_id="iso-A", expression="window.__secret = 'A'", timeout=30)
    assert set_a["success"], set_a
    # A different conversation has its own page/context — it must NOT see __secret.
    await b.web_open(agent_id="a", conversation_id="iso-B", url="https://example.com", timeout=30)
    out = await b.web_eval(agent_id="a", conversation_id="iso-B", expression="window.__secret || 'none'", timeout=30)
    assert out["result"] == "none"


async def test_web_cdp_trusted_input_runs():
    b = _backend()
    await b.web_open(agent_id="a", conversation_id="cdp-1", url="https://example.com", timeout=30)
    # Raw CDP Input.dispatchMouseEvent — OS-trusted event, the strong-RPA path.
    out = await b.web_cdp(
        agent_id="a",
        conversation_id="cdp-1",
        method="Input.dispatchMouseEvent",
        params={"type": "mouseMoved", "x": 10, "y": 10},
        timeout=30,
    )
    assert out["success"], out


async def test_web_cdp_target_method_is_blocked_live():
    # NOTE: the guard is client-side — web_cdp returns "blocked" BEFORE any
    # sandbox/websocket call, so this asserts the guard, not a live CDP path.
    b = _backend()
    out = await b.web_cdp(agent_id="a", conversation_id="cdp-2", method="Target.getTargets", timeout=30)
    assert out["success"] is False
    assert "blocked" in out["error"]
