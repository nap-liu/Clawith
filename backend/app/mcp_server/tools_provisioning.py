"""MCP provisioning tools (write scope): create_agent, update_agent."""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone as _tz

from mcp.server.fastmcp import Context
from sqlalchemy import or_, select

from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server._common import authed_write, resolve_manageable_agent
from app.models.llm import LLMModel
from app.services.agent_provisioning import AgentProvisionInput, provision_agent
from app.services.quota_guard import QuotaExceeded

_VALID_ACCESS = {"company", "private", "custom"}


async def _resolve_model_id(db, tenant_id, ref):
    """Resolve a model ref (UUID or label) to LLMModel.id within the tenant, or None."""
    if not ref:
        return None
    base = select(LLMModel).where(
        LLMModel.enabled == True,  # noqa: E712
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
) -> str:
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err

        is_admin = pc.user.role in ("platform_admin", "org_admin")

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

        # ── Simple string/int/bool fields ───────────────────────────────────
        if name is not None:
            planned.append(("name", ag.name, name))
        if welcome_message is not None:
            planned.append(("welcome_message", ag.welcome_message, welcome_message))
        if avatar_url is not None:
            planned.append(("avatar_url", ag.avatar_url, avatar_url))
        if autonomy_policy is not None:
            planned.append(("autonomy_policy", ag.autonomy_policy, autonomy_policy))
        if max_tokens_per_day is not None:
            planned.append(("max_tokens_per_day", ag.max_tokens_per_day, max_tokens_per_day))
        if max_tokens_per_month is not None:
            planned.append(("max_tokens_per_month", ag.max_tokens_per_month, max_tokens_per_month))
        if max_triggers is not None:
            planned.append(("max_triggers", ag.max_triggers, max_triggers))
        if heartbeat_enabled is not None:
            planned.append(("heartbeat_enabled", ag.heartbeat_enabled, heartbeat_enabled))
        if heartbeat_active_hours is not None:
            planned.append(("heartbeat_active_hours", ag.heartbeat_active_hours, heartbeat_active_hours))
        if timezone is not None:
            planned.append(("timezone", ag.timezone, timezone))
        if role_description is not None:
            planned.append(("role_description", ag.role_description, role_description))
        if bio is not None:
            planned.append(("bio", ag.bio, bio))
        if context_window_size is not None:
            planned.append(("context_window_size", ag.context_window_size, context_window_size))
        if max_tool_rounds is not None:
            planned.append(("max_tool_rounds", ag.max_tool_rounds, max_tool_rounds))

        # ── Model resolution ────────────────────────────────────────────────
        if primary_model is not None:
            pm = await _resolve_model_id(db, pc.tenant_id, primary_model)
            if pm is None:
                return (
                    f"❌ 找不到 primary_model（你传入了 {primary_model!r}）。"
                    "用 list_models 查看可用模型，并以其 id 或 label 重试。"
                )
            planned.append(("primary_model_id", ag.primary_model_id, pm))
        if fallback_model is not None:
            fm = await _resolve_model_id(db, pc.tenant_id, fallback_model)
            if fm is None:
                return (
                    f"❌ 找不到 fallback_model（你传入了 {fallback_model!r}）。"
                    "用 list_models 查看可用模型，并以其 id 或 label 重试。"
                )
            planned.append(("fallback_model_id", ag.fallback_model_id, fm))

        # ── Tenant clamps ───────────────────────────────────────────────────
        # Load tenant once if we need it
        needs_clamp = (heartbeat_interval_minutes is not None
                       or min_poll_interval_min is not None
                       or webhook_rate_limit is not None)
        tenant = None
        if needs_clamp and ag.tenant_id:
            from app.models.tenant import Tenant
            t_result = await db.execute(select(Tenant).where(Tenant.id == ag.tenant_id))
            tenant = t_result.scalar_one_or_none()

        if heartbeat_interval_minutes is not None:
            old_hbi = ag.heartbeat_interval_minutes
            new_hbi = heartbeat_interval_minutes
            if tenant and new_hbi < tenant.min_heartbeat_interval_minutes:
                new_hbi = tenant.min_heartbeat_interval_minutes
                clamp_notes.append(
                    f"heartbeat_interval_minutes 已按企业下限从 {heartbeat_interval_minutes} 调整为 {new_hbi}"
                )
            planned.append(("heartbeat_interval_minutes", old_hbi, new_hbi))

        if min_poll_interval_min is not None:
            old_mpi = ag.min_poll_interval_min
            new_mpi = min_poll_interval_min
            if tenant and new_mpi < tenant.min_poll_interval_floor:
                new_mpi = tenant.min_poll_interval_floor
                clamp_notes.append(
                    f"min_poll_interval_min 已按企业下限从 {min_poll_interval_min} 调整为 {new_mpi}"
                )
            planned.append(("min_poll_interval_min", old_mpi, new_mpi))

        if webhook_rate_limit is not None:
            old_wrl = ag.webhook_rate_limit
            new_wrl = webhook_rate_limit
            if tenant and new_wrl > tenant.max_webhook_rate_ceiling:
                new_wrl = tenant.max_webhook_rate_ceiling
                clamp_notes.append(
                    f"webhook_rate_limit 已按企业上限从 {webhook_rate_limit} 调整为 {new_wrl}"
                )
            planned.append(("webhook_rate_limit", old_wrl, new_wrl))

        if not planned:
            return "（未提供任何要修改的字段：请至少传一个字段，如 name 或 role_description。）"

        # ── Apply changes ───────────────────────────────────────────────────
        for field, _old, new_val in planned:
            setattr(ag, field, new_val)

        # ── Participant sync ────────────────────────────────────────────────
        changed_fields = {f for f, _, _ in planned}
        if "name" in changed_fields or "avatar_url" in changed_fields:
            from app.models.participant import Participant
            p_r = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == ag.id)
            )
            p = p_r.scalar_one_or_none()
            if p:
                if "name" in changed_fields:
                    p.display_name = ag.name
                if "avatar_url" in changed_fields:
                    p.avatar_url = ag.avatar_url

        await db.commit()

        # ── Build before→after report ───────────────────────────────────────
        # Use human-friendly field labels for model fields
        _field_label = {"primary_model_id": "primary_model", "fallback_model_id": "fallback_model"}
        change_lines = []
        revert_parts = []
        for field, old_val, new_val in planned:
            label = _field_label.get(field, field)
            change_lines.append(f"  {label}: {old_val!r} → {new_val!r}")
            revert_parts.append(f"{label}={old_val!r}")

        report = f"✅ 已更新「{ag.name}」：\n" + "\n".join(change_lines)
        if clamp_notes:
            report += "\n⚠ 企业限制已应用：\n" + "\n".join(f"  • {n}" for n in clamp_notes)
        report += f"\n↩ 如需回滚：用相同 update_agent 传回旧值（{'; '.join(revert_parts)}）。"
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
) -> str:
    """Update an existing agent's settings (requires write scope + manage access).
    agent: id or name. Only the fields you pass are changed.
    primary_model/fallback_model accept a model id or label (see list_models).
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
    )
