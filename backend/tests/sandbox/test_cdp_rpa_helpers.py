"""Unit tests for the reusable CDP page helpers. No sandbox required."""
import json

import pytest

from app.services.sandbox.remote.cdp_browser import (
    CdpConnection,
    CdpError,
    attach_page,
    capture_screenshot,
    eval_js,
    navigate_page,
    page_title,
)


class FakeWs:
    """Scripted websocket: each sent frame gets the next queued reply."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self._replies:
            raise AssertionError("no more scripted replies")
        return json.dumps(self._replies.pop(0))


async def test_attach_page_returns_session_id():
    ws = FakeWs([
        {"id": 1, "result": {"sessionId": "S1"}},  # Target.attachToTarget
        {"id": 2, "result": {}},                    # Page.enable
    ])
    conn = CdpConnection(ws)
    sid = await attach_page(conn, "T1", timeout=5.0)
    assert sid == "S1"
    assert ws.sent[0]["method"] == "Target.attachToTarget"
    assert ws.sent[0]["params"] == {"targetId": "T1", "flatten": True}
    assert ws.sent[1]["method"] == "Page.enable"
    assert ws.sent[1]["sessionId"] == "S1"


async def test_eval_js_returns_value():
    ws = FakeWs([{"id": 1, "result": {"result": {"value": "hello"}}}])
    conn = CdpConnection(ws)
    out = await eval_js(conn, "S1", "document.title", timeout=5.0)
    assert out == "hello"
    frame = ws.sent[0]["params"]
    assert frame["expression"] == "document.title"
    assert frame["returnByValue"] is True
    assert frame["awaitPromise"] is True


async def test_eval_js_raises_on_js_exception():
    ws = FakeWs([{"id": 1, "result": {
        "result": {"type": "object"},
        "exceptionDetails": {"exception": {"description": "ReferenceError: x is not defined"}},
    }}])
    conn = CdpConnection(ws)
    with pytest.raises(CdpError, match="ReferenceError"):
        await eval_js(conn, "S1", "x", timeout=5.0)


async def test_navigate_page_navigates_and_waits_load():
    ws = FakeWs([
        {"id": 1, "result": {"frameId": "F1"}},                 # Page.navigate
        {"method": "Page.loadEventFired", "params": {}},        # event
    ])
    conn = CdpConnection(ws)
    await navigate_page(conn, "S1", "https://e.com", timeout=5.0)
    assert ws.sent[0]["method"] == "Page.navigate"
    assert ws.sent[0]["params"] == {"url": "https://e.com"}


async def test_page_title_and_screenshot():
    ws = FakeWs([
        {"id": 1, "result": {"result": {"value": "T"}}},   # Runtime.evaluate (title)
        {"id": 2, "result": {"data": "iVBORw0KGgo="}},     # Page.captureScreenshot
    ])
    conn = CdpConnection(ws)
    assert await page_title(conn, "S1", timeout=5.0) == "T"
    assert (await capture_screenshot(conn, "S1", timeout=5.0)).startswith("iVBOR")
