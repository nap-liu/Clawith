"""Browser automation methods for the AIO sandbox backend."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from loguru import logger

from app.services.sandbox.base import ExecutionResult
from app.services.sandbox.remote.aio_sandbox_backend import (
    _is_browser_global_method,
    compute_session_anchor,
)
from app.services.sandbox.remote.cdp_browser import (
    CdpConnection,
    CdpError,
    attach_page,
    capture_screenshot,
    eval_js,
    navigate_page,
    open_and_extract,
    page_title,
)


class AioSandboxBrowserMixin:
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.config.api_key:
            h["Authorization"] = f"Bearer {self.config.api_key}"
        return h

    async def _browser_ws_url(self, client: httpx.AsyncClient) -> str:
        """Resolve the browser-level CDP WebSocket URL, reachable from the backend.

        aio-sandbox exposes the bundled Chrome's CDP through its 8080 endpoint.
        `/v1/browser/info` returns a `cdp_url`; `/json/version` returns a
        `webSocketDebuggerUrl`. Either may carry an in-container host
        (localhost/127.0.0.1/0.0.0.0) that the backend cannot reach, so we
        always rewrite the host:port to our own `self.base_url`.
        """
        cdp_url = ""
        try:
            resp = await client.get(
                f"{self.base_url}/v1/browser/info",
                headers=self._headers(),
                timeout=5.0,
            )
            if resp.status_code == 200:
                cdp_url = (resp.json().get("data") or resp.json()).get("cdp_url", "") or ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AioSandbox] /v1/browser/info failed: {e}")
        if not cdp_url:
            resp = await client.get(
                f"{self.base_url}/json/version",
                headers=self._headers(),
                timeout=5.0,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"browser CDP not reachable: /json/version HTTP {resp.status_code}"
                )
            cdp_url = resp.json().get("webSocketDebuggerUrl", "") or ""
        if not cdp_url:
            raise RuntimeError("browser CDP endpoint returned no ws url")
        # Rewrite host:port to the backend-reachable base_url; keep ws path.
        base = urlsplit(self.base_url)
        cdp = urlsplit(cdp_url)
        scheme = "wss" if base.scheme == "https" else "ws"
        return urlunsplit((scheme, base.netloc, cdp.path, cdp.query, ""))

    async def _ensure_browser_context(self, conn, anchor: str, *, timeout: float) -> str:
        """Get-or-create the CDP browserContextId for an anchor."""
        ctx = self._browser_contexts.get(anchor)
        if ctx:
            return ctx
        created = await conn.call(
            "Target.createBrowserContext", {"disposeOnDetach": False}, timeout=timeout
        )
        ctx = created["browserContextId"]
        self._browser_contexts[anchor] = ctx
        return ctx

    async def _ensure_browser_page(self, conn, anchor: str, *, timeout: float) -> tuple[str, str]:
        """Get-or-create the anchor's persistent RPA page; return (targetId, sessionId).

        The target lives in the anchor's browserContext and is never closed, so
        page state survives across calls. sessionId is connection-scoped, so we
        re-attach on every new websocket. A stale targetId (e.g. container
        restart) fails to attach and is transparently recreated.
        """
        ctx = await self._ensure_browser_context(conn, anchor, timeout=timeout)
        target_id = self._browser_pages.get(anchor)
        if target_id:
            try:
                sid = await attach_page(conn, target_id, timeout=timeout)
                return target_id, sid
            except CdpError:
                self._browser_pages.pop(anchor, None)
        created = await conn.call(
            "Target.createTarget",
            {"url": "about:blank", "browserContextId": ctx},
            timeout=timeout,
        )
        target_id = created["targetId"]
        self._browser_pages[anchor] = target_id
        sid = await attach_page(conn, target_id, timeout=timeout)
        return target_id, sid

    @asynccontextmanager
    async def _rpa_page(self, agent_id: str | None, conversation_id: str | None, *, timeout: float):
        """Connect, register/evict the anchor, ensure its persistent page, attach.

        Yields (conn, session_id) routed to the anchor's persistent RPA page.
        Closes the websocket on exit; the page (targetId) and its state survive
        for the next call.
        """
        anchor = compute_session_anchor(agent_id, conversation_id)
        async with httpx.AsyncClient() as client:
            for stale in self._register_anchor(agent_id or "default", anchor):
                await self._evict_anchor(client, stale)
            ws_url = await self._browser_ws_url(client)
            async with websockets.connect(ws_url, max_size=20_000_000) as ws_conn:
                conn = CdpConnection(ws_conn)
                _, sid = await self._ensure_browser_page(conn, anchor, timeout=timeout)
                yield conn, sid

    async def browse(
        self,
        *,
        agent_id: str | None,
        conversation_id: str | None,
        url: str,
        extract: bool = True,
        screenshot: bool = False,
        timeout: int = 30,
    ) -> dict[str, Any]:
        anchor = compute_session_anchor(agent_id, conversation_id)
        try:
            async with httpx.AsyncClient() as client:
                # Register in the same per-agent LRU as execute() so browse-only
                # conversations are bounded and their browser contexts get disposed
                # on eviction (otherwise they leak until process restart).
                for stale in self._register_anchor(agent_id or "default", anchor):
                    await self._evict_anchor(client, stale)
                ws_url = await self._browser_ws_url(client)
                async with websockets.connect(ws_url, max_size=20_000_000) as ws_conn:
                    conn = CdpConnection(ws_conn)
                    ctx = await self._ensure_browser_context(conn, anchor, timeout=float(timeout))
                    out = await open_and_extract(
                        conn,
                        browser_context_id=ctx,
                        url=url,
                        want_text=extract,
                        want_screenshot=screenshot,
                        text_limit=_STDOUT_LIMIT,
                        timeout=float(timeout),
                    )
            return {"success": True, "error": None, **out}
        except Exception as e:  # noqa: BLE001
            logger.exception("[AioSandbox] browse error")
            return {
                "success": False,
                "error": f"browse failed: {str(e)[:200]}",
                "url": url,
                "title": "",
                "text": "",
                "screenshot_b64": None,
                "truncated": False,
            }

    async def web_eval(
        self, *, agent_id: str | None, conversation_id: str | None, expression: str, timeout: int = 30
    ) -> dict[str, Any]:
        """Run arbitrary async JS in the conversation's persistent page."""
        try:
            async with self._rpa_page(agent_id, conversation_id, timeout=float(timeout)) as (conn, sid):
                value = await eval_js(conn, sid, expression, timeout=float(timeout))
            return {"success": True, "error": None, "result": value}
        except Exception as e:  # noqa: BLE001
            logger.exception("[AioSandbox] web_eval error")
            return {"success": False, "error": f"web_eval failed: {str(e)[:300]}", "result": None}

    async def web_cdp(
        self,
        *,
        agent_id: str | None,
        conversation_id: str | None,
        method: str,
        params: dict | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Raw CDP passthrough, scoped to the conversation's own page session.

        Browser-global methods are rejected to preserve per-conversation
        isolation in the shared container (see _is_browser_global_method).
        """
        method = (method or "").strip()
        if _is_browser_global_method(method):
            return {
                "success": False,
                "error": (
                    f"web_cdp: method {method!r} is blocked (browser-global; it would break "
                    f"per-conversation isolation in the shared browser). Use a session-scoped "
                    f"method such as Page.*, DOM.*, Input.*, Network.*, Emulation.* or Fetch.*."
                ),
                "result": None,
            }
        if params is not None and not isinstance(params, dict):
            return {"success": False, "error": "web_cdp: 'params' must be an object.", "result": None}
        try:
            async with self._rpa_page(agent_id, conversation_id, timeout=float(timeout)) as (conn, sid):
                result = await conn.call(method, params or {}, session_id=sid, timeout=float(timeout))
            return {"success": True, "error": None, "result": result}
        except Exception as e:  # noqa: BLE001
            logger.exception("[AioSandbox] web_cdp error")
            return {"success": False, "error": f"web_cdp failed: {str(e)[:300]}", "result": None}

    async def web_open(
        self, *, agent_id: str | None, conversation_id: str | None, url: str, timeout: int = 30
    ) -> dict[str, Any]:
        """Navigate the conversation's persistent page to `url`."""
        try:
            async with self._rpa_page(agent_id, conversation_id, timeout=float(timeout)) as (conn, sid):
                await navigate_page(conn, sid, url, timeout=float(timeout))
                title = await page_title(conn, sid, timeout=float(timeout))
            return {"success": True, "error": None, "url": url, "title": title}
        except Exception as e:  # noqa: BLE001
            logger.exception("[AioSandbox] web_open error")
            return {"success": False, "error": f"web_open failed: {str(e)[:300]}", "url": url, "title": ""}

    async def web_screenshot(
        self, *, agent_id: str | None, conversation_id: str | None, timeout: int = 30
    ) -> dict[str, Any]:
        """Capture a PNG of the conversation's persistent page."""
        try:
            async with self._rpa_page(agent_id, conversation_id, timeout=float(timeout)) as (conn, sid):
                b64 = await capture_screenshot(conn, sid, timeout=float(timeout))
            return {"success": True, "error": None, "screenshot_b64": b64 or None}
        except Exception as e:  # noqa: BLE001
            logger.exception("[AioSandbox] web_screenshot error")
            return {"success": False, "error": f"web_screenshot failed: {str(e)[:300]}", "screenshot_b64": None}

    @staticmethod
    def _is_session_missing(body: dict[str, Any]) -> bool:
        msg = (body.get("message") or "").lower()
        if "session not found" in msg:
            return True
        # v1.0.0.152 quirk: after DELETE (or a kill-from-elsewhere) the
        # session lingers in the list with status="terminated" and exec
        # calls against it return success:true + exit_code:-1 + empty
        # output. Treat that as missing so the recreate path runs and the
        # next command lands on a fresh bash subprocess.
        data = body.get("data") or {}
        if data.get("status") == "terminated":
            return True
        return False

    @staticmethod
    def _error_result(error_msg: str, start: float, exit_code: int = 1) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            stdout="",
            stderr="",
            exit_code=exit_code,
            duration_ms=int((time.time() - start) * 1000),
            error=error_msg,
        )
