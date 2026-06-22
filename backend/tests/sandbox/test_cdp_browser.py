"""Unit tests for the CDP JSON-RPC connection wrapper. No sandbox required."""
import json

import pytest

from app.services.sandbox.remote.cdp_browser import CdpConnection, CdpError, open_and_extract


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


class ScriptedWs:
    """Replies keyed by request method, plus injected events between calls."""

    def __init__(self):
        self.sent = []
        # queued (kind, payload): kind 'event' frames are emitted on recv
        # until the next response is needed.
        self._inbox: list[dict] = []
        self._by_method = {
            "Target.createTarget": {"targetId": "T1"},
            "Target.attachToTarget": {"sessionId": "S1"},
            "Page.enable": {},
            "Page.navigate": {"frameId": "F1"},
            "Runtime.evaluate": None,  # filled per-call below
            "Page.captureScreenshot": {"data": "iVBORw0KGgoAAAANS=="},
            "Target.closeTarget": {"success": True},
        }
        self._eval_results = [
            {"result": {"value": "Example Title"}},      # document.title
            {"result": {"value": "Hello body text"}},    # innerText
        ]

    async def send(self, raw: str):
        import json as _j
        msg = _j.loads(raw)
        self.sent.append(msg)
        method = msg["method"]
        if method == "Page.navigate":
            # queue the load event to be delivered before the next call's reply
            self._inbox.append({"method": "Page.loadEventFired", "params": {}})
        if method == "Runtime.evaluate":
            payload = self._eval_results.pop(0)
        else:
            payload = self._by_method[method]
        reply = {"id": msg["id"], "result": payload}
        if msg.get("sessionId"):
            reply["sessionId"] = msg["sessionId"]
        self._inbox.append(reply)

    async def recv(self) -> str:
        import json as _j
        return _j.dumps(self._inbox.pop(0))


async def test_open_and_extract_returns_title_text_and_screenshot():
    conn = CdpConnection(ScriptedWs())
    out = await open_and_extract(
        conn,
        browser_context_id="CTX1",
        url="https://example.com",
        want_text=True,
        want_screenshot=True,
        text_limit=1000,
        timeout=5.0,
    )
    assert out["title"] == "Example Title"
    assert out["text"] == "Hello body text"
    assert out["screenshot_b64"].startswith("iVBOR")
    assert out["url"] == "https://example.com"
    assert out["truncated"] is False


async def test_open_and_extract_truncates_text():
    ws = ScriptedWs()
    ws._eval_results = [
        {"result": {"value": "T"}},
        {"result": {"value": "X" * 50}},
    ]
    conn = CdpConnection(ws)
    out = await open_and_extract(
        conn, browser_context_id="C", url="https://e.com",
        want_text=True, want_screenshot=False, text_limit=10, timeout=5.0,
    )
    assert out["text"] == "X" * 10
    assert out["truncated"] is True
    assert out["screenshot_b64"] is None
