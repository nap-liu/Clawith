"""Registry search adapters used by resource discovery."""

import uuid

import httpx
from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.tool import AgentTool, Tool
from app.services.tool_config import decrypt_sensitive_fields, get_tenant_tool_config


# ── Smithery Registry Search ────────────────────────────────────

SMITHERY_API_BASE = "https://registry.smithery.ai"
MODELSCOPE_API_BASE = "https://modelscope.cn"


async def _get_smithery_api_key(agent_id: uuid.UUID | None = None) -> str:
    """Read Smithery API key.

    Priority: 1) per-agent AgentTool config, 2) system-level tool config.

    Sensitive fields in tool/AgentTool config are stored encrypted (see
    api.tools._encrypt_sensitive_fields). We must decrypt here before
    handing the value to httpx — otherwise Smithery rejects with 401.
    Falls back to raw value when decrypt fails (e.g. legacy plaintext keys).
    """
    def _maybe_decrypt(raw: str) -> str:
        if not raw:
            return ""
        return decrypt_sensitive_fields({"value": raw}, {"fields": [{"key": "value", "type": "password"}]}).get("value", raw)

    try:
        async with async_session() as db:
            agent_tenant_id = None
            if agent_id:
                from app.models.agent import Agent as AgentModel
                tenant_r = await db.execute(select(AgentModel.tenant_id).where(AgentModel.id == agent_id))
                agent_tenant_id = tenant_r.scalar_one_or_none()

            # 1) Per-agent: check AgentTool configs for any MCP tool with a smithery_api_key
            if agent_id:
                at_r = await db.execute(
                    select(AgentTool).where(AgentTool.agent_id == agent_id)
                )
                for at in at_r.scalars().all():
                    if at.config and at.config.get("smithery_api_key"):
                        return _maybe_decrypt(at.config["smithery_api_key"])
            # 2) Tenant/company fallback for builtin discovery tools
            for tool_name in ("discover_resources", "import_mcp_server"):
                r = await db.execute(select(Tool).where(Tool.name == tool_name))
                tool = r.scalar_one_or_none()
                if not tool:
                    continue
                tenant_config = await get_tenant_tool_config(db, agent_tenant_id, tool.name, tool.config_schema)
                if tenant_config.get("smithery_api_key"):
                    return tenant_config["smithery_api_key"]
                if tool.config and tool.config.get("smithery_api_key") and not agent_tenant_id:
                    return _maybe_decrypt(tool.config["smithery_api_key"])
    except Exception:
        pass
    return ""


async def _search_smithery_api(query: str, max_results: int, api_key: str) -> list[dict]:
    """Search Smithery registry, returns normalized results."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                f"{SMITHERY_API_BASE}/servers",
                params={"q": query, "pageSize": max_results},
                headers=headers,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
        results = []
        for srv in data.get("servers", [])[:max_results]:
            results.append({
                "name": srv.get("qualifiedName", ""),
                "display_name": srv.get("displayName", ""),
                "description": srv.get("description", "")[:200],
                "remote": srv.get("remote", False),
                "verified": srv.get("verified", False),
                "use_count": srv.get("useCount", 0),
                "homepage": srv.get("homepage", ""),
                "source": "Smithery",
            })
        return results
    except Exception:
        return []


async def _get_modelscope_api_token(agent_id: uuid.UUID | None = None) -> str:
    """Read ModelScope API token from discover_resources tool config."""
    try:
        async with async_session() as db:
            agent_tenant_id = None
            if agent_id:
                from app.models.agent import Agent as AgentModel
                tenant_r = await db.execute(select(AgentModel.tenant_id).where(AgentModel.id == agent_id))
                agent_tenant_id = tenant_r.scalar_one_or_none()
            for tool_name in ("discover_resources", "import_mcp_server"):
                r = await db.execute(select(Tool).where(Tool.name == tool_name))
                tool = r.scalar_one_or_none()
                if not tool:
                    continue
                tenant_config = await get_tenant_tool_config(db, agent_tenant_id, tool.name, tool.config_schema)
                if tenant_config.get("modelscope_api_token"):
                    return tenant_config["modelscope_api_token"]
                if tool.config and tool.config.get("modelscope_api_token") and not agent_tenant_id:
                    return tool.config["modelscope_api_token"]
    except Exception:
        pass
    return ""


async def _search_modelscope_api(query: str, max_results: int, agent_id: uuid.UUID | None = None) -> list[dict]:
    """Search ModelScope MCP Hub via official OpenAPI (no WAF issues)."""
    api_token = await _get_modelscope_api_token(agent_id)
    if not api_token:
        return []  # Silently skip if no token configured

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}",
        "Cookie": f"m_session_id={api_token}",
        "User-Agent": "modelscope-mcp-server/1.0",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.put(
                f"{MODELSCOPE_API_BASE}/openapi/v1/mcp/servers",
                json={"page_size": max_results, "page_number": 1, "search": query, "filter": {}},
                headers=headers,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not data.get("success"):
                return []

        servers_data = data.get("data", {}).get("mcp_server_list", [])
        if not servers_data:
            return []

        results = []
        for srv in servers_data[:max_results]:
            server_id = srv.get("id", "")
            results.append({
                "name": server_id,
                "display_name": srv.get("name", server_id),
                "description": srv.get("description", "")[:200],
                "remote": srv.get("is_hosted", False),
                "verified": True,
                "use_count": 0,
                "homepage": f"https://modelscope.cn/mcp/servers/{server_id}",
                "source": "ModelScope",
            })
        return results
    except Exception as e:
        logger.error(f"[ResourceDiscovery] ModelScope search failed: {e}")
        return []


async def search_registries(query: str, max_results: int = 5, agent_id: uuid.UUID | None = None) -> str:
    """Search both Smithery and ModelScope for MCP servers."""
    api_key = await _get_smithery_api_key(agent_id)

    # Search both registries in parallel
    import asyncio
    smithery_task = _search_smithery_api(query, max_results, api_key)
    modelscope_task = _search_modelscope_api(query, max_results, agent_id)
    smithery_results, modelscope_results = await asyncio.gather(smithery_task, modelscope_task)

    # Merge: Smithery first, then ModelScope (deduplicate by name)
    seen_names = set()
    all_results = []
    for r in smithery_results + modelscope_results:
        if r["name"] not in seen_names:
            seen_names.add(r["name"])
            all_results.append(r)

    if not all_results:
        return f'🔍 No MCP servers found for "{query}" on Smithery or ModelScope. Try different keywords.'

    results = []
    for i, srv in enumerate(all_results[:max_results], 1):
        verified = " ✅" if srv["verified"] else ""
        source_tag = f"[{srv['source']}]"
        if srv["remote"]:
            deploy_info = "🌐 Remote (no local install needed)"
        else:
            deploy_info = "💻 Local install required"
        use_info = f" · 👥 {srv['use_count']:,} users" if srv["use_count"] else ""
        hp = srv['homepage']

        results.append(
            f"**{i}. {srv['display_name']}**{verified} {source_tag}\n"
            f"   ID: `{srv['name']}`\n"
            f"   {srv['description']}\n"
            f"   {deploy_info}{use_info}\n"
            f"   {'🔗 ' + hp if hp else ''}"
        )

    header = f'🔍 Found {len(results)} MCP server(s) for "{query}":\n\n'
    footer = (
        "\n\n---\n"
        "💡 To import a remote server, use `import_mcp_server` with the server ID.\n"
        '   Example: import_mcp_server(server_id="gmail")'
    )
    return header + "\n\n".join(results) + footer


# Keep backward-compatible alias
async def search_smithery(query: str, max_results: int = 5, agent_id: uuid.UUID | None = None) -> str:
    return await search_registries(query, max_results, agent_id=agent_id)
