"""Admin REST endpoints for MCP server management.

All endpoints require ``platform_admin`` role. Credentials are
encrypted at rest and NEVER returned in responses (response schemas
expose ``credential_state`` only).

Audit: every write writes a record via ``write_audit_log`` with
action prefix ``MCP_SERVER_*`` and a JSON diff of changed fields.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.mcp_server_import import router as import_router
from app.api.mcp_server_updates import normalize_masked_secret_update
from app.core.security import get_current_user, require_role
from app.database import get_db
from app.models.agent import Agent
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.schemas.mcp_server import (
    DryRunRequest,
    DryRunResponse,
    MCPServerCreate,
    MCPServerOut,
    MCPServerOverrideOut,
    MCPServerOverridePut,
    MCPServerUpdate,
    MCPToolRefreshResultOut,
    OverridesGroupedOut,
    TestConnectionResult,
)
from app.services.audit_logger import write_audit_log
from app.services.mcp_client import MCPClient
from app.services.mcp_dry_run_context import _build_user_ctx, _mask_auth_headers
from app.services.mcp_refresh_service import refresh_mcp_server_tools
from app.services.mcp_server_service import compose_runtime_config, lookup_overrides
from app.services.placeholder_engine import (
    ALL_ROOTS,
    PROMPT_SAFE_ROOTS,
    DisallowedPlaceholderError,
    PlaceholderContext,
    UnknownPlaceholderError,
    render,
    render_dict,
)
from app.services.sandbox_mcp_host import SandboxMcpHost
from app.services.sandbox_mcp_hub_client import SandboxMcpHubClient

router = APIRouter(prefix="/admin/mcp-servers", tags=["mcp-admin"])
router.include_router(import_router)


from app.services.mcp_permissions import (  # noqa: E402
    assert_can_edit_server as _assert_can_edit_server_sync,
)
from app.services.mcp_permissions import (
    assert_can_patch_server as _assert_can_patch_server,
)
from app.services.mcp_permissions import (
    can_edit_server as _can_edit_server,
)
from app.services.mcp_permissions import (
    is_platform_admin as _is_platform_admin,
)


async def _assert_can_edit_server(current_user: User, server: MCPServer) -> None:
    """Async wrapper around the shared sync helper so endpoint awaits don't break."""
    _assert_can_edit_server_sync(current_user, server)


async def _assert_can_view_server_for_agent(
    current_user: User,
    server: MCPServer,
    agent_id: uuid.UUID | None,
    db: AsyncSession,
) -> Agent:
    """Allow an Agent-scoped editor to read masked server metadata.

    Reading the effective configuration is deliberately separate from editing
    the company MCP definition.  A same-tenant user may inspect a server while
    configuring an Agent, but the global PATCH endpoint remains protected by
    ``can_edit_server``.
    """

    if agent_id is None:
        raise HTTPException(status_code=403, detail="agent_id required for Agent configuration")
    return await _require_agent_server_access(
        current_user,
        agent_id,
        server,
        db,
    )


PlatformAdmin = Annotated[User, Depends(require_role("platform_admin"))]


@router.get("", response_model=list[MCPServerOut])
async def list_mcp_servers(
    current_user: PlatformAdmin,
    db: AsyncSession = Depends(get_db),
) -> list[MCPServerOut]:
    rows = (await db.execute(select(MCPServer).order_by(MCPServer.name))).scalars().all()
    return [MCPServerOut.from_orm_model(s) for s in rows]


@router.get("/{server_id}", response_model=MCPServerOut)
async def get_mcp_server(
    server_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MCPServerOut:
    """Read a masked MCP definition for global or Agent-scoped configuration."""
    srv = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if srv is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if not _can_edit_server(current_user, srv):
        await _assert_can_view_server_for_agent(current_user, srv, agent_id, db)
    return MCPServerOut.from_orm_model(srv)


@router.post("", response_model=MCPServerOut, status_code=status.HTTP_201_CREATED)
async def create_mcp_server(
    payload: MCPServerCreate,
    current_user: PlatformAdmin,
    db: AsyncSession = Depends(get_db),
) -> MCPServerOut:
    srv = MCPServer(
        tenant_id=payload.tenant_id,
        name=payload.name,
        display_name=payload.display_name,
        base_url_template=payload.base_url_template,
        headers_template=payload.headers_template,
        credential_template=payload.credential_template,  # TODO P3: envelope-encrypt
        system_prompt_block=payload.system_prompt_block,
        placeholder_allowlist=payload.placeholder_allowlist,
        created_by_user_id=current_user.id,
        transport=payload.transport,
        command_template=payload.command_template,
        args_template=payload.args_template,
        env_template=payload.env_template,
    )
    db.add(srv)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"name conflict or invalid data: {e}") from e
    await db.refresh(srv)

    await write_audit_log(
        action="MCP_SERVER_CREATE",
        details={"server_id": str(srv.id), "name": srv.name},
        user_id=current_user.id,
    )
    return MCPServerOut.from_orm_model(srv)


@router.patch("/{server_id}", response_model=MCPServerOut)
async def update_mcp_server(
    server_id: uuid.UUID,
    payload: MCPServerUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MCPServerOut:
    srv = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if srv is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    await _assert_can_patch_server(current_user, srv, db)

    diff: dict[str, dict] = {}
    update_data = normalize_masked_secret_update(
        payload.model_dump(exclude_unset=True),
        srv.headers_template,
        srv.env_template,
    )

    for field, new_value in update_data.items():
        # credential_template: None means "don't touch" per schema contract;
        # callers must send "" to explicitly clear.
        if field == "credential_template" and new_value is None:
            continue
        # placeholder_allowlist: None means "don't touch"; send [] to clear restriction.
        if field == "placeholder_allowlist" and new_value is None:
            continue
        old_value = getattr(srv, field)
        if old_value != new_value:
            # Don't include credential plaintext in audit log — log the fact, not the value
            if field in {"credential_template", "headers_template", "env_template"}:
                diff[field] = {"changed": True}
            else:
                diff[field] = {"old": old_value, "new": new_value}
            setattr(srv, field, new_value)

    await db.commit()
    await db.refresh(srv)

    if diff:
        await write_audit_log(
            action="MCP_SERVER_UPDATE",
            details={"server_id": str(srv.id), "diff": diff},
            user_id=current_user.id,
        )
    return MCPServerOut.from_orm_model(srv)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(
    server_id: uuid.UUID,
    current_user: PlatformAdmin,
    db: AsyncSession = Depends(get_db),
) -> None:
    srv = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if srv is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    name = srv.name
    tool_ids = list(
        (
            await db.execute(
                select(Tool.id).where(Tool.mcp_server_id == server_id)
            )
        ).scalars()
    )
    if tool_ids:
        await db.execute(delete(AgentTool).where(AgentTool.tool_id.in_(tool_ids)))
        await db.execute(delete(Tool).where(Tool.id.in_(tool_ids)))
    await db.delete(srv)
    await db.commit()

    await write_audit_log(
        action="MCP_SERVER_DELETE",
        details={"server_id": str(server_id), "name": name, "deleted_tools": len(tool_ids)},
        user_id=current_user.id,
    )


@router.post("/{server_id}/test-connection", response_model=TestConnectionResult)
async def test_mcp_server_connection(
    server_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TestConnectionResult:
    srv = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if srv is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    await _assert_can_edit_server(current_user, srv)

    # Transport-aware routing: stdio → aio-sandbox hub; http → MCPClient (unchanged).
    transport = getattr(srv, "transport", "http") or "http"
    try:
        if transport == "stdio":
            from app.config import get_settings as _get_settings
            _s = _get_settings()
            if not _s.SANDBOX_API_URL:
                return TestConnectionResult(
                    success=False,
                    error="stdio MCP unavailable — SANDBOX_API_URL not configured",
                )
            # Discovery context: render templates with on_unknown="keep_literal" so
            # missing agent-scoped placeholders don't block admin discovery.
            # env may contain ${agent.*} tokens — keep them as literals; the hub
            # will still start the process (token auth will fail, but listing tools
            # only needs the process to boot).
            ctx = _SYNTHETIC_CTX
            try:
                r_cmd = render(
                    getattr(srv, "command_template", None) or "",
                    ctx, ALL_ROOTS, on_unknown="keep_literal",
                )
                r_args = [
                    render(a, ctx, ALL_ROOTS, on_unknown="keep_literal")
                    for a in (getattr(srv, "args_template", None) or [])
                ]
                r_env = render_dict(
                    getattr(srv, "env_template", None) or {},
                    ctx, ALL_ROOTS, on_unknown="keep_literal",
                )
            except (DisallowedPlaceholderError, UnknownPlaceholderError) as e:
                return TestConnectionResult(success=False, error=f"stdio placeholder error — {e}")

            host = SandboxMcpHost(_s.SANDBOX_API_URL, _s.SANDBOX_API_KEY)
            entry = await host.ensure_registered(
                srv.name, "__discovery__",
                {"command": r_cmd, "args": r_args, "env": r_env},
            )
            hub = SandboxMcpHubClient(_s.SANDBOX_API_URL, _s.SANDBOX_API_KEY)
            try:
                tools = await hub.list_tools(entry)
                tool_count = len(tools)
            finally:
                # Best-effort cleanup: remove the __discovery__ hub entry so it
                # doesn't linger.  Swallow deregister errors — cleanup failure
                # must not mask the test-connection result.
                try:
                    await host.deregister(entry)
                except Exception:  # noqa: BLE001
                    pass

            # Persist discovered tools as Tool rows (idempotent upsert).
            # Mirrors the HTTP MCP flow so stdio tools are assignable to agents.
            from app.services.mcp_server_service import persist_stdio_discovered_tools
            await persist_stdio_discovered_tools(db, srv, tools)

            # Persist discovery metadata
            srv.instructions = f"stdio MCP server; {tool_count} tools discovered via hub entry {entry}"
            srv.instructions_captured_at = datetime.now(timezone.utc)
            await db.commit()

            await write_audit_log(
                action="MCP_SERVER_TEST_CONNECTION",
                details={"server_id": str(server_id), "ok": True, "transport": "stdio", "tool_count": tool_count},
                user_id=current_user.id,
            )
            return TestConnectionResult(
                success=True,
                instructions=srv.instructions,
                server_info={"transport": "stdio", "hub_entry": entry, "tool_count": tool_count},
            )
        else:
            # NOTE: At this point base_url_template / headers_template / credential_template
            # may contain ${user.email} etc. — P3 will resolve those. For P1, we treat them
            # as literals (test-connection from admin context has no user identity).
            client = MCPClient(
                server_url=srv.base_url_template,
                api_key=srv.credential_template,  # may be None
                headers=srv.headers_template,
            )
            await client.list_tools()
            # Capture & persist
            srv.instructions = client.server_instructions
            srv.instructions_captured_at = datetime.now(timezone.utc)
            await db.commit()

            await write_audit_log(
                action="MCP_SERVER_TEST_CONNECTION",
                details={"server_id": str(server_id), "ok": True},
                user_id=current_user.id,
            )
            return TestConnectionResult(
                success=True,
                instructions=client.server_instructions,
                server_info=client.server_info,
            )
    except Exception as e:  # noqa: BLE001 — we want to surface anything to the admin
        await write_audit_log(
            action="MCP_SERVER_TEST_CONNECTION",
            details={"server_id": str(server_id), "ok": False, "error": str(e)[:200]},
            user_id=current_user.id,
        )
        return TestConnectionResult(success=False, error=str(e))


# ---------------------------------------------------------------------------
# Override CRUD helpers
# ---------------------------------------------------------------------------


async def _require_tenant_override_access(
    current_user: User, scope_id: uuid.UUID,
) -> None:
    """Platform admin OR org_admin of the given tenant."""
    is_platform = (current_user.role == "platform_admin"
                   or (current_user.identity and current_user.identity.is_platform_admin))
    if is_platform:
        return
    if current_user.role == "org_admin" and current_user.tenant_id == scope_id:
        return
    raise HTTPException(status_code=403, detail="not authorized for this tenant override")


async def _require_agent_override_access(
    current_user: User, agent_id: uuid.UUID, db: AsyncSession,
) -> Agent:
    """Require authority to mutate one Agent's override, including project ACL."""
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if _is_platform_admin(current_user):
        return agent
    if agent.scope == "project" and agent.project_id is not None:
        from app.services.project_service import require_project

        await require_project(db, current_user, agent.project_id, edit=True)
        return agent
    if agent.creator_id == current_user.id:
        return agent
    # Same-tenant admin: can manage all agent overrides in their tenant
    if (
        current_user.role in ("org_admin", "agent_admin")
        and agent.tenant_id is not None
        and current_user.tenant_id == agent.tenant_id
    ):
        return agent
    raise HTTPException(status_code=403, detail="not authorized for this agent override")


async def _require_agent_server_access(
    current_user: User,
    agent_id: uuid.UUID,
    server: MCPServer,
    db: AsyncSession,
) -> Agent:
    """Authorize one Agent-scoped MCP view or override without global edit authority."""
    agent = await _require_agent_override_access(current_user, agent_id, db)
    if server.tenant_id is not None and server.tenant_id != agent.tenant_id:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if agent.scope != "project":
        return agent

    assigned_tool_id = await db.scalar(
        select(AgentTool.id)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .where(
            AgentTool.agent_id == agent.id,
            Tool.type == "mcp",
            Tool.mcp_server_id == server.id,
        )
        .limit(1)
    )
    if assigned_tool_id is None:
        # Project configuration may only override MCP servers already copied
        # into this project's isolated Digital Employee snapshot.
        raise HTTPException(status_code=404, detail="MCP server not found")
    return agent


@router.post("/{server_id}/refresh-tools", response_model=MCPToolRefreshResultOut)
async def refresh_mcp_server_tool_catalog(
    server_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_id: uuid.UUID | None = None,
) -> MCPToolRefreshResultOut:
    """Refresh globally, or refresh an Agent's exclusively self-installed server."""
    server = (
        await db.execute(select(MCPServer).where(MCPServer.id == server_id))
    ).scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")

    if agent_id is None:
        await _assert_can_edit_server(current_user, server)
    else:
        agent = await _require_agent_server_access(current_user, agent_id, server, db)
        if agent.scope == "project":
            raise HTTPException(
                status_code=403,
                detail="Project MCP configuration cannot refresh the shared tool catalog",
            )

    try:
        result = await refresh_mcp_server_tools(
            db,
            server_id,
            agent_id=agent_id,
            user_id=current_user.id,
            assign_to_agent=agent_id is not None,
        )
        await db.commit()
    except (LookupError, PermissionError) as exc:
        await db.rollback()
        status_code = 404 if isinstance(exc, LookupError) else 403
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except Exception as exc:
        await db.rollback()
        await write_audit_log(
            action="MCP_SERVER_REFRESH_TOOLS",
            details={
                "server_id": str(server_id),
                "agent_id": str(agent_id) if agent_id else None,
                "ok": False,
                "error": str(exc)[:200],
            },
            user_id=current_user.id,
        )
        raise HTTPException(status_code=502, detail=f"MCP tool refresh failed: {exc}") from exc

    await write_audit_log(
        action="MCP_SERVER_REFRESH_TOOLS",
        details={
            "server_id": str(server_id),
            "agent_id": str(agent_id) if agent_id else None,
            "ok": True,
            **result.to_dict(),
        },
        user_id=current_user.id,
    )
    return MCPToolRefreshResultOut(
        success=True,
        **result.to_dict(),
        effective="next_turn",
    )


async def _upsert_override(
    db: AsyncSession, server_id: uuid.UUID, scope_type: str,
    scope_id: uuid.UUID, payload: MCPServerOverridePut, user_id: uuid.UUID,
) -> MCPServerOverride:
    existing = (await db.execute(
        select(MCPServerOverride).where(
            MCPServerOverride.mcp_server_id == server_id,
            MCPServerOverride.scope_type == scope_type,
            MCPServerOverride.scope_id == scope_id,
        )
    )).scalar_one_or_none()

    update_data = payload.model_dump(exclude_unset=True)
    # credential_template: None means "don't touch" (mirrors server PATCH semantics)
    if "credential_template" in update_data and update_data["credential_template"] is None:
        update_data.pop("credential_template")
    update_data = normalize_masked_secret_update(
        update_data,
        existing.headers_template if existing is not None else None,
        existing.env_template if existing is not None else None,
    )

    if existing is None:
        ovr = MCPServerOverride(
            mcp_server_id=server_id, scope_type=scope_type, scope_id=scope_id,
            last_modified_by_user_id=user_id,
            **update_data,
        )
        db.add(ovr)
    else:
        for f, v in update_data.items():
            setattr(existing, f, v)
        existing.last_modified_by_user_id = user_id
        ovr = existing
    await db.commit()
    await db.refresh(ovr)
    return ovr


# ---------------------------------------------------------------------------
# Override endpoints
# ---------------------------------------------------------------------------


@router.get("/{server_id}/overrides", response_model=OverridesGroupedOut)
async def list_mcp_overrides(
    server_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_id: uuid.UUID | None = None,
) -> OverridesGroupedOut:
    """Platform admin: returns all overrides (all scopes).
    Non-admin with agent_id: returns only that agent's overrides (after creator check).
    Non-admin without agent_id: 403.
    """
    is_platform = _is_platform_admin(current_user)
    if not is_platform:
        if agent_id is None:
            raise HTTPException(status_code=403, detail="agent_id required for non-admin callers")
        server = (
            await db.execute(select(MCPServer).where(MCPServer.id == server_id))
        ).scalar_one_or_none()
        if server is None:
            raise HTTPException(status_code=404, detail="MCP server not found")
        await _require_agent_server_access(current_user, agent_id, server, db)
        # Return only the specific agent's overrides — no tenant rows visible to agent admins
        rows = (await db.execute(
            select(MCPServerOverride).where(
                MCPServerOverride.mcp_server_id == server_id,
                MCPServerOverride.scope_type == "agent",
                MCPServerOverride.scope_id == agent_id,
            )
        )).scalars().all()
        grouped = OverridesGroupedOut()
        for r in rows:
            grouped.agent.append(MCPServerOverrideOut.from_orm_model(r))
        return grouped

    rows = (await db.execute(
        select(MCPServerOverride).where(MCPServerOverride.mcp_server_id == server_id)
    )).scalars().all()
    grouped = OverridesGroupedOut()
    for r in rows:
        target_list = grouped.tenant if r.scope_type == "tenant" else grouped.agent
        target_list.append(MCPServerOverrideOut.from_orm_model(r))
    return grouped


@router.put("/{server_id}/overrides/tenant/{tenant_id}", response_model=MCPServerOverrideOut)
async def put_tenant_override(
    server_id: uuid.UUID,
    tenant_id: uuid.UUID,
    payload: MCPServerOverridePut,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MCPServerOverrideOut:
    await _require_tenant_override_access(current_user, tenant_id)
    if (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    ovr = await _upsert_override(db, server_id, "tenant", tenant_id, payload, current_user.id)
    await write_audit_log(
        action="MCP_SERVER_OVERRIDE_UPSERT",
        details={"server_id": str(server_id), "scope_type": "tenant", "scope_id": str(tenant_id)},
        user_id=current_user.id,
    )
    return MCPServerOverrideOut.from_orm_model(ovr)


@router.delete("/{server_id}/overrides/tenant/{tenant_id}", status_code=204)
async def delete_tenant_override(
    server_id: uuid.UUID,
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _require_tenant_override_access(current_user, tenant_id)
    rows = (await db.execute(
        select(MCPServerOverride).where(
            MCPServerOverride.mcp_server_id == server_id,
            MCPServerOverride.scope_type == "tenant",
            MCPServerOverride.scope_id == tenant_id,
        )
    )).scalars().all()
    for r in rows:
        await db.delete(r)
    await db.commit()
    await write_audit_log(
        action="MCP_SERVER_OVERRIDE_DELETE",
        details={"server_id": str(server_id), "scope_type": "tenant", "scope_id": str(tenant_id)},
        user_id=current_user.id,
    )


@router.put("/{server_id}/overrides/agent/{agent_id}", response_model=MCPServerOverrideOut)
async def put_agent_override(
    server_id: uuid.UUID,
    agent_id: uuid.UUID,
    payload: MCPServerOverridePut,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MCPServerOverrideOut:
    server = (
        await db.execute(select(MCPServer).where(MCPServer.id == server_id))
    ).scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    await _require_agent_server_access(current_user, agent_id, server, db)
    ovr = await _upsert_override(db, server_id, "agent", agent_id, payload, current_user.id)
    await write_audit_log(
        action="MCP_SERVER_OVERRIDE_UPSERT",
        details={"server_id": str(server_id), "scope_type": "agent", "scope_id": str(agent_id)},
        user_id=current_user.id,
    )
    return MCPServerOverrideOut.from_orm_model(ovr)


@router.delete("/{server_id}/overrides/agent/{agent_id}", status_code=204)
async def delete_agent_override(
    server_id: uuid.UUID,
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    server = (
        await db.execute(select(MCPServer).where(MCPServer.id == server_id))
    ).scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    await _require_agent_server_access(current_user, agent_id, server, db)
    rows = (await db.execute(
        select(MCPServerOverride).where(
            MCPServerOverride.mcp_server_id == server_id,
            MCPServerOverride.scope_type == "agent",
            MCPServerOverride.scope_id == agent_id,
        )
    )).scalars().all()
    for r in rows:
        await db.delete(r)
    await db.commit()
    await write_audit_log(
        action="MCP_SERVER_OVERRIDE_DELETE",
        details={"server_id": str(server_id), "scope_type": "agent", "scope_id": str(agent_id)},
        user_id=current_user.id,
    )


# ---------------------------------------------------------------------------
# Dry-run endpoint
# ---------------------------------------------------------------------------

_SYNTHETIC_CTX = PlaceholderContext(
    user={"id": "00000000-0000-0000-0000-000000000000", "email": "preview@example.local",
          "phone": "0000000000", "name": "Preview User", "display_name": "Preview"},
    agent={"id": "00000000-0000-0000-0000-000000000000", "name": "PreviewAgent",
           "slug": "preview"},
    tenant={"id": "00000000-0000-0000-0000-000000000000"},
    session={"id": "00000000-0000-0000-0000-000000000000"},
    channel={"type": "web"},
)


@router.post("/{server_id}/dry-run", response_model=DryRunResponse)
async def dry_run_mcp_server(
    server_id: uuid.UUID,
    payload: DryRunRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DryRunResponse:
    srv = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if srv is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if not _can_edit_server(current_user, srv):
        if payload.scope != "agent":
            raise HTTPException(status_code=403, detail="Agent scope required for project configuration")
        await _assert_can_view_server_for_agent(current_user, srv, payload.agent_id, db)

    # Determine layers + lookup overrides
    used: list[str] = ["platform"]
    # If caller omits tenant_id, infer it: prefer the server's tenant_id,
    # falling back to the caller's. This lets the unified editor preview
    # without redundantly sending tenant context the server already knows.
    effective_tenant_id = payload.tenant_id or srv.tenant_id or current_user.tenant_id
    if payload.scope in ("tenant", "agent"):
        if effective_tenant_id is None:
            raise HTTPException(status_code=400, detail="tenant_id required for scope=tenant|agent")
    if payload.scope == "agent" and payload.agent_id is None:
        raise HTTPException(status_code=400, detail="agent_id required for scope=agent")

    t_ovr, a_ovr = await lookup_overrides(
        db, server_id,
        effective_tenant_id if payload.scope in ("tenant", "agent") else None,
        payload.agent_id if payload.scope == "agent" else None,
    )
    if t_ovr:
        used.append("tenant")
    if a_ovr:
        used.append("agent")

    cfg = compose_runtime_config(srv, t_ovr, a_ovr)

    # Apply unsaved draft overrides (highest priority, transient). Semantic:
    # url/headers/credential — draft REPLACES the composed value entirely.
    # prompt — draft APPENDS as the final prompt block (visible alongside the
    # composed stack, so the user sees "what gets added on top of what exists").
    if payload.draft_overrides is not None:
        from dataclasses import replace
        d = payload.draft_overrides
        new_prompts = cfg.prompt_blocks
        if d.system_prompt_block is not None and d.system_prompt_block.strip():
            new_prompts = cfg.prompt_blocks + [d.system_prompt_block]
        cfg = replace(
            cfg,
            url_template=d.base_url_template if d.base_url_template is not None else cfg.url_template,
            headers_template=d.headers_template if d.headers_template is not None else cfg.headers_template,
            credential_template=d.credential_template if d.credential_template is not None else cfg.credential_template,
            prompt_blocks=new_prompts,
        )
        if "draft" not in used:
            used.append("draft")

    # Build context
    if payload.identity == "synthetic":
        ctx = _SYNTHETIC_CTX
    else:
        ctx = await _build_user_ctx(db, current_user, payload.agent_id, effective_tenant_id)

    errors: list[str] = []

    def _safe(template: str, allowed: frozenset[str]) -> str:
        try:
            return render(template, ctx, allowed, on_unknown="keep_literal")
        except DisallowedPlaceholderError as e:
            errors.append(str(e))
            return template
        except UnknownPlaceholderError as e:
            errors.append(str(e))
            return template

    resolved_url = _safe(cfg.url_template, ALL_ROOTS)
    resolved_headers = render_dict(cfg.headers_template or {}, ctx,
                                   allowed_roots=ALL_ROOTS, on_unknown="keep_literal")
    resolved_headers = _mask_auth_headers(resolved_headers)
    # Prompt is rendered with PROMPT_SAFE_ROOTS — ${user.*} would error
    resolved_prompt = _safe("\n\n".join(cfg.prompt_blocks), PROMPT_SAFE_ROOTS)

    return DryRunResponse(
        resolved_url=resolved_url,
        resolved_headers=resolved_headers,
        resolved_credential_state="set" if (cfg.credential_template or "").strip() else "unset",
        resolved_prompt=resolved_prompt,
        used_layers=used,  # type: ignore[arg-type]
        errors=errors,
    )
