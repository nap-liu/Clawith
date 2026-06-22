"""Unit tests for the CDP JSON-RPC connection wrapper. No sandbox required."""
import asyncio
import json

import pytest

from app.services.sandbox.remote.cdp_browser import CdpConnection, CdpError


class FakeWs:
    """Scripted websocket: each sent frame gets the next queued reply."""

    def __init__(self, replies: list[dict]):
        self._replies = list(replies)
        self.sent: list[dict] = []

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if not self._replies:
            raise AssertionError("no more scripted replies")
        return json.dumps(self._replies.pop(0))


async def test_call_returns_result_for_matching_id():
    ws = FakeWs([{"id": 1, "result": {"product": "Chrome/146"}}])
    conn = CdpConnection(ws)
    result = await conn.call("Browser.getVersion")
    assert result == {"product": "Chrome/146"}
    assert ws.sent[0]["method"] == "Browser.getVersion"
    assert ws.sent[0]["id"] == 1


async def test_call_includes_session_id_when_given():
    ws = FakeWs([{"id": 1, "sessionId": "S1", "result": {}}])
    conn = CdpConnection(ws)
    await conn.call("Page.navigate", {"url": "about:blank"}, session_id="S1")
    assert ws.sent[0]["sessionId"] == "S1"


async def test_call_skips_unrelated_events_and_ids():
    ws = FakeWs([
        {"method": "Page.frameStartedLoading", "params": {}},  # event, no id
        {"id": 99, "result": {"ignored": True}},               # stale id
        {"id": 1, "result": {"ok": True}},                      # ours
    ])
    conn = CdpConnection(ws)
    assert await conn.call("Page.enable") == {"ok": True}


async def test_call_raises_on_error_response():
    ws = FakeWs([{"id": 1, "error": {"message": "boom"}}])
    conn = CdpConnection(ws)
    with pytest.raises(CdpError, match="boom"):
        await conn.call("Bad.method")


async def test_wait_for_event_returns_params():
    ws = FakeWs([
        {"id": 5, "result": {}},  # some unrelated response
        {"method": "Page.loadEventFired", "params": {"timestamp": 1.0}},
    ])
    conn = CdpConnection(ws)
    params = await conn.wait_for_event("Page.loadEventFired", timeout=2.0)
    assert params == {"timestamp": 1.0}
