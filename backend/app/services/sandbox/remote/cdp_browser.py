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
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise CdpError(f"{method}: timed out after {timeout}s")
            msg = json.loads(raw)
            if msg.get("id") != msg_id:
                continue  # event, or a different in-flight call
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
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for CDP event {method}")
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            msg = json.loads(raw)
            if msg.get("method") != method:
                continue
            if session_id and msg.get("sessionId") != session_id:
                continue
            return msg.get("params", {})
