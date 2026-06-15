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


class SandboxMcpHubClient:
    """HTTP client for the aio-sandbox MCP hub REST API."""

    def __init__(self, base_url: str, api_key: str | None) -> None:
        self.base = (base_url or "").rstrip("/")
        self.api_key = api_key

    # ------------------------------------------------------------------ Internals

    def _h(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    # ------------------------------------------------------------------ Public API

    async def list_tools(self, server_name: str) -> list[dict]:
        """List tools exposed by *server_name* via the hub.

        Returns a list of ``{name, description, inputSchema}`` dicts.
        Raises on ``success: false`` — callers that want a soft error should
        catch the exception.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{self.base}/v1/mcp/{server_name}/tools",
                headers=self._h(),
                timeout=120.0,
            )
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

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        """Call *tool_name* on *server_name* via the hub.

        Returns the concatenated text content on success.
        On ``success: false`` returns an error string (does *not* raise) so the
        LLM sees the error message rather than a Python exception.
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self.base}/v1/mcp/{server_name}/tools/{tool_name}",
                json=arguments or {},
                headers=self._h(),
                timeout=120.0,
            )
            body = r.json()

        if not body.get("success"):
            return f"❌ MCP tool error: {body.get('message')}"

        data = body.get("data") or {}
        blocks: list[dict] = data.get("content", []) if isinstance(data, dict) else []
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]

        if texts:
            return "\n".join(texts)
        # Fallback: data as string if no text blocks
        return data if isinstance(data, str) else str(data)
