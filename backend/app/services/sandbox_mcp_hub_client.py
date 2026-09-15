"""Thin REST adapter for the aio-sandbox MCP hub.

Talks to the hub's REST endpoints:
- GET  /v1/mcp/{server_name}/tools          → list available tools
- POST /v1/mcp/{server_name}/tools/{tool}   → call a tool

Both endpoints return a ``{success, message, data}`` envelope.
Auth and base URL mirror ``aio_sandbox_backend.AioSandboxBackend._headers()``
and are sourced from ``settings.SANDBOX_API_URL`` / ``settings.SANDBOX_API_KEY``.

Usage::

    from app.services.sandbox_mcp_hub_client import SandboxMcpHubClient
    from app.config import settings

    client = SandboxMcpHubClient(settings.SANDBOX_API_URL, settings.SANDBOX_API_KEY)
    tools  = await client.list_tools("yunxiao__abc123456789")
    result = await client.call_tool("yunxiao__abc123456789", "get_current_user", {})
"""

import httpx
from app.services.tool_results import normalize_tool_result


class SandboxMcpHubClient:
    """HTTP client for the aio-sandbox MCP hub REST API."""

    def __init__(self, base_url: str, api_key: str | None) -> None:
        self.base = (base_url or "").rstrip("/")
        self.api_key = api_key

    # ------------------------------------------------------------------ Internals

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    # ------------------------------------------------------------------ Public API

    async def list_tools(self, server_name: str, timeout: float = 120.0) -> list[dict]:
        """List tools exposed by *server_name* via the hub.

        Returns a list of ``{name, description, inputSchema}`` dicts.
        Raises on non-200 HTTP status, network errors, or ``success: false`` —
        callers that want a soft error should catch the exception.
        """
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(
                    f"{self.base}/v1/mcp/{server_name}/tools",
                    params={"timeout": timeout},
                    headers=self._headers(),
                    timeout=timeout + 10.0,
                )
        except httpx.HTTPError as e:
            raise Exception(f"MCP hub list_tools network error: {e}") from e

        if r.status_code != 200:
            raise Exception(f"MCP hub list_tools HTTP {r.status_code}: {r.text[:200]}")

        body = r.json()

        if not body.get("success"):
            raise Exception(body.get("message") or "list_tools failed")

        data = body.get("data") or {}
        # Hub may return {data: {tools: [...]}} or {data: [...]} directly.
        if isinstance(data, list):
            raw_tools: list[dict] = data
        else:
            raw_tools = data.get("tools", [])

        return [
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema", {}),
            }
            for t in raw_tools
        ]

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict,
        timeout: float = 120.0,
    ) -> dict:
        """Call *tool_name* on *server_name* via the hub.

        Returns a standard CallToolResult with every content block intact.
        Never raises — all errors (network, HTTP, envelope) are returned as an
        standard error result so the LLM sees the error message rather than a
        Python exception.
        """
        try:
            async with httpx.AsyncClient() as c:
                r = await c.post(
                    f"{self.base}/v1/mcp/{server_name}/tools/{tool_name}",
                    json=arguments or {},
                    params={"timeout": timeout},
                    headers=self._headers(),
                    timeout=timeout + 10.0,
                )

            if r.status_code != 200:
                return normalize_tool_result(f"❌ MCP tool error: HTTP {r.status_code}: {r.text[:200]}", is_error=True)

            body = r.json()
        except httpx.HTTPError as e:
            return normalize_tool_result(f"❌ MCP tool network error: {e}", is_error=True)
        except Exception as e:
            return normalize_tool_result(f"❌ MCP tool error: {e}", is_error=True)

        if not body.get("success"):
            return normalize_tool_result(f"❌ MCP tool error: {body.get('message')}", is_error=True)

        data = body.get("data") or {"content": []}
        if isinstance(data, dict) and "content" not in data and "structuredContent" not in data:
            data = {"content": [], "structuredContent": data}
        return normalize_tool_result(data)
