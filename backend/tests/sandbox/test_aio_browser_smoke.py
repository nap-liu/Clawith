"""Integration smoke for backend→shared-container CDP reachability.

Requires a running aio-sandbox container reachable at $SANDBOX_API_URL with
its bundled browser enabled (DISABLE_BROWSER=false, the prod default).
Skipped when SANDBOX_API_URL is unset.

To run locally against a sandbox on :8091:
    export SANDBOX_API_URL=http://localhost:8091
    pytest backend/tests/sandbox/test_aio_browser_smoke.py -v
"""
import json
import os

import httpx
import pytest
import websockets

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


async def test_browser_ws_url_round_trips_get_version(backend):
    async with httpx.AsyncClient() as client:
        ws_url = await backend._browser_ws_url(client)
    assert ws_url.startswith("ws://") or ws_url.startswith("wss://")
    async with websockets.connect(ws_url, max_size=20_000_000) as conn:
        await conn.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
        while True:
            msg = json.loads(await conn.recv())
            if msg.get("id") == 1:
                break
    assert "result" in msg
    assert "product" in msg["result"]  # e.g. "Chrome/146.0.0.0"


async def test_browse_extracts_text_from_example_com(backend):
    out = await backend.browse(
        agent_id="smoke-agent",
        conversation_id="conv-browse-1",
        url="https://example.com",
        extract=True,
        screenshot=False,
        timeout=30,
    )
    assert out["success"] is True, out.get("error")
    assert "example" in (out["title"] + out["text"]).lower()
    # context is cached for the anchor
    assert backend._browser_contexts.get("smoke-agent:conv-browse-1")
