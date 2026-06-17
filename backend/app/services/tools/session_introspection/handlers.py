"""Session-introspection builtin tool handlers.

Thin orchestrators invoked from ``agent_tools.execute_tool``. Each owns its DB
session, resolves the permission scope via ``session_query`` (which discriminates
human-in-loop by the current session's source_channel), enforces per-agent
isolation on every read, and renders via ``formatting``.

Signature contract: ``(agent_id, user_id, ctx_session_id, arguments) -> str``
  - agent_id / user_id / ctx_session_id are the execute_tool context values
    (user_id is the real human only on web/IM turns; a creator otherwise).
  - arguments is the LLM tool-call payload.
"""

from __future__ import annotations

import time
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.services import session_query as sq
from app.services.tools.session_introspection import formatting as fmt


def _clamp(value, *, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


async def _load_agent(db, agent_id) -> Agent | None:
    aid = sq._as_uuid(agent_id)
    if aid is None:
        return None
    return (await db.execute(select(Agent).where(Agent.id == aid))).scalar_one_or_none()


async def handle_list_sessions(agent_id, user_id, ctx_session_id, arguments) -> str:
    async with async_session() as db:
        agent = await _load_agent(db, agent_id)
        if not agent:
            return sq.DENIAL_MSG
        _scope, where = await sq.resolve_scope(db, agent, ctx_session_id, user_id)
        if where is None:
            return fmt.empty_list("会话")

        limit = _clamp(arguments.get("limit"), default=20, lo=1, hi=50)
        offset = _clamp(arguments.get("offset"), default=0, lo=0, hi=1_000_000)
        sessions, total = await sq.fetch_sessions(
            db,
            where,
            channel=arguments.get("channel"),
            title_query=(arguments.get("query") or "").strip() or None,
            since=_parse_dt(arguments.get("since")),
            until=_parse_dt(arguments.get("until")),
            limit=limit,
            offset=offset,
        )
        counts = await sq.count_messages_per_session(db, [s.id for s in sessions])
        counterparts = await sq.resolve_session_counterparts(db, agent.id, sessions)
        return fmt.render_session_list(
            sessions, counts, counterparts, total=total, offset=offset, limit=limit
        )


async def handle_read_session_messages(agent_id, user_id, ctx_session_id, arguments) -> str:
    target = (arguments.get("session_id") or "").strip()
    if not target:
        return "❌ 缺少必填参数 session_id"
    tid = sq._as_uuid(target)
    if tid is None:
        return sq.DENIAL_MSG

    async with async_session() as db:
        agent = await _load_agent(db, agent_id)
        if not agent:
            return sq.DENIAL_MSG
        _scope, where = await sq.resolve_scope(db, agent, ctx_session_id, user_id)
        if where is None:
            return sq.DENIAL_MSG
        # per-agent isolation + scope: target must be in the permitted session set
        in_scope = (
            await db.execute(select(ChatSession.id).where(ChatSession.id == tid, where))
        ).scalar_one_or_none()
        if not in_scope:
            return sq.DENIAL_MSG

        limit = _clamp(arguments.get("limit"), default=30, lo=1, hi=100)
        before = sq.decode_cursor(arguments.get("before"))
        include_tc = bool(arguments.get("include_tool_calls", False))
        msgs = await sq.fetch_session_messages(
            db, target, limit=limit, before=before, include_tool_calls=include_tc
        )
        senders = await sq.resolve_senders(db, msgs)
        more_available = len(msgs) >= limit
        return fmt.render_messages(msgs, senders, more_available=more_available)


async def handle_search_sessions(agent_id, user_id, ctx_session_id, arguments) -> str:
    keyword = (arguments.get("query") or "").strip()
    if not keyword:
        return "❌ 缺少必填参数 query"

    async with async_session() as db:
        agent = await _load_agent(db, agent_id)
        if not agent:
            return sq.DENIAL_MSG
        _scope, where = await sq.resolve_scope(db, agent, ctx_session_id, user_id)
        if where is None:
            return fmt.render_search_hits([], {}, keyword=keyword)

        limit = _clamp(arguments.get("limit"), default=20, lo=1, hi=50)
        channel = arguments.get("channel")
        sess_q = select(ChatSession.id, ChatSession.title, ChatSession.group_name).where(where)
        if channel and channel != "all":
            sess_q = sess_q.where(ChatSession.source_channel == channel)
        sess_rows = (await db.execute(sess_q)).all()
        session_ids = [r[0] for r in sess_rows]
        titles = {str(r[0]): (r[2] or r[1] or "(无标题)") for r in sess_rows}
        if not session_ids:
            return fmt.render_search_hits([], {}, keyword=keyword)

        t0 = time.monotonic()
        hits = await sq.search_messages(db, session_ids, keyword, limit=limit)
        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            f"[search_sessions] agent={agent.id} scope_sessions={len(session_ids)} "
            f"hits={len(hits)} {elapsed_ms:.0f}ms kw_len={len(keyword)}"
        )
        return fmt.render_search_hits(hits, titles, keyword=keyword)
