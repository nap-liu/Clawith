"""MCP discovery tools (read scope): enumerate enablable tools and LLM models."""
from __future__ import annotations

from mcp.server.fastmcp import Context
from sqlalchemy import or_, select

from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server._common import _tenant_tool_clause
from app.mcp_server.auth import resolve_pat_context
from app.models.llm import LLMModel
from app.models.tool import Tool

_UNAUTH = "❌ 未鉴权：请在 MCP 客户端配置 Authorization: Bearer <clw_...> 令牌。"


async def list_available_tools_impl(ctx, keyword: str | None = None) -> str:
    async with async_session() as db:
        pc = await resolve_pat_context(ctx, db)
        if pc is None:
            return _UNAUTH
        rows = (await db.execute(
            select(Tool).where(Tool.enabled == True, _tenant_tool_clause(pc.tenant_id))  # noqa: E712
        )).scalars().all()
        if keyword:
            k = keyword.lower()
            rows = [t for t in rows if k in (t.name or "").lower() or k in (t.description or "").lower()]
        if not rows:
            return "（没有可用的工具）"
        lines = [
            f"- {t.name} (id={t.id}) — {t.description or '—'} [{t.category}]"
            f"{' ·默认' if getattr(t, 'is_default', False) else ''}"
            for t in rows
        ]
        return "可启用的工具：\n" + "\n".join(lines)


async def list_models_impl(ctx) -> str:
    async with async_session() as db:
        pc = await resolve_pat_context(ctx, db)
        if pc is None:
            return _UNAUTH
        rows = (await db.execute(
            select(LLMModel).where(
                LLMModel.enabled == True,  # noqa: E712
                or_(LLMModel.tenant_id == pc.tenant_id, LLMModel.tenant_id.is_(None)),
            )
        )).scalars().all()
        if not rows:
            return "（没有可用的模型）"
        lines = [f"- {m.label} (id={m.id}) — {m.provider}/{m.model} · ctx={m.context_window}" for m in rows]
        return "可用模型：\n" + "\n".join(lines)


@mcp.tool()
async def list_available_tools(ctx: Context, keyword: str | None = None) -> str:  # noqa: D401
    """List builtin/admin tools you can enable on an agent (for set_agent_tools / create_agent).
    Each line shows the tool id, name, category, and whether it's seeded by default."""
    return await list_available_tools_impl(ctx, keyword)


@mcp.tool()
async def list_models(ctx: Context) -> str:  # noqa: D401
    """List LLM models available to your tenant (for primary_model / fallback_model).
    Each line shows the model id, label, provider, and context window."""
    return await list_models_impl(ctx)
