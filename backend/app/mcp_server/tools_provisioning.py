"""MCP provisioning tools (write scope): create_agent, update_agent."""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone as _tz

from mcp.server.fastmcp import Context
from sqlalchemy import or_, select

from app.database import async_session
from app.core.permissions import is_platform_admin_user
from app.mcp_server import mcp
from app.mcp_server._common import authed_write, resolve_manageable_agent
from app.models.llm import LLMModel
from app.services.model_capabilities import purpose_clause
from app.services.agent_provisioning import AgentProvisionInput, provision_agent
from app.services.agent_settings_update import apply_agent_settings_patch, public_setting_name
from app.services.quota_guard import QuotaExceeded

_VALID_ACCESS = {"company", "private", "custom"}
_CLEARABLE_SETTINGS = frozenset({
    "avatar_url",
    "bio",
    "fallback_model",
    "imagination",
    "reasoning_effort",
    "max_tokens_per_day",
    "max_tokens_per_month",
    "primary_model",
    "timezone",
    "welcome_message",
})


async def _resolve_model_id(db, tenant_id, ref):
    """Resolve a model ref (UUID or label) to LLMModel.id within the tenant, or None."""
    if not ref:
        return None
    base = select(LLMModel).where(
        LLMModel.enabled == True,  # noqa: E712
                purpose_clause(),
        or_(LLMModel.tenant_id == tenant_id, LLMModel.tenant_id.is_(None)),
    )
    try:
        mid = _uuid.UUID(str(ref))
        row = (await db.execute(base.where(LLMModel.id == mid))).scalar_one_or_none()
        if row:
            return row.id
    except (ValueError, TypeError):
        pass
    rows = (await db.execute(base.where(LLMModel.label == ref))).scalars().all()
    return rows[0].id if len(rows) == 1 else None


async def create_agent_impl(
    ctx,
    name,
    role_description="",
    personality="",
    boundaries="",
    bio="",
    primary_model=None,
    fallback_model=None,
    access_mode="company",
    autonomy_policy=None,
    avatar_url=None,
    max_tokens_per_day=None,
    max_tokens_per_month=None,
    template_id=None,
    skill_ids=None,
    grant_user_ids=None,
    access_level="use",
) -> str:
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        if access_mode not in _VALID_ACCESS:
            return (
                f"❌ access_mode 取值非法（你传入了 {access_mode!r}，可选：{', '.join(sorted(_VALID_ACCESS))}）。"
                "请修正后重试。"
            )
        if not name or len(name) < 2:
            return "❌ name 太短（至少 2 个字符）：请提供 2 个字符以上的名字。"
        pm = await _resolve_model_id(db, pc.tenant_id, primary_model)
        if primary_model and pm is None:
            return (
                f"❌ 找不到 primary_model（你传入了 {primary_model!r}）。"
                "用 list_models 查看可用模型，并以其 id 或 label 重试。"
            )
        fm = await _resolve_model_id(db, pc.tenant_id, fallback_model)
        if fallback_model and fm is None:
            return (
                f"❌ 找不到 fallback_model（你传入了 {fallback_model!r}）。"
                "用 list_models 查看可用模型，并以其 id 或 label 重试。"
            )

        # Map access_mode to permission_scope_type
        if access_mode == "private":
            scope_type = "user"
            scope_ids = []
        elif access_mode == "custom":
            scope_type = "custom"
            # Parse grant_user_ids UUIDs
            scope_ids = []
            for uid_str in (grant_user_ids or []):
                try:
                    scope_ids.append(_uuid.UUID(str(uid_str)))
                except (ValueError, TypeError):
                    return f"❌ grant_user_ids 中包含无效 UUID：{uid_str!r}（需为标准 UUID 格式，如 550e8400-e29b-41d4-a716-446655440000）。"
        else:
            scope_type = "company"
            scope_ids = []

        # Parse template_id
        parsed_template_id = None
        if template_id:
            try:
                parsed_template_id = _uuid.UUID(str(template_id))
            except (ValueError, TypeError):
                return (
                    f"❌ template_id 无效 UUID：{template_id!r}。"
                    "请传入标准 UUID 格式（如 550e8400-e29b-41d4-a716-446655440000）。"
                )

        # Parse skill_ids
        parsed_skill_ids = []
        for sid_str in (skill_ids or []):
            try:
                parsed_skill_ids.append(_uuid.UUID(str(sid_str)))
            except (ValueError, TypeError):
                return (
                    f"❌ skill_ids 中包含无效 UUID：{sid_str!r}。"
                    "请传入标准 UUID 格式（如 550e8400-e29b-41d4-a716-446655440000）。"
                )

        inp = AgentProvisionInput(
            name=name,
            agent_type="native",
            role_description=role_description,
            personality=personality,
            boundaries=boundaries,
            bio=bio or None,
            primary_model_id=pm,
            fallback_model_id=fm,
            permission_scope_type=scope_type,
            permission_scope_ids=scope_ids,
            permission_access_level=access_level,
            autonomy_policy=autonomy_policy,
            max_tokens_per_day=max_tokens_per_day,
            max_tokens_per_month=max_tokens_per_month,
            template_id=parsed_template_id,
            skill_ids=parsed_skill_ids,
            avatar_url=avatar_url,
        )
        try:
            agent, _raw = await provision_agent(db, creator=pc.user, tenant_id=pc.tenant_id, data=inp)
        except QuotaExceeded as e:
            return f"❌ 创建配额已用尽：{e.message}"
        except ValueError as e:
            return f"❌ {e}"
        return (f"✅ 已创建 agent「{agent.name}」(id={agent.id})，状态={agent.status}。\n"
                f"下一步可用 set_agent_tools / set_agent_trigger / set_agent_relationships / edit_agent_soul 配置它。")


async def update_agent_impl(
    ctx,
    agent,
    # Existing fields
    role_description=None,
    primary_model=None,
    fallback_model=None,
    bio=None,
    context_window_size=None,
    max_tool_rounds=None,
    # New fields
    name=None,
    welcome_message=None,
    avatar_url=None,
    autonomy_policy=None,
    max_tokens_per_day=None,
    max_tokens_per_month=None,
    max_triggers=None,
    min_poll_interval_min=None,
    webhook_rate_limit=None,
    heartbeat_enabled=None,
    heartbeat_interval_minutes=None,
    heartbeat_active_hours=None,
    timezone=None,
    expires_at=None,
    imagination=None,
    reasoning_effort=None,
    daily_memory_load_days=None,
    im_thinking_output_enabled=None,
    clear_fields=None,
) -> str:
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err

        is_admin = is_platform_admin_user(pc.user) or pc.user.role == "org_admin"

        # Collect requested changes with before values
        # Each entry: (field_name, old_value, new_value)
        planned: list[tuple[str, object, object]] = []
        clamp_notes: list[str] = []

        # ── expires_at (admin only) ─────────────────────────────────────────
        if expires_at is not None:
            if not is_admin:
                return (
                    "❌ 仅管理员可修改过期时间（expires_at 为管理员专属字段）。"
                    "如需变更请联系 platform_admin 或 org_admin 操作。"
                )
            try:
                parsed_expires = datetime.fromisoformat(expires_at)
                # Ensure tz-aware
                if parsed_expires.tzinfo is None:
                    parsed_expires = parsed_expires.replace(tzinfo=_tz.utc)
            except (ValueError, TypeError):
                return (
                    f"❌ expires_at 格式无效（你传入了 {expires_at!r}）。"
                    "请使用 ISO8601 格式，例如 2030-01-01T00:00:00+00:00。"
                )
            # Re-activate if new expiry is future or cleared
            if parsed_expires > datetime.now(_tz.utc):
                if ag.is_expired:
                    ag.is_expired = False
                    ag.status = "idle"
            planned.append(("expires_at", ag.expires_at, parsed_expires))

        # Keep existing governance fields as standard MCP update_agent capabilities.
        if autonomy_policy is not None:
            planned.append(("autonomy_policy", ag.autonomy_policy, autonomy_policy))

        ordinary_values = {
            field: value
            for field, value in {
                "name": name,
                "welcome_message": welcome_message,
                "avatar_url": avatar_url,
                "role_description": role_description,
                "bio": bio,
                "primary_model": primary_model,
                "fallback_model": fallback_model,
                "imagination": imagination,
                "reasoning_effort": reasoning_effort,
                "context_window_size": context_window_size,
                "daily_memory_load_days": daily_memory_load_days,
                "max_tool_rounds": max_tool_rounds,
                "max_tokens_per_day": max_tokens_per_day,
                "max_tokens_per_month": max_tokens_per_month,
                "max_triggers": max_triggers,
                "min_poll_interval_min": min_poll_interval_min,
                "webhook_rate_limit": webhook_rate_limit,
                "heartbeat_enabled": heartbeat_enabled,
                "heartbeat_interval_minutes": heartbeat_interval_minutes,
                "heartbeat_active_hours": heartbeat_active_hours,
                "timezone": timezone,
                "im_thinking_output_enabled": im_thinking_output_enabled,
            }.items()
            if value is not None
        }
        requested_clear_fields = set(clear_fields or [])
        unsupported_clear_fields = requested_clear_fields - _CLEARABLE_SETTINGS
        if unsupported_clear_fields:
            return (
                "❌ clear_fields 包含不可清空的设置："
                + ", ".join(sorted(unsupported_clear_fields))
                + "。可清空字段："
                + ", ".join(sorted(_CLEARABLE_SETTINGS))
                + "。"
            )
        conflicts = requested_clear_fields & ordinary_values.keys()
        if conflicts:
            return "❌ 同一设置不能同时赋值和清空：" + ", ".join(sorted(conflicts)) + "。"
        ordinary_values.update({field: None for field in requested_clear_fields})
        try:
            ordinary_outcome = await apply_agent_settings_patch(db, ag, ordinary_values)
        except ValueError as exc:
            await db.rollback()
            return f"❌ 设置未更新：{exc}"
        planned.extend(ordinary_outcome.changes)
        clamp_notes.extend(
            f"{item['field']} 已按企业限制从 {item['requested']} 调整为 {item['applied']}"
            for item in ordinary_outcome.clamps
        )

        if not planned:
            return "（未提供任何要修改的字段：请至少传一个字段，如 name 或 role_description。）"

        # The shared service applied ordinary settings. Apply the retained
        # governance/lifecycle fields collected above in the same transaction.
        for field, _old, new_val in planned:
            if field in {"autonomy_policy", "expires_at"}:
                setattr(ag, field, new_val)

        await db.commit()

        # ── Build before→after report ───────────────────────────────────────
        # Use human-friendly field labels for model fields
        change_lines = []
        revert_parts = []
        revert_clear_fields = []
        for field, old_val, new_val in planned:
            label = public_setting_name(field)
            change_lines.append(f"  {label}: {old_val!r} → {new_val!r}")
            if old_val is None and label in _CLEARABLE_SETTINGS:
                revert_clear_fields.append(label)
            else:
                old_arg = str(old_val) if isinstance(old_val, _uuid.UUID) else old_val
                revert_parts.append(f"{label}={old_arg!r}")

        report = f"✅ 已更新「{ag.name}」：\n" + "\n".join(change_lines)
        if clamp_notes:
            report += "\n⚠ 企业限制已应用：\n" + "\n".join(f"  • {n}" for n in clamp_notes)
        revert_hints = list(revert_parts)
        if revert_clear_fields:
            revert_hints.append(f"clear_fields={sorted(revert_clear_fields)!r}")
        report += f"\n↩ 如需回滚：用相同 update_agent 传回旧值（{'; '.join(revert_hints)}）。"
        return report


@mcp.tool()
async def create_agent(  # noqa: D401
    ctx: Context,
    name: str,
    role_description: str = "",
    personality: str = "",
    boundaries: str = "",
    bio: str = "",
    primary_model: str | None = None,
    fallback_model: str | None = None,
    access_mode: str = "company",
    autonomy_policy: dict | None = None,
    avatar_url: str | None = None,
    max_tokens_per_day: int | None = None,
    max_tokens_per_month: int | None = None,
    template_id: str | None = None,
    skill_ids: list[str] | None = None,
    grant_user_ids: list[str] | None = None,
    access_level: str = "use",
) -> str:
    """Create a new native digital-employee agent (requires a write-scope PAT).
    Created in your tenant with you as creator. primary_model/fallback_model accept a model id or
    label (see list_models); omit for the tenant default.
    access_mode: "company" (all users), "private" (creator only), or "custom" (explicit users via
    grant_user_ids + access_level).
    autonomy_policy: dict of action→level (e.g. {"read_files": "L1"}).
    Configure tools/triggers/relationships/soul afterwards. Returns the new agent id."""
    return await create_agent_impl(
        ctx,
        name=name,
        role_description=role_description,
        personality=personality,
        boundaries=boundaries,
        bio=bio,
        primary_model=primary_model,
        fallback_model=fallback_model,
        access_mode=access_mode,
        autonomy_policy=autonomy_policy,
        avatar_url=avatar_url,
        max_tokens_per_day=max_tokens_per_day,
        max_tokens_per_month=max_tokens_per_month,
        template_id=template_id,
        skill_ids=skill_ids,
        grant_user_ids=grant_user_ids,
        access_level=access_level,
    )


@mcp.tool()
async def update_agent(  # noqa: D401
    ctx: Context,
    agent: str,
    role_description: str | None = None,
    primary_model: str | None = None,
    fallback_model: str | None = None,
    bio: str | None = None,
    context_window_size: int | None = None,
    max_tool_rounds: int | None = None,
    name: str | None = None,
    welcome_message: str | None = None,
    avatar_url: str | None = None,
    autonomy_policy: dict | None = None,
    max_tokens_per_day: int | None = None,
    max_tokens_per_month: int | None = None,
    max_triggers: int | None = None,
    min_poll_interval_min: int | None = None,
    webhook_rate_limit: int | None = None,
    heartbeat_enabled: bool | None = None,
    heartbeat_interval_minutes: int | None = None,
    heartbeat_active_hours: str | None = None,
    timezone: str | None = None,
    expires_at: str | None = None,
    imagination: float | None = None,
    reasoning_effort: str | None = None,
    daily_memory_load_days: int | None = None,
    im_thinking_output_enabled: bool | None = None,
    clear_fields: list[str] | None = None,
) -> str:
    """Update an existing agent's settings (requires write scope + manage access).
    agent: id or name. Only the fields you pass are changed.
    primary_model/fallback_model accept a model id or label (see list_models).
    imagination ranges from 0 (stable) to 2 (rich); omit it to keep the current value.
    reasoning_effort uses none/minimal/low/medium/high/xhigh/max; none disables thinking.
    daily_memory_load_days=0 disables Daily Memory loading while retaining Core Memory.
    im_thinking_output_enabled controls clear Agent-authored work updates in IM; despite the
    legacy field name it never exposes raw provider reasoning.
    clear_fields explicitly clears nullable ordinary settings such as primary_model, fallback_model,
    imagination, avatar_url, bio, welcome_message, timezone, and token limits.
    expires_at: ISO8601 datetime string — ADMIN ONLY (platform_admin/org_admin).
    Tenant-floor clamps apply to heartbeat_interval_minutes (floor), min_poll_interval_min (floor),
    and webhook_rate_limit (ceiling) — the response will note any adjustments.
    Response includes a before→after report for every changed field and a revert hint so you can
    roll back by re-calling update_agent with the old values."""
    return await update_agent_impl(
        ctx,
        agent=agent,
        role_description=role_description,
        primary_model=primary_model,
        fallback_model=fallback_model,
        bio=bio,
        context_window_size=context_window_size,
        max_tool_rounds=max_tool_rounds,
        name=name,
        welcome_message=welcome_message,
        avatar_url=avatar_url,
        autonomy_policy=autonomy_policy,
        max_tokens_per_day=max_tokens_per_day,
        max_tokens_per_month=max_tokens_per_month,
        max_triggers=max_triggers,
        min_poll_interval_min=min_poll_interval_min,
        webhook_rate_limit=webhook_rate_limit,
        heartbeat_enabled=heartbeat_enabled,
        heartbeat_interval_minutes=heartbeat_interval_minutes,
        heartbeat_active_hours=heartbeat_active_hours,
        timezone=timezone,
        expires_at=expires_at,
        imagination=imagination,
        reasoning_effort=reasoning_effort,
        daily_memory_load_days=daily_memory_load_days,
        im_thinking_output_enabled=im_thinking_output_enabled,
        clear_fields=clear_fields,
    )
