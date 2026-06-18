"""MCP agent channel (IM) config tools: get/set/delete per-agent channel configuration.

All three tools require:
- write-scope PAT
- caller is the agent's creator OR has platform_admin / org_admin role
- set/delete are confirm-guided (secrets are sensitive)
- secrets are encrypted at rest and NEVER echoed in output
"""
from __future__ import annotations

from mcp.server.fastmcp import Context
from sqlalchemy import delete, select

from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server._common import authed_write, needs_confirm, resolve_manageable_agent

_ADMIN_ROLES = ("platform_admin", "org_admin")

_CREATOR_ONLY_MSG = (
    "❌ 仅创建者或管理员可配置渠道（需要 creator / platform_admin / org_admin）。"
    "请以 agent 创建者身份或 platform_admin/org_admin 角色重试，或联系管理员操作。"
)


def _check_creator_or_admin(pc, ag) -> str | None:
    """Return an error string if caller is not creator/admin, else None."""
    from app.core.permissions import is_agent_creator

    if is_agent_creator(pc.user, ag) or pc.user.role in _ADMIN_ROLES:
        return None
    return _CREATOR_ONLY_MSG


# ── impl functions ──────────────────────────────────────────────────────────


async def get_agent_channel_config_impl(ctx, agent: str, channel: str) -> str:
    """Load channel config for the given agent+channel; mask secrets in output."""
    from app.models.channel_config import ChannelConfig
    from app.services.tool_config import decrypt_sensitive_fields

    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        err = _check_creator_or_admin(pc, ag)
        if err:
            return err

        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == ag.id,
                ChannelConfig.channel_type == channel,
            )
        )
        config = result.scalar_one_or_none()

    if config is None:
        return (
            f"（该渠道未配置）agent=「{ag.name}」 channel={channel}。"
            f"请使用 set_agent_channel_config(agent=..., channel={channel!r}, config={{...}}) 配置后再查询。"
        )

    # Build a display dict — decrypt so we can describe keys, then mask secrets
    full = {
        "api_key": config.app_secret,
        **(config.extra_config or {}),
    }
    decrypted = decrypt_sensitive_fields(full)
    # Remove None values
    decrypted = {k: v for k, v in decrypted.items() if v is not None}

    _SENSITIVE = {"api_key", "api_secret", "app_secret", "password", "token", "secret"}
    masked: dict = {}
    for k, v in decrypted.items():
        if k in _SENSITIVE or "secret" in k or "key" in k or "token" in k or "password" in k:
            masked[k] = "******"
        else:
            masked[k] = v

    lines = [f"渠道配置 · agent=「{ag.name}」 channel={channel}", f"is_configured: {config.is_configured}"]
    for k, v in masked.items():
        lines.append(f"  {k}: {v}")
    lines.append("（已配置 · 密钥已脱敏显示）")
    return "\n".join(lines)


async def set_agent_channel_config_impl(
    ctx, agent: str, channel: str, config: dict, confirm: bool = False
) -> str:
    """Upsert channel config for agent; encrypts secrets; confirm-guided."""
    from app.models.channel_config import ChannelConfig
    from app.services.tool_config import encrypt_sensitive_fields

    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        err = _check_creator_or_admin(pc, ag)
        if err:
            return err

        guidance = needs_confirm(confirm, f"将配置「{ag.name}」的 {channel} 渠道（含密钥）")
        if guidance:
            return guidance

        # Mirror update_category_config: encrypt → split app_secret vs extra
        encrypted = encrypt_sensitive_fields(config)
        app_secret = (
            encrypted.get("api_key")
            or encrypted.get("api_secret")
            or encrypted.get("app_secret")
        )
        extra = {k: v for k, v in encrypted.items() if k not in ("api_key", "api_secret", "app_secret")}

        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == ag.id,
                ChannelConfig.channel_type == channel,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            if app_secret:
                existing.app_secret = app_secret
            existing.extra_config = {**(existing.extra_config or {}), **extra}
            existing.is_configured = True
        else:
            db.add(
                ChannelConfig(
                    agent_id=ag.id,
                    channel_type=channel,
                    app_id=channel,
                    app_secret=app_secret,
                    extra_config=extra,
                    is_configured=True,
                )
            )

        await db.commit()

    # Atlassian special-case: trigger async sync (mirrors update_category_config)
    if channel == "atlassian":
        import asyncio

        from app.api.atlassian import _sync_atlassian_tools_for_agent

        plaintext_key = (
            config.get("api_key") or config.get("api_secret") or config.get("app_secret")
        )
        asyncio.create_task(_sync_atlassian_tools_for_agent(ag.id, plaintext_key))

    return (
        f"✅ 已保存「{ag.name}」的 {channel} 渠道配置（密钥已加密存储）。\n"
        f"↩ 回滚：delete_agent_channel_config(agent={agent!r}, channel={channel!r}, confirm=True)"
    )


async def delete_agent_channel_config_impl(
    ctx, agent: str, channel: str, confirm: bool = False
) -> str:
    """Delete channel config for agent; confirm-guided."""
    from app.models.channel_config import ChannelConfig

    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        err = _check_creator_or_admin(pc, ag)
        if err:
            return err

        guidance = needs_confirm(confirm, f"将删除「{ag.name}」的 {channel} 渠道配置（不可逆）")
        if guidance:
            return guidance

        await db.execute(
            delete(ChannelConfig).where(
                ChannelConfig.agent_id == ag.id,
                ChannelConfig.channel_type == channel,
            )
        )
        await db.commit()

    return (
        f"✅ 已删除「{ag.name}」的 {channel} 渠道配置。\n"
        f"注：如需重新配置，请使用 set_agent_channel_config。"
    )


# ── MCP tool registrations ──────────────────────────────────────────────────


@mcp.tool()
async def get_agent_channel_config(ctx: Context, agent: str, channel: str) -> str:
    """获取 agent 的渠道（IM）配置摘要。

    - 仅 creator / platform_admin / org_admin 可调用
    - 密钥字段（app_secret / api_key 等）已脱敏，显示为 ******
    - channel: 渠道类型，如 "slack" / "feishu" / "dingtalk" / "wecom" 等

    Args:
        agent: Agent UUID 或名称
        channel: 渠道类型（channel_type），如 "slack"
    """
    return await get_agent_channel_config_impl(ctx, agent=agent, channel=channel)


@mcp.tool()
async def set_agent_channel_config(
    ctx: Context, agent: str, channel: str, config: dict, confirm: bool = False
) -> str:
    """设置 agent 的渠道（IM）配置（含密钥）。

    - 仅 creator / platform_admin / org_admin 可调用（write PAT 必须）
    - 密钥字段加密存储，输出不回显明文
    - 敏感操作：首次调用返回确认引导，加 confirm=True 执行
    - channel: 渠道类型，如 "slack" / "feishu" / "dingtalk"
    - config: 配置字典，例如 {"app_secret": "xxx", "app_id": "yyy"}

    Args:
        agent: Agent UUID 或名称
        channel: 渠道类型
        config: 配置字典（含密钥等敏感字段）
        confirm: 二次确认标志（默认 False，需显式传 True 执行）
    """
    return await set_agent_channel_config_impl(ctx, agent=agent, channel=channel, config=config, confirm=confirm)


@mcp.tool()
async def delete_agent_channel_config(
    ctx: Context, agent: str, channel: str, confirm: bool = False
) -> str:
    """删除 agent 的渠道（IM）配置。

    - 仅 creator / platform_admin / org_admin 可调用（write PAT 必须）
    - 敏感操作：首次调用返回确认引导，加 confirm=True 执行
    - 删除后渠道配置清空，需重新配置才能重连

    Args:
        agent: Agent UUID 或名称
        channel: 渠道类型，如 "slack"
        confirm: 二次确认标志（默认 False，需显式传 True 执行）
    """
    return await delete_agent_channel_config_impl(ctx, agent=agent, channel=channel, confirm=confirm)
