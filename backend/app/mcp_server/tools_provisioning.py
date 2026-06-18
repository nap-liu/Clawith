"""MCP provisioning tools (write scope): create_agent, update_agent."""
from __future__ import annotations

import uuid as _uuid

from mcp.server.fastmcp import Context
from sqlalchemy import or_, select

from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server._common import authed_write, resolve_manageable_agent
from app.models.llm import LLMModel
from app.services.agent_provisioning import AgentProvisionInput, provision_agent
from app.services.quota_guard import QuotaExceeded

_VALID_ACCESS = {"company", "private"}


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


async def create_agent_impl(ctx, name, role_description="", personality="", boundaries="",
                            bio="", primary_model=None, fallback_model=None, access_mode="company") -> str:
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        if access_mode not in _VALID_ACCESS:
            return f"❌ access_mode 取值非法（可选：{', '.join(sorted(_VALID_ACCESS))}）。"
        if not name or len(name) < 2:
            return "❌ name 至少 2 个字符。"
        pm = await _resolve_model_id(db, pc.tenant_id, primary_model)
        if primary_model and pm is None:
            return "❌ 找不到 primary_model（用 list_models 查看可用模型）。"
        fm = await _resolve_model_id(db, pc.tenant_id, fallback_model)
        if fallback_model and fm is None:
            return "❌ 找不到 fallback_model（用 list_models 查看可用模型）。"
        scope_type = "user" if access_mode == "private" else "company"
        inp = AgentProvisionInput(
            name=name, agent_type="native", role_description=role_description,
            personality=personality, boundaries=boundaries, bio=bio or None,
            primary_model_id=pm, fallback_model_id=fm, permission_scope_type=scope_type,
        )
        try:
            agent, _raw = await provision_agent(db, creator=pc.user, tenant_id=pc.tenant_id, data=inp)
        except QuotaExceeded as e:
            return f"❌ 创建配额已用尽：{e.message}"
        except ValueError as e:
            return f"❌ {e}"
        return (f"✅ 已创建 agent「{agent.name}」(id={agent.id})，状态={agent.status}。\n"
                f"下一步可用 set_agent_tools / set_agent_trigger / set_agent_relationships / edit_agent_soul 配置它。")


async def update_agent_impl(ctx, agent, role_description=None, primary_model=None, fallback_model=None,
                            bio=None, context_window_size=None, max_tool_rounds=None) -> str:
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        changed = []
        if role_description is not None:
            ag.role_description = role_description
            changed.append("role_description")
        if bio is not None:
            ag.bio = bio
            changed.append("bio")
        if context_window_size is not None:
            ag.context_window_size = context_window_size
            changed.append("context_window_size")
        if max_tool_rounds is not None:
            ag.max_tool_rounds = max_tool_rounds
            changed.append("max_tool_rounds")
        if primary_model is not None:
            pm = await _resolve_model_id(db, pc.tenant_id, primary_model)
            if pm is None:
                return "❌ 找不到 primary_model（用 list_models 查看）。"
            ag.primary_model_id = pm
            changed.append("primary_model")
        if fallback_model is not None:
            fm = await _resolve_model_id(db, pc.tenant_id, fallback_model)
            if fm is None:
                return "❌ 找不到 fallback_model（用 list_models 查看）。"
            ag.fallback_model_id = fm
            changed.append("fallback_model")
        if not changed:
            return "（未提供任何要修改的字段）"
        await db.commit()
        return f"✅ 已更新 agent「{ag.name}」：{', '.join(changed)}。"


@mcp.tool()
async def create_agent(ctx: Context, name: str, role_description: str = "", personality: str = "",  # noqa: D401
                       boundaries: str = "", bio: str = "", primary_model: str | None = None,
                       fallback_model: str | None = None, access_mode: str = "company") -> str:
    """Create a new native digital-employee agent (requires a write-scope PAT).
    Created in your tenant with you as creator. primary_model/fallback_model accept a model id or
    label (see list_models); omit for the tenant default. access_mode: "company" or "private".
    Configure tools/triggers/relationships/soul afterwards. Returns the new agent id."""
    return await create_agent_impl(ctx, name=name, role_description=role_description, personality=personality,
                                   boundaries=boundaries, bio=bio, primary_model=primary_model,
                                   fallback_model=fallback_model, access_mode=access_mode)


@mcp.tool()
async def update_agent(ctx: Context, agent: str, role_description: str | None = None,  # noqa: D401
                       primary_model: str | None = None, fallback_model: str | None = None,
                       bio: str | None = None, context_window_size: int | None = None,
                       max_tool_rounds: int | None = None) -> str:
    """Update an existing agent's settings (requires write scope + manage access).
    agent: id or name. Only the fields you pass are changed. primary_model/fallback_model accept
    a model id or label (see list_models)."""
    return await update_agent_impl(ctx, agent=agent, role_description=role_description,
                                   primary_model=primary_model, fallback_model=fallback_model, bio=bio,
                                   context_window_size=context_window_size, max_tool_rounds=max_tool_rounds)
