"""Resource discovery — search Smithery & ModelScope registries and import MCP servers."""

from app.services.llm.failure_outcome import render_message

import uuid
import httpx
from loguru import logger
from sqlalchemy import select
from app.database import async_session
from app.models.tool import Tool, AgentTool
from app.services.tool_config import (
    decrypt_sensitive_fields as decrypt_sensitive_fields,
    get_tenant_tool_config as get_tenant_tool_config,
)
# Module-level bindings for patchability in tests — no circular import risk
# since agent_tools only imports resource_discovery inside function bodies.
# ensure_workspace was removed in the v1.10 storage refactor; _agent_workspace_root
# is the merged equivalent (returns the per-agent path without creating it).
from app.services.agent_tools import _agent_workspace_root
from app.services.sandbox_mcp_host import SandboxMcpHost
from app.services.sandbox_mcp_hub_client import SandboxMcpHubClient
from app.config import get_settings
from app.services.resource_atlassian import (
    ATLASSIAN_ROVO_MCP_URL as ATLASSIAN_ROVO_MCP_URL,
    ATLASSIAN_ROVO_SERVER_NAME as ATLASSIAN_ROVO_SERVER_NAME,
    ATLASSIAN_ROVO_TOOL_PREFIX as ATLASSIAN_ROVO_TOOL_PREFIX,
    refresh_atlassian_rovo_api_key as refresh_atlassian_rovo_api_key,
    seed_atlassian_rovo_tools as seed_atlassian_rovo_tools,
)
from app.services.resource_registry_search import (
    MODELSCOPE_API_BASE as MODELSCOPE_API_BASE,
    SMITHERY_API_BASE,
    _get_modelscope_api_token as _get_modelscope_api_token,
    _get_smithery_api_key,
    _search_modelscope_api as _search_modelscope_api,
    _search_smithery_api as _search_smithery_api,
    search_registries as search_registries,
    search_smithery as search_smithery,
)


# ── Import MCP Server ───────────────────────────────────────────

async def _ensure_smithery_connection(api_key: str, mcp_url: str, display_name: str, *, connection_id=None) -> dict:
    """Create or reuse a Smithery Connect namespace + connection.

    Returns dict with keys: namespace, connection_id, auth_url (if OAuth needed).
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            # Get or create namespace
            ns_resp = await client.get("https://api.smithery.ai/namespaces", headers=headers)
            namespaces = ns_resp.json().get("namespaces", []) if ns_resp.status_code == 200 else []
            if namespaces:
                namespace = namespaces[0]["name"]
            else:
                create_ns = await client.post(
                    "https://api.smithery.ai/namespaces",
                    json={"name": "clawith"},
                    headers=headers,
                )
                if create_ns.status_code not in (200, 201):
                    return {"error": f"Failed to create namespace: HTTP {create_ns.status_code}"}
                namespace = create_ns.json()["name"]

            # Create connection
            conn_id = connection_id or f"mcp-{uuid.uuid4().hex}"
            conn_resp = await client.post(
                f"https://api.smithery.ai/connect/{namespace}",
                json={"connectionId": conn_id, "mcpUrl": mcp_url, "name": display_name},
                headers=headers,
            )
            if conn_resp.status_code not in (200, 201):
                return {"error": f"Failed to create connection: HTTP {conn_resp.status_code} — {conn_resp.text[:200]}"}

            conn_data = conn_resp.json()
            result = {
                "namespace": namespace,
                "connection_id": conn_data.get("connectionId", conn_id),
            }
            status = conn_data.get("status", {})
            if isinstance(status, dict) and status.get("state") == "auth_required":
                result["auth_url"] = status.get("authorizationUrl", "")
            return result
    except Exception as e:
        return {"error": str(e)[:200]}


async def import_mcp_from_smithery(
    server_id: str,
    agent_id: uuid.UUID,
    config: dict | None = None,
    reauthorize: bool = False,
) -> str:
    """Import an MCP server from Smithery into the platform.

    Uses the Smithery Registry detail API to get tool definitions,
    and stores the deploymentUrl for runtime execution via Smithery Connect.
    If config contains 'smithery_api_key', it's stored per-agent for future use.
    """
    # Defensive: tolerate config passed as JSON string (some LLM tool-calling
    # paths stringify object args). Anything that doesn't look dict-shaped
    # gets dropped instead of blowing up at `dict(config)`.
    if isinstance(config, str):
        try:
            import json as _json
            config = _json.loads(config) if config else {}
        except Exception:
            config = {}
    config = dict(config) if isinstance(config, dict) else {}

    # Extract smithery_api_key from config (user-provided) or fallback to stored
    api_key = config.pop("smithery_api_key", None) or await _get_smithery_api_key(agent_id)
    if not api_key:
        return (
            "❌ Smithery API key is required to import MCP servers.\n\n"
            "请提供你的 Smithery API Key，你可以通过以下步骤获取：\n"
            "1. 注册/登录 https://smithery.ai\n"
            "2. 前往 https://smithery.ai/account/api-keys 创建 API Key\n"
            "3. 将 Key 提供给我，例如：\n"
            '   `import_mcp_server(server_id="github", config={"smithery_api_key": "your-key"})`'
        )

    # Write key back to discover_resources / import_mcp_server AgentTool configs
    # so it shows up in the Config dialog
    try:
        async with async_session() as db:
            for tool_name in ("discover_resources", "import_mcp_server"):
                r = await db.execute(select(Tool).where(Tool.name == tool_name))
                tool = r.scalar_one_or_none()
                if not tool:
                    continue
                at_r = await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.tool_id == tool.id,
                    )
                )
                at = at_r.scalar_one_or_none()
                if at:
                    at.config = {**(at.config or {}), "smithery_api_key": api_key}
                else:
                    db.add(AgentTool(
                        agent_id=agent_id, tool_id=tool.id, enabled=True,
                        source="system", config={"smithery_api_key": api_key},
                    ))
            await db.commit()
    except Exception:
        pass  # non-critical — key is still usable from MCP tool configs

    # Step 1: Search for server by ID
    headers = {"Accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                f"{SMITHERY_API_BASE}/servers",
                params={"q": server_id.lstrip("@"), "pageSize": 5},
                headers=headers,
            )
            if resp.status_code != 200:
                return f"❌ Server '{server_id}' not found on Smithery (HTTP {resp.status_code})"
            data = resp.json()
            servers = data.get("servers", [])
            server_info = None
            clean_id = server_id.lstrip("@")
            for s in servers:
                if s.get("qualifiedName") == clean_id or s.get("qualifiedName") == server_id:
                    server_info = s
                    break
            if not server_info and servers:
                server_info = servers[0]
            if not server_info:
                return f"❌ Server '{server_id}' not found on Smithery."
    except Exception as e:
        return f"❌ Failed to fetch server info: {str(e)[:200]}"

    display_name = server_info.get("displayName", server_id.split("/")[-1])
    description = server_info.get("description", "")
    qualified_name = server_info.get("qualifiedName", server_id.lstrip("@"))

    # Check if server supports remote hosting
    if not server_info.get("remote"):
        return (
            f"⚠️ **{display_name}** (`{qualified_name}`) does not support remote hosting via Smithery Connect.\n"
            f"This server requires local installation and cannot be imported automatically.\n"
            f"🔗 {server_info.get('homepage', '')}"
        )

    # Step 2: Get full server details including tools from registry API
    tools_discovered = []
    deployment_url = None
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            detail_resp = await client.get(
                f"{SMITHERY_API_BASE}/servers/{qualified_name}",
                headers=headers,
            )
            if detail_resp.status_code == 200:
                detail = detail_resp.json()
                deployment_url = detail.get("deploymentUrl")
                raw_tools = detail.get("tools", [])
                tools_discovered = [
                    {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "inputSchema": t.get("inputSchema", {}),
                    }
                    for t in raw_tools if t.get("name")
                ]
                logger.info(f"[ResourceDiscovery] Got {len(tools_discovered)} tools from registry for {qualified_name}")
            else:
                logger.warning(f"[ResourceDiscovery] Could not fetch detail for {qualified_name}: HTTP {detail_resp.status_code}")
    except Exception as e:
        logger.error(f"[ResourceDiscovery] Could not fetch server detail: {e}")

    # Step 3: Determine the MCP server URL for runtime execution
    base_mcp_url = deployment_url or f"https://{qualified_name}.run.tools"

    # Step 3.5: Auto-create Smithery Connect namespace + connection
    smithery_config = {}  # will be merged into every AgentTool.config
    auth_message = ""
    from app.models.agent import Agent as _Agent
    from app.services.mcp_private_installations import private_server

    identity_config = {**config, "server_id": qualified_name, "mcp_url": base_mcp_url,
                       "smithery_api_key": api_key}
    existing_config = {}
    async with async_session() as db:
        agent_row = await db.get(_Agent, agent_id)
        tenant_id = agent_row.tenant_id if agent_row else None
        existing = await private_server(db, agent_id, tenant_id, display_name, {
            "transport": "http", "base_url_template": base_mcp_url,
            "headers_template": {}, "credential_template": None,
        }, installation_config=identity_config, create=False)
        if existing is not None:
            stored = await db.scalar(select(AgentTool.config).join(Tool).where(
                Tool.mcp_server_id == existing.id, AgentTool.agent_id == agent_id,
            ).limit(1))
            existing_config = dict(stored or {})
        await db.rollback()
    if existing_config.get("smithery_connection_id") and not reauthorize:
        conn_result = {"namespace": existing_config["smithery_namespace"],
                       "connection_id": existing_config["smithery_connection_id"]}
    else:
        conn_result = await _ensure_smithery_connection(
            api_key, base_mcp_url, display_name,
            connection_id=existing_config.get("smithery_connection_id"),
        )
    if "error" in conn_result:
        auth_message = f"\n\n⚠️ Could not auto-create Smithery connection: {conn_result['error']}"
    else:
        smithery_config = {
            "smithery_namespace": conn_result["namespace"],
            "smithery_connection_id": conn_result["connection_id"],
        }
        if conn_result.get("auth_url"):
            auth_message = (
                f"\n\n🔐 **OAuth 授权需要**: 请在浏览器中访问以下链接完成授权：\n"
                f"{conn_result['auth_url']}\n"
                f"授权完成后，工具即可使用。"
            )

    # Step 3.6: Override registry-advertised schema with the runtime server's
    # actual tools/list. Smithery's registry detail can drift behind the live
    # server (we hit this with shibui/finance: registry said `sql`, server
    # required `user_prompt` + `query`). The truth is whatever tools/list
    # returns at call time, so prefer it whenever available.
    if smithery_config:
        ns_ = smithery_config["smithery_namespace"]
        conn_ = smithery_config["smithery_connection_id"]
        try:
            import json as _json
            async with httpx.AsyncClient(timeout=15) as client:
                live_resp = await client.post(
                    f"https://api.smithery.ai/connect/{ns_}/{conn_}/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                )
            if live_resp.status_code == 200:
                live_data = None
                # Smithery Connect returns SSE; parse the first data: line.
                for line in live_resp.text.split("\n"):
                    line = line.strip()
                    if line.startswith("data: "):
                        try:
                            live_data = _json.loads(line[6:])
                            break
                        except _json.JSONDecodeError:
                            pass
                if live_data is None:
                    try:
                        live_data = _json.loads(live_resp.text)
                    except _json.JSONDecodeError:
                        live_data = None
                live_tools = (live_data or {}).get("result", {}).get("tools", []) if live_data else []
                # MCP servers also return prompts here; only treat actual tools.
                live_tools_normalized = [
                    {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "inputSchema": t.get("inputSchema", {}),
                    }
                    for t in live_tools
                    if t.get("name") and isinstance(t.get("inputSchema"), dict)
                ]
                if live_tools_normalized:
                    logger.info(
                        f"[ResourceDiscovery] Using live tools/list for {qualified_name}: "
                        f"{len(live_tools_normalized)} tool(s) override registry's "
                        f"{len(tools_discovered)}"
                    )
                    tools_discovered = live_tools_normalized
        except Exception as e:
            logger.warning(
                f"[ResourceDiscovery] Live tools/list failed for {qualified_name}, "
                f"falling back to registry schema: {e}"
            )

    # Merge smithery_config + user config for AgentTool
    agent_tool_config = {
        **smithery_config,
        **config,
        "server_id": qualified_name,
        "mcp_url": base_mcp_url,
        "smithery_api_key": api_key,
    }

    async with async_session() as db:
        from app.models.agent import Agent as _Agent
        imported_tools = []
        agent_row = (
            await db.execute(select(_Agent).where(_Agent.id == agent_id))
        ).scalar_one_or_none()
        tenant_id = agent_row.tenant_id if agent_row else None
        from app.services.mcp_private_installations import private_server, persist_private_catalog

        srv = await private_server(db, agent_id, tenant_id, display_name, {
            "transport": "http", "base_url_template": base_mcp_url,
            "headers_template": {}, "credential_template": None,
        }, installation_config=agent_tool_config)
        mcp_server_id = srv.id
        stored_config = await db.scalar(select(AgentTool.config).join(Tool).where(
            Tool.mcp_server_id == srv.id, AgentTool.agent_id == agent_id,
        ).limit(1))
        route_keys = ("smithery_namespace", "smithery_connection_id")
        if stored_config and any(stored_config.get(key) != agent_tool_config.get(key) for key in route_keys):
            # Another import committed while discovery was waiting on the provider.
            # Its connection owns this installation, including any pending OAuth.
            imported_tools = list(await db.scalars(select(Tool.display_name).join(AgentTool).where(
                Tool.mcp_server_id == srv.id, AgentTool.agent_id == agent_id,
            )))
            auth_message = "\n\n" + render_message("mcpAccess.existingInstallation")
        else:
            catalog = tools_discovered or [{"name": None, "description": description}]
            imported_tools = await persist_private_catalog(db, srv, agent_id, catalog, agent_tool_config)

        await db.commit()

    result = f"🔌 Imported MCP server: **{display_name}** (`{server_id}`)\n\n"
    result += "\n".join(imported_tools)
    result += f"\n\n🆔 MCP Server ID: `{mcp_server_id}`"
    result += f"\n\n📡 MCP Server URL: `{base_mcp_url}`"
    if auth_message:
        result += auth_message
    else:
        result += "\n\n💡 The imported tools are now available for use."
    return result


# ── Direct URL Import ───────────────────────────────────────────

async def import_mcp_direct(
    mcp_url: str,
    agent_id: uuid.UUID,
    server_name: str | None = None,
    api_key: str | None = None,
    headers: dict | None = None,
) -> str:
    """Import an MCP server by directly connecting to its HTTP/SSE endpoint.

    This bypasses Smithery entirely — useful for self-hosted or third-party
    MCP servers that provide their own public endpoint.

    `headers` accepts the standard `mcpServers.<name>.headers` dict and is
    persisted in AgentTool.config so the runtime call path can replay them.
    """
    from app.services.mcp_client import MCPClient

    # Build URL with apiKey if provided
    full_url = mcp_url
    if api_key and "?" in mcp_url:
        full_url = f"{mcp_url}&apiKey={api_key}"
    elif api_key:
        full_url = f"{mcp_url}?apiKey={api_key}"

    # Derive a display name from a hostname-like value. The tool-name PREFIX is
    # no longer derived here — it comes from the (uniquified) mcp_servers row
    # created below, so two different servers can never produce the same
    # Tool.name. Even when the caller passes a full URL as `server_name` we
    # strip it down so the final display fits Tool.name's varchar(100).
    candidate = (server_name or "").strip()
    looks_like_url_or_path = (
        candidate.startswith("http://")
        or candidate.startswith("https://")
        or "/" in candidate
        or len(candidate) > 60
    )
    if not candidate or looks_like_url_or_path:
        candidate = mcp_url.split("//")[-1].split("/")[0].split(":")[0]
    display_name = candidate[:60] or "mcp-server"

    # Try to list tools from the endpoint
    tools_discovered = []
    server_instructions: str | None = None
    try:
        client = MCPClient(full_url, headers=headers)
        tools_discovered = await client.list_tools()
        # `list_tools` triggers the MCP `initialize` handshake under the hood,
        # which populates `client.server_instructions` if the server provided
        # one. Persisted on each tool row so the agent collector can inject
        # it into the system prompt without re-handshaking on every request.
        server_instructions = client.server_instructions
        logger.info(
            f"[DirectImport] Got {len(tools_discovered)} tools from {mcp_url}"
            + (f" with server instructions ({len(server_instructions)} chars)" if server_instructions else "")
        )
    except Exception as e:
        error_message = str(e).strip() or type(e).__name__
        logger.error(f"[DirectImport] Could not list tools from {mcp_url}: {error_message}")
        return (
            f"❌ MCP server import failed: **{display_name}**\n\n"
            f"Could not discover tools from `{mcp_url}`.\n\n"
            f"Cause: `{error_message[:500]}`\n\n"
            "No MCP server or tool records were created. Check the endpoint, transport, and credentials, then retry."
        )

    if not tools_discovered:
        logger.warning(f"[DirectImport] Server returned zero tools: {mcp_url}")
        return (
            f"❌ MCP server import failed: **{display_name}**\n\n"
            f"`{mcp_url}` connected successfully but returned zero tools.\n\n"
            "No MCP server or tool records were created. Confirm that this endpoint exposes MCP tools, then retry."
        )

    # Config to store in AgentTool
    agent_tool_config: dict = {
        "mcp_url": mcp_url,
        "server_name": display_name,
    }
    if api_key:
        agent_tool_config["api_key"] = api_key
    if isinstance(headers, dict) and headers:
        agent_tool_config["headers"] = headers

    async with async_session() as db:
        from app.models.mcp_server import MCPServer
        from app.services.mcp_server_service import upsert_mcp_server_from_tools

        imported_tools = []

        # Resolve agent's tenant_id for the mcp_servers bridge.
        from app.models.agent import Agent as _Agent
        _agent_row = (await db.execute(select(_Agent).where(_Agent.id == agent_id))).scalar_one_or_none()
        _tenant_id = _agent_row.tenant_id if _agent_row else None

        # Find-or-create an Agent-private mcp_servers row FIRST. Every
        # tool we import is then named after — and bound to — THIS server. The
        # server identity is stable per (Agent, connection configuration), so per-server tool names are
        # unique too: this is what stops the global Tool.name dedup from merging
        # different Agent installations onto one mutable catalog.
        srv_id = await upsert_mcp_server_from_tools(
            db,
            tenant_id=_tenant_id,
            server_url=mcp_url,
            server_name=display_name,
            headers_template=isinstance(headers, dict) and headers or None,
            api_key=api_key,
            owner_agent_id=agent_id,
        )
        srv = (await db.execute(select(MCPServer).where(MCPServer.id == srv_id))).scalar_one()

        async def _ensure_agent_tool(tool_id: uuid.UUID):
            agent_check = await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.tool_id == tool_id,
                )
            )
            at = agent_check.scalar_one_or_none()
            if at:
                at.config = {**(at.config or {}), **agent_tool_config}
            else:
                db.add(AgentTool(
                    agent_id=agent_id, tool_id=tool_id, enabled=True,
                    source="user_installed", installed_by_agent_id=agent_id,
                    config=agent_tool_config,
                ))

        async def _upsert_tool(raw_name: str | None, description: str, schema: dict, tool_display: str) -> bool:
            """Create-or-update a Tool row scoped to THIS server, enable it on the
            agent, and return True iff it was newly created. Dedup is per-server
            (mcp_server_id, mcp_tool_name) — never the global Tool.name."""
            # Tool.name is varchar(100); cap defensively for long tool names.
            from app.services.mcp_naming import tool_function_name

            tool_name = tool_function_name(srv, raw_name or "server")
            dedup = select(Tool).where(Tool.mcp_server_id == srv.id)
            dedup = dedup.where(Tool.mcp_tool_name == raw_name) if raw_name else dedup.where(Tool.mcp_tool_name.is_(None))
            existing = (await db.execute(dedup)).scalar_one_or_none()
            if existing is not None:
                existing.description = description[:500]
                existing.parameters_schema = schema
                existing.mcp_server_url = mcp_url
                existing.mcp_server_name = display_name
                if server_instructions:
                    existing.mcp_server_instructions = server_instructions
                await _ensure_agent_tool(existing.id)
                return False
            tool = Tool(
                name=tool_name,
                display_name=tool_display,
                description=description[:500],
                type="mcp",
                category="mcp",
                icon="🔌",
                parameters_schema=schema,
                mcp_server_url=mcp_url,
                mcp_server_name=display_name,
                mcp_tool_name=raw_name,
                mcp_server_instructions=server_instructions,
                mcp_server_id=srv.id,
                enabled=True,
                is_default=False,
                source="agent",
            )
            db.add(tool)
            await db.flush()
            await _ensure_agent_tool(tool.id)
            return True

        for mcp_tool in tools_discovered:
            raw = mcp_tool["name"]
            created = await _upsert_tool(
                raw,
                mcp_tool.get("description", ""),
                mcp_tool.get("inputSchema", {"type": "object", "properties": {}}),
                f"{display_name}: {raw}",
            )
            imported_tools.append(
                f"{'✅' if created else '⏭️'} {display_name}: {raw}" + ("" if created else " (updated)")
            )

        await db.commit()

    result = f"🔌 Imported MCP server: **{display_name}** ({len(tools_discovered)} tools)\n\n"
    result += "\n".join(imported_tools)
    result += f"\n\n🆔 MCP Server ID: `{srv.id}`"
    result += f"\n\n📡 MCP Server URL: `{mcp_url}`"
    result += "\n\n💡 The imported tools are now available for use."
    return result




# ── Agent Self-Install: stdio MCP via aio-sandbox hub ─────────────────────────

async def import_mcp_stdio_direct(agent_id, parsed: dict) -> str:
    """Agent self-install of a stdio/npx MCP server (fully open — no allowlist).

    Mirrors import_mcp_direct (http) but hosts the process via the aio-sandbox hub.
    `parsed` is the dict from mcp_config_parser (transport=stdio, command/args/env).
    """
    import uuid as _uuid
    from app.services.mcp_server_service import (
        get_or_create_agent_stdio_server, persist_stdio_discovered_tools,
    )
    from app.models.agent import Agent
    from sqlalchemy import select

    _settings = get_settings()
    if not _settings.SANDBOX_API_URL:
        return "❌ 无法自助安装 stdio MCP:本环境未配置 SANDBOX_API_URL(需要 aio-sandbox)。"

    cfg = {"command": parsed.get("command"),
           "args": parsed.get("args") or [],
           "env": parsed.get("env") or {}}
    if not cfg["command"]:
        return "❌ stdio MCP 配置缺少 command。"

    async with async_session() as db:
        agent_row = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
        if agent_row is None:
            return "❌ 找不到当前 agent。"
        tenant_id = agent_row.tenant_id

        await db.rollback()
        discovery_name = f"mcp-discovery-{_uuid.uuid4().hex}"

        # 发现:按 agent workspace cwd 注册临时条目 → list → 持久化 → 注销
        host = SandboxMcpHost(_settings.SANDBOX_API_URL, _settings.SANDBOX_API_KEY)
        hub = SandboxMcpHubClient(_settings.SANDBOX_API_URL, _settings.SANDBOX_API_KEY)
        ws = _agent_workspace_root(_uuid.UUID(str(agent_id)))
        ws.mkdir(parents=True, exist_ok=True)
        work_dir = str(ws.resolve())
        try:
            entry = await host.ensure_registered(discovery_name, str(agent_id), cfg, cwd=work_dir)
        except Exception as e:
            return f"❌ 注册到沙箱失败:{e}"
        try:
            tools = await hub.list_tools(entry)
        except httpx.TimeoutException:
            return (
                "⏱️ 工具发现超时:npx 包可能较大或网络较慢(首次安装尤甚),"
                "请稍后用相同配置重试。"
            )
        except Exception as e:
            return f"❌ 启动/发现工具失败(可能是包名错误或网络不通):{e}"
        finally:
            try:
                await host.deregister(entry)
            except Exception:
                pass

        if not tools:
            return render_message("mcpAccess.noDiscoveredTools")

        srv = await get_or_create_agent_stdio_server(db, agent_id, tenant_id, cfg)
        count = await persist_stdio_discovered_tools(db, srv, tools, source="agent")

        # 分配给当前 agent
        assigned = []
        rows = (await db.execute(select(Tool).where(Tool.mcp_server_id == srv.id))).scalars().all()
        for tool in rows:
            at = (await db.execute(select(AgentTool).where(
                AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id))).scalar_one_or_none()
            if at is None:
                db.add(AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=True,
                                 source="user_installed", installed_by_agent_id=agent_id,
                                 config=dict(cfg)))
            assigned.append(tool.mcp_tool_name)
        await db.commit()

        lines = "\n".join(f"• {n}" for n in assigned[:20])
        more = f"\n…共 {len(assigned)} 个" if len(assigned) > 20 else ""
        return (
            f"✅ 已安装 stdio MCP 服务 `{srv.name}` (`{srv.id}`),发现并分配 {count} 个工具:\n{lines}{more}\n\n"
            f"⚠️ 重要:这 {count} 个工具会在**下一轮对话**才进入你的可用工具列表,"
            f"**本轮还调用不了**。请现在就**结束本轮回复**(不要在本轮尝试调用它们),"
            f"告诉用户工具已安装就绪,请用户在下一条消息里让你执行需要这些工具的任务。"
        )
