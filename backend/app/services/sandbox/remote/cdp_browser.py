"""Minimal CDP (Chrome DevTools Protocol) JSON-RPC over an open websocket.

Reuses the repo's existing websockets-based CDP pattern (see
services/document_conversion/html_to_pdf.py) but adds flatten-mode
`sessionId` routing so a single browser-level connection can drive
isolated targets in separate browser contexts.
"""
import asyncio
import json
from typing import Any

from loguru import logger


class CdpError(Exception):
    """A CDP method returned an error frame."""


class CdpConnection:
    def __init__(self, ws_conn: Any):
        self._ws = ws_conn
        self._next_id = 0
        # Buffer for CDP events received while waiting for a call reply.
        self._event_buf: list[dict] = []

    async def _recv_msg(self, timeout: float) -> dict:
        """Receive one raw frame from the websocket and parse it."""
        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        return json.loads(raw)

    async def call(
        self,
        method: str,
        params: dict | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 15.0,
    ) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        frame: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            frame["sessionId"] = session_id
        await self._ws.send(json.dumps(frame))
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise CdpError(f"{method}: timed out after {timeout}s")
            try:
                msg = await self._recv_msg(remaining)
            except asyncio.TimeoutError:
                raise CdpError(f"{method}: timed out after {timeout}s")
            if msg.get("id") != msg_id:
                # An event or a different in-flight call — buffer it.
                if "method" in msg:
                    self._event_buf.append(msg)
                continue
            if "error" in msg:
                raise CdpError(f"{method}: {msg['error'].get('message', msg['error'])}")
            return msg.get("result", {})

    async def wait_for_event(
        self,
        method: str,
        *,
        session_id: str | None = None,
        timeout: float = 15.0,
    ) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        def _matches(msg: dict) -> bool:
            if msg.get("method") != method:
                return False
            # Only reject if the event carries an explicit sessionId that differs.
            event_sid = msg.get("sessionId")
            if session_id and event_sid and event_sid != session_id:
                return False
            return True

        # Check buffered events first.
        for i, msg in enumerate(self._event_buf):
            if _matches(msg):
                self._event_buf.pop(i)
                return msg.get("params", {})
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for CDP event {method}")
            msg = await self._recv_msg(remaining)
            if _matches(msg):
                return msg.get("params", {})
            # Buffer events for other methods so call() can still find its reply.
            if "method" in msg:
                self._event_buf.append(msg)


async def open_and_extract(
    conn: "CdpConnection",
    *,
    browser_context_id: str,
    url: str,
    want_text: bool,
    want_screenshot: bool,
    text_limit: int,
    timeout: float,
) -> dict:
    """Open `url` in an isolated target inside `browser_context_id` and extract."""
    target = await conn.call(
        "Target.createTarget",
        {"url": "about:blank", "browserContextId": browser_context_id},
        timeout=timeout,
    )
    target_id = target["targetId"]
    title = ""
    text = ""
    screenshot_b64: str | None = None
    truncated = False
    try:
        attached = await conn.call(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
            timeout=timeout,
        )
        sid = attached["sessionId"]
        await conn.call("Page.enable", session_id=sid, timeout=timeout)
        await conn.call("Page.navigate", {"url": url}, session_id=sid, timeout=timeout)
        try:
            await conn.wait_for_event("Page.loadEventFired", session_id=sid, timeout=timeout)
        except TimeoutError:
            logger.warning(f"[CDP] load event timeout for {url}; extracting anyway")
        if want_text:
            t = await conn.call(
                "Runtime.evaluate",
                {"expression": "document.title", "returnByValue": True},
                session_id=sid, timeout=timeout,
            )
            title = (t.get("result") or {}).get("value") or ""
            b = await conn.call(
                "Runtime.evaluate",
                {"expression": "document.body ? document.body.innerText : ''",
                 "returnByValue": True},
                session_id=sid, timeout=timeout,
            )
            raw_text = (b.get("result") or {}).get("value") or ""
            if len(raw_text) > text_limit:
                text = raw_text[:text_limit]
                truncated = True
            else:
                text = raw_text
        if want_screenshot:
            shot = await conn.call(
                "Page.captureScreenshot", {"format": "png"},
                session_id=sid, timeout=timeout,
            )
            screenshot_b64 = shot.get("data")
    finally:
        try:
            await conn.call("Target.closeTarget", {"targetId": target_id}, timeout=5.0)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[CDP] closeTarget {target_id} failed: {e}")
    return {
        "url": url,
        "title": title,
        "text": text,
        "screenshot_b64": screenshot_b64,
        "truncated": truncated,
    }
