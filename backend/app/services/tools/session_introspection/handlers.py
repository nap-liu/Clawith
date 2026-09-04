"""Session-introspection builtin tool handlers.

Thin orchestrators invoked from ``agent_tools.execute_tool``. Each owns its DB
session, resolves the permission scope from the effective execution identity via
``session_query``, enforces per-agent isolation on every read, and renders via
``formatting``.

Signature contract: ``(agent_id, user_id, ctx_session_id, arguments) -> str``
  - agent_id / user_id / ctx_session_id are the execute_tool context values
    (user_id is the effective execution user; callers preserve the designed
    creator fallback when a persisted background resource has no executor).
  - arguments is the LLM tool-call payload.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.services import session_query as sq
from app.services.timezone_utils import (
    format_datetime_for_agent,
    get_agent_timezone_in_session,
    parse_datetime_for_agent,
)
from app.services.tools.session_introspection import formatting as fmt


def _clamp(value, *, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _parse_dt(value, timezone_name: str):
    try:
        return parse_datetime_for_agent(value, timezone_name)
    except (ValueError, TypeError) as exc:
        raise ValueError("必须是有效的 ISO 8601 时间") from exc


def _parse_time_bounds(arguments: dict, timezone_name: str):
    return (
        _parse_dt(arguments.get("since"), timezone_name),
        _parse_dt(arguments.get("until"), timezone_name),
    )


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
        timezone_name = await get_agent_timezone_in_session(db, agent)
        scope, where = await sq.resolve_scope(db, agent, ctx_session_id, user_id)
        if where is None:
            return fmt.empty_list("会话")

        scene = (arguments.get("scene") or "").strip() or None
        if scene:
            try:
                from app.schemas.scene import validate_scene_key

                scene = validate_scene_key(scene)
            except ValueError as exc:
                return f"❌ 无效 scene: {exc}"

        counterpart = (arguments.get("counterpart") or "").strip() or None
        group = (arguments.get("group") or "").strip() or None
        counterpart_match = arguments.get("counterpart_match") or "fuzzy"
        group_match = arguments.get("group_match") or "fuzzy"
        if counterpart_match not in {"exact", "fuzzy"}:
            return "❌ counterpart_match 仅支持 exact 或 fuzzy"
        if group_match not in {"exact", "fuzzy"}:
            return "❌ group_match 仅支持 exact 或 fuzzy"
        is_group = arguments.get("is_group")
        if is_group is not None and not isinstance(is_group, bool):
            return "❌ is_group 必须是布尔值"

        limit = _clamp(arguments.get("limit"), default=20, lo=1, hi=50)
        try:
            since, until = _parse_time_bounds(arguments, timezone_name)
        except ValueError as exc:
            return f"❌ since/until {exc}"
        if since and until and since > until:
            return "❌ since 不能晚于 until"
        raw = arguments.get("raw") is True
        if raw:
            filter_fingerprint = sq.session_filter_fingerprint(
                agent_id=agent.id,
                scope=scope,
                viewer_id=user_id,
                channel=arguments.get("channel") or None,
                query=(arguments.get("query") or "").strip() or None,
                since=since.isoformat() if since else None,
                until=until.isoformat() if until else None,
                scene=scene,
                counterpart=counterpart,
                counterpart_match=counterpart_match,
                is_group=is_group,
                group=group,
                group_match=group_match,
            )
            cursor_value = arguments.get("cursor")
            cursor = sq.decode_session_cursor(
                cursor_value,
                expected_filter_fingerprint=filter_fingerprint,
            )
            if cursor_value and cursor is None:
                return "❌ 无效 cursor"
            sessions, total, snapshot_at, next_cursor = await sq.fetch_sessions_raw(
                db,
                where,
                agent_id=agent.id,
                channel=arguments.get("channel"),
                title_query=(arguments.get("query") or "").strip() or None,
                since=since,
                until=until,
                scene=scene,
                counterpart=counterpart,
                counterpart_match=counterpart_match,
                is_group=is_group,
                group=group,
                group_match=group_match,
                limit=limit,
                cursor=cursor,
                filter_fingerprint=filter_fingerprint,
            )
            items = []
            for session in sessions:
                item = {
                    column.name: getattr(session, column.name)
                    for column in ChatSession.__table__.columns
                }
                for column in ChatSession.__table__.columns:
                    value = item[column.name]
                    if isinstance(value, datetime):
                        item[f"{column.name}_local"] = format_datetime_for_agent(
                            value, timezone_name
                        )
                items.append(item)
            return json.dumps(
                {
                    "items": items,
                    "effective_timezone": timezone_name,
                    "page": {
                        "limit": limit,
                        "total": total,
                        "has_more": next_cursor is not None,
                        "next_cursor": next_cursor,
                        "snapshot_at": snapshot_at.isoformat(),
                        "snapshot_at_local": format_datetime_for_agent(
                            snapshot_at, timezone_name
                        ),
                    },
                },
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )

        offset = _clamp(arguments.get("offset"), default=0, lo=0, hi=1_000_000)
        sessions, total = await sq.fetch_sessions(
            db,
            where,
            agent_id=agent.id,
            channel=arguments.get("channel"),
            title_query=(arguments.get("query") or "").strip() or None,
            since=since,
            until=until,
            scene=scene,
            counterpart=counterpart,
            counterpart_match=counterpart_match,
            is_group=is_group,
            group=group,
            group_match=group_match,
            limit=limit,
            offset=offset,
        )
        counts = await sq.count_messages_per_session(db, [s.id for s in sessions])
        counterparts = await sq.resolve_session_counterparts(db, agent.id, sessions)
        return fmt.render_session_list(
            sessions,
            counts,
            counterparts,
            total=total,
            offset=offset,
            limit=limit,
            timezone_name=timezone_name,
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
        timezone_name = await get_agent_timezone_in_session(db, agent)
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
        raw_before = arguments.get("before")
        before = sq.decode_cursor(raw_before)
        if raw_before and before is None:
            return "❌ before 游标无效，请原样使用上一页返回的游标"
        include_tc = bool(arguments.get("include_tool_calls", False))
        msgs = await sq.fetch_session_messages(
            db, target, limit=limit, before=before, include_tool_calls=include_tc
        )
        senders = await sq.resolve_senders(db, msgs)
        more_available = len(msgs) >= limit
        return fmt.render_messages(
            msgs,
            senders,
            more_available=more_available,
            timezone_name=timezone_name,
        )


async def handle_search_sessions(agent_id, user_id, ctx_session_id, arguments) -> str:
    keyword = (arguments.get("query") or "").strip()
    if not keyword:
        return "❌ 缺少必填参数 query"

    async with async_session() as db:
        agent = await _load_agent(db, agent_id)
        if not agent:
            return sq.DENIAL_MSG
        timezone_name = await get_agent_timezone_in_session(db, agent)
        _scope, where = await sq.resolve_scope(db, agent, ctx_session_id, user_id)
        if where is None:
            return fmt.render_search_hits([], {}, keyword=keyword)

        limit = _clamp(arguments.get("limit"), default=20, lo=1, hi=50)
        try:
            since, until = _parse_time_bounds(arguments, timezone_name)
        except ValueError as exc:
            return f"❌ since/until {exc}"
        if since and until and since > until:
            return "❌ since 不能晚于 until"
        channel = arguments.get("channel")
        sess_q = select(
            ChatSession.id,
            ChatSession.title,
            ChatSession.group_name,
            ChatSession.source_channel,
        ).where(where)
        if channel and channel != "all":
            sess_q = sess_q.where(ChatSession.source_channel == channel)
        sess_rows = (await db.execute(sess_q)).all()
        session_ids = [r[0] for r in sess_rows]
        titles = {str(r[0]): (r[2] or r[1] or "(无标题)") for r in sess_rows}
        channels = {str(r[0]): r[3] for r in sess_rows}
        if not session_ids:
            return fmt.render_search_hits([], {}, keyword=keyword)

        t0 = time.monotonic()
        hits = await sq.search_messages(
            db,
            session_ids,
            keyword,
            limit=limit,
            since=since,
            until=until,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            f"[search_sessions] agent={agent.id} scope_sessions={len(session_ids)} "
            f"hits={len(hits)} {elapsed_ms:.0f}ms kw_len={len(keyword)}"
        )
        return fmt.render_search_hits(
            hits,
            titles,
            keyword=keyword,
            channels=channels,
            timezone_name=timezone_name,
        )
