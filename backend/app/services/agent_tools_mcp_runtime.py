"""MCP and Smithery tool execution runtime."""

import uuid

from loguru import logger
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.services.agent_runtime_workspace import current_agent_runtime_workspace
from app.services.agent_tools_config_runtime import _decrypt_sensitive_fields
from app.services.agent_tools_file_support import _agent_workspace_root
from app.services.sandbox_mcp_host import SandboxMcpHost
from app.services.sandbox_mcp_hub_client import SandboxMcpHubClient


async def _execute_mcp_tool(
    tool_name: str,
    arguments: dict,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    tool_call_id: str = "",
) -> str:
    """Execute a tool via MCP if it exists in the DB as an MCP tool."""
    try:
        from app.models.tool import Tool, AgentTool
        from app.models.mcp_server import MCPServer
        from app.services.mcp_client import MCPClient
        from app.services.placeholder_engine import (
            render,
            render_dict,
            ALL_ROOTS,
            DisallowedPlaceholderError,
            UnknownPlaceholderError,
        )
        from app.services.mcp_server_service import (
            compose_runtime_config,
            lookup_overrides,
            lookup_project_source_tool_config,
            build_placeholder_context_for_call,
        )

        async with async_session() as db:
            # Primary lookup: legacy-prefixed name (e.g.
            # mcp_shibui_finance_unlock_financial_analysis).
            result = await db.execute(
                select(Tool).where(Tool.name == tool_name, Tool.type == "mcp", Tool.enabled == True)
            )
            tool = result.scalar_one_or_none()

            # Fallback: LLM sometimes drops the mcp_<server>_ prefix and calls
            # the bare MCP-side tool name (e.g. unlock_financial_analysis).
            # Resolve by mcp_tool_name when the prefixed name doesn't match.
            if not tool:
                if not agent_id:
                    return f"❌ MCP tool {tool_name}: current agent identity is required"
                candidates = (
                    (
                        await db.execute(
                            select(Tool)
                            .join(AgentTool, AgentTool.tool_id == Tool.id)
                            .where(
                                Tool.mcp_tool_name == tool_name,
                                Tool.type == "mcp",
                                Tool.enabled == True,
                                AgentTool.agent_id == agent_id,
                                AgentTool.enabled == True,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(candidates) > 1:
                    return (
                        f"❌ MCP tool name '{tool_name}' is ambiguous for this agent; use the exact platform tool name."
                    )
                tool = candidates[0] if candidates else None

            if not tool:
                logger.warning(f"[MCP] Unknown tool: {tool_name}")
                return f"Unknown tool: {tool_name}"

            # The LLM tool schema is frozen at the beginning of a turn.  A
            # server may be uninstalled later in that same turn, so execution
            # must re-check the live assignment immediately before every call.
            if not agent_id:
                return f"❌ MCP tool {tool_name}: current agent identity is required"
            live_assignment = (
                await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.tool_id == tool.id,
                        AgentTool.enabled == True,
                    )
                )
            ).scalar_one_or_none()
            if live_assignment is None:
                return f"❌ MCP tool {tool_name}: no longer installed or enabled for this agent"
            runtime_workspace = current_agent_runtime_workspace(agent_id)
            referenced_source_config = (
                await lookup_project_source_tool_config(
                    db,
                    project_agent_id=agent_id,
                    tool_id=tool.id,
                    execution_user_id=user_id,
                )
                if runtime_workspace.is_project
                else {}
            )
            effective_assignment_config = _decrypt_sensitive_fields(
                {**referenced_source_config, **dict(live_assignment.config or {})},
                tool.config_schema,
            )

            # NEW PATH: when tool.mcp_server_id is populated (P0a migration done),
            # use the mcp_servers table + overrides + placeholder rendering.
            if tool.mcp_server_id:
                from app.models.agent import Agent

                srv = (
                    await db.execute(select(MCPServer).where(MCPServer.id == tool.mcp_server_id))
                ).scalar_one_or_none()
                if srv is None:
                    return f"❌ MCP tool {tool_name}: server row {tool.mcp_server_id} not found"

                # Load agent → derive tenant_id for tenant override lookup
                agent_row = None
                if agent_id:
                    agent_row = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
                tenant_id = agent_row.tenant_id if agent_row else None

                t_ovr, a_ovr = await lookup_overrides(
                    db,
                    srv.id,
                    tenant_id,
                    agent_id,
                    execution_user_id=user_id,
                    allow_project_source_reference=runtime_workspace.is_project,
                )
                cfg = compose_runtime_config(srv, t_ovr, a_ovr)

                ctx = await build_placeholder_context_for_call(
                    db,
                    agent_id,
                    user_id,
                    session_id=session_id,
                )

                # stdio branch: route through aio-sandbox hub instead of HTTP.
                if cfg.transport == "stdio":
                    _settings_now = get_settings()
                    if not _settings_now.SANDBOX_API_URL:
                        return f"❌ MCP tool {tool_name}: stdio MCP unavailable — SANDBOX_API_URL not configured"
                    try:
                        r_cmd = render(cfg.command_template or "", ctx, ALL_ROOTS, on_unknown="raise")
                        r_args = [render(a, ctx, ALL_ROOTS, on_unknown="raise") for a in (cfg.args_template or [])]
                        r_env = render_dict(cfg.env_template or {}, ctx, ALL_ROOTS, on_unknown="raise")
                    except (DisallowedPlaceholderError, UnknownPlaceholderError) as e:
                        return f"❌ MCP tool {tool_name}: stdio placeholder error — {e}"
                    # Resolve the agent's per-agent workspace so the stdio process
                    # runs in the same isolated directory as code execution.
                    work_dir: str | None = None
                    if agent_id:
                        try:
                            # ensure_workspace was removed in the v1.10 storage refactor;
                            # _agent_workspace_root is the merged equivalent (path only),
                            # so create-if-missing here to preserve the prior semantics.
                            ws = _agent_workspace_root(uuid.UUID(str(agent_id)))
                            ws.mkdir(parents=True, exist_ok=True)
                            work_dir = str(ws.resolve())
                        except Exception as workspace_exc:
                            logger.error(
                                "[MCP] agent workspace unavailable agent={} server_id={}: {}",
                                agent_id,
                                srv.id,
                                workspace_exc,
                            )
                            return (
                                f"❌ MCP tool {tool_name}: agent workspace unavailable — "
                                f"{type(workspace_exc).__name__}: {workspace_exc}"
                            )
                    host = SandboxMcpHost(_settings_now.SANDBOX_API_URL, _settings_now.SANDBOX_API_KEY)
                    # Provider tool_call_id values are not guaranteed unique
                    # across sessions or requests.  Include correlation data
                    # for diagnostics, but always add a backend nonce so two
                    # real executions can never share/deregister one entry.
                    invocation_id = f"{session_id or 'no-session'}:{tool_call_id or 'no-tool-call'}:{uuid.uuid4().hex}"
                    entry = await host.ensure_registered(
                        srv.name,
                        str(agent_id),
                        {"command": r_cmd, "args": r_args, "env": r_env},
                        cwd=work_dir,
                        invocation_id=invocation_id,
                    )
                    logger.info(
                        "[MCP] stdio call start agent={} session={} tool_call={} server_id={} entry={} workspace={}",
                        agent_id,
                        session_id or "no-session",
                        tool_call_id or "no-tool-call",
                        srv.id,
                        entry,
                        work_dir or "default",
                    )
                    hub = SandboxMcpHubClient(_settings_now.SANDBOX_API_URL, _settings_now.SANDBOX_API_KEY)
                    try:
                        return await hub.call_tool(entry, tool.mcp_tool_name or tool_name, arguments)
                    finally:
                        # A hub registration is per call. Removing it on every
                        # exit path prevents failed/timed-out calls from leaving
                        # a process that poisons later MCP calls in the sandbox.
                        try:
                            await host.deregister(entry)
                            logger.info(
                                "[MCP] stdio call cleanup complete agent={} server_id={} entry={} workspace={}",
                                agent_id,
                                srv.id,
                                entry,
                                work_dir or "default",
                            )
                        except Exception as cleanup_exc:
                            logger.warning(
                                "[MCP] stdio runtime cleanup failed agent={} server_id={} entry={} workspace={}: {}",
                                agent_id,
                                srv.id,
                                entry,
                                work_dir or "default",
                                cleanup_exc,
                            )
                # else: existing http path continues below.

                # Render — MCP connection-time gets the FULL ALL_ROOTS context.
                # Unknown placeholders fail loudly so the LLM sees the config error.
                try:
                    resolved_url = render(cfg.url_template, ctx, ALL_ROOTS, on_unknown="raise")
                except (DisallowedPlaceholderError, UnknownPlaceholderError) as e:
                    return f"❌ MCP tool {tool_name}: URL placeholder error — {e}"

                try:
                    resolved_headers = render_dict(cfg.headers_template or {}, ctx, ALL_ROOTS, on_unknown="raise")
                except (DisallowedPlaceholderError, UnknownPlaceholderError) as e:
                    return f"❌ MCP tool {tool_name}: header placeholder error — {e}"

                # HTTP headers are ASCII-only on the wire (RFC 7230). When a
                # placeholder renders to non-ASCII (e.g. ${agent.name} = "小智"),
                # percent-encode the value so the receiving server can decode it
                # via standard urllib.parse.unquote. Pure-ASCII values pass through
                # unchanged.
                from urllib.parse import quote as _url_quote

                def _ascii_safe_header(v):
                    s = str(v)
                    try:
                        s.encode("ascii")
                        return s
                    except UnicodeEncodeError:
                        return _url_quote(s, safe="")

                resolved_headers = {k: _ascii_safe_header(v) for k, v in resolved_headers.items()}

                resolved_credential = None
                if cfg.credential_template:
                    try:
                        resolved_credential = render(cfg.credential_template, ctx, ALL_ROOTS, on_unknown="raise")
                    except (DisallowedPlaceholderError, UnknownPlaceholderError) as e:
                        return f"❌ MCP tool {tool_name}: credential placeholder error — {e}"

                mcp_name = tool.mcp_tool_name or tool_name
                # Smithery routing: if the URL still contains run.tools and we have config,
                # delegate. This preserves the legacy code's Smithery support.
                if ".run.tools" in resolved_url:
                    # Smithery path expects merged_config dict with credential and headers.
                    # Adapter: stuff resolved values back into a dict matching the legacy contract.
                    smithery_cfg = {
                        **effective_assignment_config,
                        "smithery_api_key": resolved_credential,
                        "headers": resolved_headers if resolved_headers else None,
                    }
                    smithery_cfg = {k: v for k, v in smithery_cfg.items() if v}
                    return await _execute_via_smithery_connect(
                        resolved_url,
                        mcp_name,
                        arguments,
                        smithery_cfg,
                        agent_id=agent_id,
                    )

                client = MCPClient(
                    resolved_url,
                    api_key=resolved_credential,
                    headers=resolved_headers or None,
                )
                return await client.call_tool(mcp_name, arguments)

            # LEGACY PATH (mcp_server_id is NULL): unchanged behavior.
            # Load per-agent config override
            agent_config = {}
            if tool and agent_id:
                at_r = await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.tool_id == tool.id,
                    )
                )
                at = at_r.scalar_one_or_none()
                agent_config = effective_assignment_config

        if not tool.mcp_server_url:
            logger.error(f"[MCP] Tool {tool_name} has no server URL configured")
            return f"❌ MCP tool {tool_name} has no server URL configured"

        # Merge global config + agent override
        merged_config = {**(tool.config or {}), **agent_config}
        merged_config = _decrypt_sensitive_fields(merged_config)

        mcp_url = tool.mcp_server_url
        mcp_name = tool.mcp_tool_name or tool_name

        # Detect Smithery-hosted MCP servers (*.run.tools URLs)
        # These need Smithery Connect to route tool calls
        if ".run.tools" in mcp_url and merged_config:
            return await _execute_via_smithery_connect(mcp_url, mcp_name, arguments, merged_config, agent_id=agent_id)

        # Direct MCP call for non-Smithery servers
        # Priority for API key:
        # 1. Per-agent tool config (api_key / atlassian_api_key)
        # 2. Agent's Atlassian channel config (for atlassian_* tools)
        direct_api_key = merged_config.get("api_key") or merged_config.get("atlassian_api_key")
        if not direct_api_key and tool.mcp_server_name == "Atlassian Rovo":
            try:
                from app.api.atlassian import get_atlassian_api_key_for_agent

                direct_api_key = await get_atlassian_api_key_for_agent(agent_id)
            except Exception:
                pass
        direct_headers = merged_config.get("headers") if isinstance(merged_config.get("headers"), dict) else None
        client = MCPClient(mcp_url, api_key=direct_api_key, headers=direct_headers)
        return await client.call_tool(mcp_name, arguments)

    except Exception as e:
        logger.exception(f"[MCP] Tool execution error: {tool_name}")
        return f"❌ MCP tool execution error: {str(e)[:200]}"


async def _execute_via_smithery_connect(
    mcp_url: str, tool_name: str, arguments: dict, config: dict, agent_id=None
) -> str:
    """Execute an MCP tool via Smithery Connect API.

    Uses stored namespace/connection or falls back to creating one.
    Smithery Connect returns SSE-format responses that need special parsing.
    """
    import httpx
    import json as json_mod

    # Get Smithery API key centrally (from discover_resources/import_mcp_server AgentTool config)
    from app.services.resource_discovery import _get_smithery_api_key

    api_key = await _get_smithery_api_key(agent_id)
    if not api_key:
        return (
            "❌ Smithery API key not configured.\n\n"
            "请提供你的 Smithery API Key，你可以通过以下步骤获取：\n"
            "1. 注册/登录 https://smithery.ai\n"
            "2. 前往 https://smithery.ai/account/api-keys 创建 API Key\n"
            "3. 将 Key 提供给我，我会帮你配置"
        )

    # Get namespace + connection from tool config, or use defaults
    namespace = config.pop("smithery_namespace", None)
    connection_id = config.pop("smithery_connection_id", None)

    if not namespace or not connection_id:
        # Fallback: try to get from Smithery settings
        try:
            from app.models.tool import Tool

            async with async_session() as db:
                r = await db.execute(select(Tool).where(Tool.name == "discover_resources"))
                disc_tool = r.scalar_one_or_none()
                if disc_tool and disc_tool.config:
                    namespace = namespace or disc_tool.config.get("smithery_namespace")
                    connection_id = connection_id or disc_tool.config.get("smithery_connection_id")
        except Exception:
            pass

    if not namespace or not connection_id:
        return (
            "❌ Smithery Connect namespace/connection not configured. "
            "Please set smithery_namespace and smithery_connection_id in the tool configuration."
        )

    # Smithery Connect (and many MCP servers) emit SSE responses for tools/call.
    # The server returns 406 Not Acceptable if the client doesn't declare both
    # application/json and text/event-stream in the Accept header. We parse
    # both formats below, so advertise both.
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Call the tool via the existing connection
            tool_resp = await client.post(
                f"https://api.smithery.ai/connect/{namespace}/{connection_id}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                },
                headers=headers,
            )

            # Detect auth/connection failures and attempt auto-recovery
            if tool_resp.status_code in (401, 403, 404):
                recovery_result = await _smithery_auto_recover(api_key, mcp_url, namespace, connection_id, agent_id)
                if recovery_result:
                    return recovery_result
                # If recovery returned None, fall through to normal parsing

            # Smithery Connect returns SSE format: "event: message\ndata: {...}\n"
            raw = tool_resp.text
            data = None

            # Parse SSE response
            for line in raw.split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    try:
                        data = json_mod.loads(line[6:])
                        break
                    except json_mod.JSONDecodeError:
                        pass

            # Fallback: try parsing as plain JSON
            if data is None:
                try:
                    data = json_mod.loads(raw)
                except json_mod.JSONDecodeError:
                    return f"❌ Unexpected response from Smithery: {raw[:300]}"

            if "error" in data:
                err = data["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                # Check if error indicates auth/connection issue
                auth_keywords = ["auth", "unauthorized", "forbidden", "expired", "not found", "connection"]
                if any(kw in msg.lower() for kw in auth_keywords):
                    recovery_result = await _smithery_auto_recover(api_key, mcp_url, namespace, connection_id, agent_id)
                    if recovery_result:
                        return recovery_result
                return f"❌ MCP tool error: {msg[:300]}"

            result = data.get("result", {})
            if isinstance(result, str):
                return result

            content_blocks = result.get("content", []) if isinstance(result, dict) else []
            texts = []
            for block in content_blocks:
                if isinstance(block, str):
                    texts.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif block.get("type") == "image":
                        texts.append(f"[Image: {block.get('mimeType', 'image')}]")
                    else:
                        texts.append(str(block))
                else:
                    texts.append(str(block))

            return "\n".join(texts) if texts else str(result)

    except Exception as e:
        return f"❌ Smithery Connect error: {str(e)[:200]}"


async def _smithery_auto_recover(
    api_key: str, mcp_url: str, namespace: str, connection_id: str, agent_id=None
) -> str | None:
    """Attempt to auto-recover a failed Smithery connection.

    Re-creates the Smithery Connect connection. If OAuth is needed,
    returns the auth URL for the user. Returns None if recovery fails silently.
    """
    try:
        from app.services.resource_discovery import _ensure_smithery_connection

        display_name = connection_id.replace("-", " ").title() if connection_id else "MCP Server"

        conn_result = await _ensure_smithery_connection(api_key, mcp_url, display_name)
        if "error" in conn_result:
            return (
                f"❌ MCP tool connection expired and auto-recovery failed: {conn_result['error']}\n\n"
                f'💡 Please re-authorize by telling me: `import_mcp_server(server_id="...", reauthorize=true)`'
            )

        if conn_result.get("auth_url"):
            # A newly-created Smithery connection is not usable until the user
            # completes OAuth. Keep the existing stored connection in place so
            # a still-valid old connection is not overwritten by an unauthenticated
            # replacement. The user-facing auth URL is enough for recovery.
            return (
                f"🔐 MCP tool connection expired. Re-authorization needed.\n\n"
                f"Please visit the following URL to re-authorize:\n"
                f"{conn_result['auth_url']}\n\n"
                f"After completing authorization, the tools will work again automatically."
            )

        # Update stored config with new connection info
        new_config = {
            "smithery_namespace": conn_result["namespace"],
            "smithery_connection_id": conn_result["connection_id"],
        }
        if agent_id:
            try:
                from app.models.tool import Tool, AgentTool

                async with async_session() as db:
                    # Update all MCP tools for this server URL
                    r = await db.execute(select(Tool).where(Tool.mcp_server_url == mcp_url, Tool.type == "mcp"))
                    for tool in r.scalars().all():
                        at_r = await db.execute(
                            select(AgentTool).where(
                                AgentTool.agent_id == agent_id,
                                AgentTool.tool_id == tool.id,
                            )
                        )
                        at = at_r.scalar_one_or_none()
                        if at:
                            at.config = {**(at.config or {}), **new_config}
                    await db.commit()
            except Exception:
                pass  # Non-critical — connection may still work

        # Connection re-created without OAuth — should work now
        return None  # Signal caller to retry (but we don't retry here to avoid loops)

    except Exception as e:
        return f"❌ Auto-recovery failed: {str(e)[:200]}"

__all__ = [name for name in globals() if not name.startswith("__")]
