"""MCP agent credentials tools (write scope, MANAGE required).

Provides list / set / delete operations for per-agent encrypted credentials.
``cookies_json`` is SENSITIVE: encrypted at rest via AES-256-CBC and is NEVER
echoed in any response. Create and delete are confirm-guided.
"""
from __future__ import annotations

import json
import uuid as _uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import Context
from sqlalchemy import select

from app.config import get_settings
from app.core.security import encrypt_data
from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server._common import authed_write, needs_confirm, resolve_manageable_agent
from app.models.agent_credential import AgentCredential


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_row(cred: AgentCredential) -> str:
    """Format a single AgentCredential as a safe summary line (no cookies_json)."""
    has = bool(cred.cookies_json)
    updated = cred.cookies_updated_at.isoformat() if cred.cookies_updated_at else "—"
    return (
        f"  id={cred.id}  type={cred.credential_type}  platform={cred.platform}"
        f"  display_name={cred.display_name or ''}  status={cred.status}"
        f"  has_cookies={has}  cookies_updated={updated}"
    )


def _validate_cookies_json(cookies_json: str) -> str | None:
    """Return an error string if cookies_json is not a valid JSON array, else None."""
    try:
        parsed = json.loads(cookies_json)
        if not isinstance(parsed, list):
            return "cookies_json 必须是 JSON 数组（array），例如 [{\"name\":\"...\",\"value\":\"...\"}]。"
    except json.JSONDecodeError as exc:
        return f"cookies_json JSON 格式有误：{exc}"
    return None


# ── impl functions ────────────────────────────────────────────────────────────

async def list_agent_credentials_impl(ctx, agent: str) -> str:
    """List credentials for an agent (write scope + manage). cookies_json never returned."""
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err

        result = await db.execute(
            select(AgentCredential)
            .where(AgentCredential.agent_id == ag.id)
            .order_by(AgentCredential.created_at.desc())
        )
        creds = result.scalars().all()

    if not creds:
        return f"「{ag.name}」暂无凭证记录。"

    lines = [f"「{ag.name}」凭证列表（共 {len(creds)} 条，cookies 已隐藏）："]
    for c in creds:
        lines.append(_safe_row(c))
    return "\n".join(lines)


async def set_agent_credential_impl(
    ctx,
    agent: str,
    credential_type: str,
    platform: str,
    display_name: str = "",
    cookies_json: str | None = None,
    credential_id: str | None = None,
    confirm: bool = False,
) -> str:
    """Create or update a credential (write scope + manage, confirm-guided).

    If credential_id is provided, updates that existing record; otherwise creates new.
    cookies_json (optional) must be a JSON array of Playwright-compatible cookie objects.
    The value is encrypted before storage and is NEVER returned.
    """
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err

        action_verb = "更新" if credential_id else "创建"
        guidance = needs_confirm(
            confirm,
            f"将{action_verb}「{ag.name}」的凭证（platform={platform}）— 含注入的密钥",
        )
        if guidance:
            return guidance

        # Validate cookies_json if provided
        if cookies_json is not None:
            err_msg = _validate_cookies_json(cookies_json)
            if err_msg:
                return f"❌ {err_msg}"

        settings = get_settings()

        if credential_id is not None:
            # UPDATE path
            try:
                cid = _uuid.UUID(str(credential_id))
            except (ValueError, TypeError):
                return (
                    f"❌ credential_id 格式无效（你传入了 {credential_id!r}，需为 UUID）。"
                    "请用 list_agent_credentials 查看该 agent 的凭证列表及其 id。"
                )

            result = await db.execute(
                select(AgentCredential).where(
                    AgentCredential.id == cid,
                    AgentCredential.agent_id == ag.id,
                )
            )
            cred = result.scalar_one_or_none()
            if cred is None:
                return (
                    f"❌ 找不到凭证 {credential_id}（或不属于该 agent）。"
                    "用 list_agent_credentials 查看该 agent 的凭证列表，并以正确 id 重试。"
                )

            # Update provided fields
            cred.credential_type = credential_type
            cred.platform = platform
            if display_name:
                cred.display_name = display_name
            if cookies_json is not None:
                cred.cookies_json = encrypt_data(cookies_json, settings.SECRET_KEY)
                cred.cookies_updated_at = datetime.now(timezone.utc)
                cred.status = "active"

            await db.commit()
            await db.refresh(cred)
            return (
                f"✅ 已更新凭证 {cred.id}（{ag.name} / {platform}）。\n"
                f"注：原凭证已覆盖，如需回退请通过 set_agent_credential 重新提供原始 cookies_json。"
            )

        else:
            # CREATE path
            cred = AgentCredential(
                agent_id=ag.id,
                credential_type=credential_type,
                platform=platform,
                display_name=display_name or "",
                status="active",
            )
            if cookies_json is not None:
                cred.cookies_json = encrypt_data(cookies_json, settings.SECRET_KEY)
                cred.cookies_updated_at = datetime.now(timezone.utc)

            db.add(cred)
            await db.commit()
            await db.refresh(cred)
            return (
                f"✅ 已创建凭证 {cred.id}（{ag.name} / {platform}）。\n"
                f"如需撤销，请调用 delete_agent_credential(agent={agent!r}, credential_id={str(cred.id)!r}, confirm=True)。\n"
                f"注：cookies_json 已加密存储，不会回显。"
            )


async def delete_agent_credential_impl(
    ctx,
    agent: str,
    credential_id: str,
    confirm: bool = False,
) -> str:
    """Delete a credential (write scope + manage, confirm-guided).

    The secret value is not echoed — if a rollback is needed, re-supply it via
    set_agent_credential.
    """
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err

        guidance = needs_confirm(confirm, f"将删除「{ag.name}」的凭证 {credential_id}")
        if guidance:
            return guidance

        try:
            cid = _uuid.UUID(str(credential_id))
        except (ValueError, TypeError):
            return (
                f"❌ credential_id 格式无效（你传入了 {credential_id!r}，需为 UUID）。"
                "请用 list_agent_credentials 查看该 agent 的凭证列表及其 id。"
            )

        result = await db.execute(
            select(AgentCredential).where(
                AgentCredential.id == cid,
                AgentCredential.agent_id == ag.id,
            )
        )
        cred = result.scalar_one_or_none()
        if cred is None:
            return (
                f"❌ 找不到凭证 {credential_id}（或不属于该 agent）。"
                "用 list_agent_credentials 查看该 agent 的凭证列表，并以正确 id 重试。"
            )

        platform = cred.platform
        await db.delete(cred)
        await db.commit()

    return (
        f"✅ 已删除凭证 {credential_id}（{ag.name} / {platform}）。\n"
        f"如需回滚，请通过 set_agent_credential 重新创建并提供原始 cookies_json（该值从未被回显，需重新获取）。"
    )


# ── MCP tool wrappers ─────────────────────────────────────────────────────────

@mcp.tool()
async def list_agent_credentials(ctx: Context, agent: str) -> str:  # noqa: D401
    """List credentials for an agent (write scope + MANAGE required).

    Returns id, credential_type, platform, display_name, status, has_cookies flag, and
    timestamps for each credential. cookies_json is NEVER included in the response.
    agent: agent id or name.
    """
    return await list_agent_credentials_impl(ctx, agent=agent)


@mcp.tool()
async def set_agent_credential(  # noqa: D401
    ctx: Context,
    agent: str,
    credential_type: str,
    platform: str,
    display_name: str = "",
    cookies_json: str | None = None,
    credential_id: str | None = None,
    confirm: bool = False,
) -> str:
    """Create or update an agent credential (write scope + MANAGE required, confirm-guided).

    Provide credential_id to update an existing record; omit to create a new one.
    cookies_json must be a JSON array of Playwright-compatible cookie objects if supplied.
    The value is encrypted at rest and is NEVER echoed back.
    This is a SENSITIVE operation: first call returns guidance; re-call with confirm=True to execute.
    """
    return await set_agent_credential_impl(
        ctx,
        agent=agent,
        credential_type=credential_type,
        platform=platform,
        display_name=display_name,
        cookies_json=cookies_json,
        credential_id=credential_id,
        confirm=confirm,
    )


@mcp.tool()
async def delete_agent_credential(ctx: Context, agent: str, credential_id: str, confirm: bool = False) -> str:  # noqa: D401
    """Delete an agent credential (write scope + MANAGE required, confirm-guided).

    This is a SENSITIVE operation: first call returns guidance; re-call with confirm=True to execute.
    The secret (cookies_json) is never echoed — to rollback you must re-supply it via set_agent_credential.
    agent: agent id or name. credential_id: UUID of the credential to delete.
    """
    return await delete_agent_credential_impl(ctx, agent=agent, credential_id=credential_id, confirm=confirm)
