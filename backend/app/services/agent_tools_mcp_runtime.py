"""MCP and Smithery tool execution runtime."""

import uuid

from loguru import logger
from app.services.tool_results import normalize_tool_result
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models.tool import AgentTool, Tool
from app.services.agent_tools_config_runtime import _decrypt_sensitive_fields
from app.services.agent_tools_file_support import _agent_workspace_root
from app.services.mcp_access import resolve_mcp_execution
from app.models.agent import Agent
from app.services.mcp_connection_resolution import (
    render_http_connection, resolve_agent_connection, resolve_assignment_config, resolve_smithery_connection,
)
from app.services.llm.failure_outcome import render_message
from app.services.sandbox_mcp_host import SandboxMcpHost
from app.services.sandbox_mcp_hub_client import SandboxMcpHubClient


async def _execute_mcp_tool(
    tool_name: str,
    arguments: dict,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    tool_call_id: str = "",
) -> str | dict:
    """Execute a tool via MCP if it exists in the DB as an MCP tool."""
    try:
        from app.models.mcp_server import MCPServer
        from app.services.mcp_client import MCPClient
        from app.services.placeholder_engine import (
            render,
            render_dict,
            ALL_ROOTS,
            DisallowedPlaceholderError,
            UnknownPlaceholderError,
        )

        async with async_session() as db:
            if not agent_id:
                return render_message("mcpAccess.unavailable")
            candidates = await resolve_mcp_execution(db, agent_id, tool_name)
            if len(candidates) > 1:
                return render_message("mcpAccess.ambiguous")
            if not candidates:
                return render_message("mcpAccess.unavailable")
            tool, live_assignment = candidates[0]
            agent = await db.get(Agent, agent_id)

            # NEW PATH: when tool.mcp_server_id is populated (P0a migration done),
            # use the mcp_servers table + overrides + placeholder rendering.
            if tool.mcp_server_id:
                srv = (
                    await db.execute(select(MCPServer).where(MCPServer.id == tool.mcp_server_id))
                ).scalar_one_or_none()
                if srv is None:
                    return f"❌ MCP tool {tool_name}: server row {tool.mcp_server_id} not found"

                cfg, effective_assignment_config, ctx = await resolve_agent_connection(
                    db, srv, agent, tool, live_assignment, user_id, session_id,
                )
                # Configuration and identity are resolved; provider waits must
                # not keep this read transaction open.
                await db.commit()

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

                try:
                    resolved_url, resolved_headers, resolved_credential = render_http_connection(cfg, ctx)
                except (DisallowedPlaceholderError, UnknownPlaceholderError) as exc:
                    return f"{render_message('mcpAccess.placeholderError')} — {exc}"

                mcp_name = tool.mcp_tool_name or tool_name
                # Smithery routing: if the URL still contains run.tools and we have config,
                # delegate. This preserves the legacy code's Smithery support.
                if ".run.tools" in resolved_url:
                    # Smithery path expects merged_config dict with credential and headers.
                    # Adapter: stuff resolved values back into a dict matching the legacy contract.
                    smithery_cfg = {
                        **effective_assignment_config,
                        "smithery_api_key": resolved_credential or effective_assignment_config.get("smithery_api_key"),
                        "headers": resolved_headers if resolved_headers else None,
                    }
                    smithery_cfg = {k: v for k, v in smithery_cfg.items() if v}
                    return await _execute_via_smithery_connect(
                        resolved_url,
                        mcp_name,
                        arguments,
                        smithery_cfg,
                        agent_id=agent_id,
                        server_id=srv.id,
                    )

                client = MCPClient(
                    resolved_url,
                    api_key=resolved_credential,
                    headers=resolved_headers or None,
                )
                return await client.call_tool(mcp_name, arguments)

            # Legacy MCP uses the same effective assignment as server-backed MCP.
            agent_config = await resolve_assignment_config(db, tool, live_assignment, agent, user_id)

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
            return await _execute_via_smithery_connect(mcp_url, mcp_name, arguments, merged_config, agent_id=agent_id, tool_id=tool.id)

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
    mcp_url: str, tool_name: str, arguments: dict, config: dict, agent_id=None, server_id=None, tool_id=None
) -> str | dict:
    """Execute an MCP tool via Smithery Connect API.

    Uses stored namespace/connection or falls back to creating one.
    Smithery Connect returns SSE-format responses that need special parsing.
    """
    import httpx
    import json as json_mod

    try:
        endpoint, api_key, namespace, connection_id = await resolve_smithery_connection(
            config, agent_id, allow_legacy_defaults=server_id is None,
        )
    except ValueError as exc:
        return str(exc)

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
                endpoint,
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
                recovery_result = await _smithery_auto_recover(api_key, mcp_url, namespace, connection_id, agent_id, server_id, tool_id)
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
                    return normalize_tool_result(f"❌ Unexpected response from Smithery: {raw[:300]}", is_error=True)

            if "error" in data:
                err = data["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                # Check if error indicates auth/connection issue
                auth_keywords = ["auth", "unauthorized", "forbidden", "expired", "not found", "connection"]
                if any(kw in msg.lower() for kw in auth_keywords):
                    recovery_result = await _smithery_auto_recover(api_key, mcp_url, namespace, connection_id, agent_id, server_id, tool_id)
                    if recovery_result:
                        return recovery_result
                return normalize_tool_result(f"❌ MCP tool error: {msg[:300]}", is_error=True)

            return normalize_tool_result(data.get("result", {}))

    except Exception as e:
        return normalize_tool_result(f"❌ Smithery Connect error: {str(e)[:200]}", is_error=True)


async def _smithery_auto_recover(
    api_key: str, mcp_url: str, namespace: str, connection_id: str, agent_id=None, server_id=None, tool_id=None
) -> str | None:
    """Attempt to auto-recover a failed Smithery connection.

    Re-creates the Smithery Connect connection. If OAuth is needed,
    returns the auth URL for the user. Returns None if recovery fails silently.
    """
    try:
        from app.services.resource_discovery import _ensure_smithery_connection

        display_name = connection_id.replace("-", " ").title() if connection_id else "MCP Server"

        conn_result = await _ensure_smithery_connection(api_key, mcp_url, display_name, connection_id=connection_id)
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
        if agent_id and (server_id or tool_id):
            try:
                async with async_session() as db:
                    # Recovery belongs to one installation, never to a URL.
                    identity = Tool.mcp_server_id == server_id if server_id else Tool.id == tool_id
                    r = await db.execute(select(Tool).where(identity, Tool.type == "mcp"))
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
