"""Clawith MCP Server — the five public tool implementations.

Every tool opens its own short-lived ``async with async_session() as db:`` so
the MCP request-scoped session is never shared across concurrent requests.  All
five return plain strings; the MCP SDK serialises them as text/result.

Security invariants (enforced structurally):
- Authentication: every tool calls ``resolve_pat_user`` first.  Unauthenticated
  requests receive ``_UNAUTH`` — never a partial result or a DB error.
- Visibility: agent resolution always goes through
  ``build_visible_agents_query(user)`` — never a bare ``SELECT * FROM agents``.
- Scope: session access always goes through ``resolve_human_viewer_access`` →
  ``scope_predicate_for``.  The predicate's ``agent_id`` is always the session's
  own ``agent_id``, never a default.
- Existence non-revealing denial: ``get_session`` / ``chat_with_agent`` return
  the same ``_DENY`` string whether the session is missing, cross-agent, or
  cross-user.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import Context
from sqlalchemy import or_, select

from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server.auth import resolve_pat_user
from app.core.permissions import build_visible_agents_query
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.agent import Agent
from app.services import session_query as sq

# ── Shared text constants ────────────────────────────────────────────────────

_UNAUTH = "❌ 未鉴权：请在 MCP 客户端配置 Authorization: Bearer <clw_...> 令牌。"
_DENY = "❌ 无法访问：该会话不存在，或你无权查看。"

# All channels a human can initiate plus 'mcp'.  Used to validate the
# ``channel`` filter parameter in ``list_sessions``.
_HUMAN_PLUS = frozenset(
    {
        "web",
        "feishu",
        "dingtalk",
        "wecom",
        "slack",
        "discord",
        "teams",
        "whatsapp",
        "wechat",
        "mcp",
    }
)


# ── Shared helper: resolve an agent by id-or-name within the user's visible set ──


async def _resolve_visible_agent(db, user, agent_ref: str):
    """Return the Agent if ``agent_ref`` (UUID string or name) matches exactly
    one entry in the user's visible agent set.  Returns ``None`` for missing,
    invisible, or ambiguous (multiple name matches) references.
    """
    visible = list((await db.execute(build_visible_agents_query(user))).scalars())

    # Try UUID match first (fast-path, unambiguous).
    try:
        aid = _uuid.UUID(str(agent_ref))
        for a in visible:
            if a.id == aid:
                return a
    except (ValueError, TypeError):
        pass

    # Fall back to case-insensitive name match.
    matches = [a for a in visible if (a.name or "").lower() == agent_ref.lower()]
    return matches[0] if len(matches) == 1 else None


# ── Shared helper: A2A-aware session scope ────────────────────────────────────


async def _session_max_scope(db, user, sess: ChatSession) -> str:
    """Determine the maximum scope a human user has over a session.

    For A2A sessions we check both ``sess.agent_id`` and ``sess.peer_agent_id``
    and return the highest scope (SCOPE_ALL > SCOPE_OWN > SCOPE_DENY).  This
    ensures that a user with ``manage`` access to *either* side of an A2A
    conversation can read it.

    The predicate returned by ``scope_predicate_for`` is always anchored to
    ``sess.agent_id`` (the canonical session owner) so the SQL query never
    escapes the session's own scope.
    """
    cand_ids = [sess.agent_id]
    if sess.source_channel == "agent" and sess.peer_agent_id:
        cand_ids.append(sess.peer_agent_id)

    best = sq.SCOPE_DENY
    for aid in cand_ids:
        a = (await db.execute(select(Agent).where(Agent.id == aid))).scalar_one_or_none()
        if a is None:
            continue
        s = await sq.resolve_human_viewer_access(db, user.id, a)
        if s == sq.SCOPE_ALL:
            return sq.SCOPE_ALL
        if s == sq.SCOPE_OWN:
            best = sq.SCOPE_OWN
    return best


# ── Shared helper: resolve or create the MCP session (three paths) ────────────


async def _resolve_mcp_session(db, user, agent, *, new_conversation: bool, session_id: str | None):
    """Return (ChatSession, error_code | None).

    Path 1 — explicit session_id: pure SELECT, no find_or_create (which would
    silently rewrite ``user_id`` of the existing session).
    Path 2 — new_conversation=True: always create a fresh session.
    Path 3 — default: reuse the most-recent ``source_channel='mcp'`` session
    for (agent, user) or create one if none exists yet.

    Error codes: "bad_id", "deny", "mismatch" (None means success).
    """
    from app.services.channel_session import find_or_create_channel_session

    if session_id:
        # Path 1 — jump to an existing session by id.
        try:
            sid = _uuid.UUID(session_id)
        except (ValueError, TypeError):
            return None, "bad_id"
        sess = (
            await db.execute(select(ChatSession).where(ChatSession.id == sid))
        ).scalar_one_or_none()
        if sess is None:
            return None, "deny"
        scope = await _session_max_scope(db, user, sess)
        pred = sq.scope_predicate_for(scope, sess.agent_id, user.id)
        if pred is None:
            return None, "deny"
        # Hit-test: the session must satisfy the predicate (e.g. SCOPE_OWN requires
        # the viewer to have personally participated — not just any session of the agent).
        hit = (
            await db.execute(
                select(ChatSession.id).where(ChatSession.id == sid, pred)
            )
        ).scalar_one_or_none()
        if hit is None:
            return None, "deny"
        # Validate agent match when an explicit agent was also given.
        if agent is not None and sess.agent_id != agent.id and sess.peer_agent_id != agent.id:
            return None, "mismatch"
        return sess, None

    if not new_conversation:
        # Path 3 — continue the most-recent mcp session.
        sess = (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == agent.id,
                    ChatSession.user_id == user.id,
                    ChatSession.source_channel == "mcp",
                )
                .order_by(ChatSession.last_message_at.desc().nullslast())
                .limit(1)
            )
        ).scalar_one_or_none()
        if sess is not None:
            return sess, None

    # Path 2 (or path 3 with no existing session): create a fresh mcp session.
    sess = await find_or_create_channel_session(
        db=db,
        agent_id=agent.id,
        user_id=user.id,
        external_conv_id=f"mcp_{_uuid.uuid4().hex}",
        source_channel="mcp",
        first_message_title="(MCP)",
        is_group=False,
    )
    return sess, None


# ── Tool 1: list_agents ───────────────────────────────────────────────────────


@mcp.tool()
async def list_agents(ctx: Context, keyword: str | None = None) -> str:  # noqa: D401
    """List the digital-employee agents you can interact with.

    Pass ``keyword`` to filter by name or role description.  Each entry shows
    the agent id (use this as the ``agent`` parameter in other tools), name,
    role description, and access mode.
    """
    async with async_session() as db:
        user, _tid = await resolve_pat_user(ctx, db)
        if user is None:
            return _UNAUTH

        q = build_visible_agents_query(user)
        agents = list((await db.execute(q)).scalars())

        if keyword:
            k = keyword.lower()
            agents = [
                a
                for a in agents
                if k in (a.name or "").lower() or k in (a.role_description or "").lower()
            ]

        if not agents:
            return "（没有可用的 agent）"

        lines = [
            f"- {a.name} (id={a.id}) — {a.role_description or '—'} [{a.access_mode}]"
            for a in agents
        ]
        return "你可对话的数字员工：\n" + "\n".join(lines)


# ── Tool 2: get_agent_info ────────────────────────────────────────────────────


@mcp.tool()
async def get_agent_info(ctx: Context, agent: str) -> str:  # noqa: D401
    """Return the public profile of a specific agent.

    ``agent`` can be the agent's UUID or its exact name.  Resolves only within
    agents visible to you — invisible or non-existent agents return the same
    denial message to avoid information leakage.

    Note: soul.md contents and internal system prompts are intentionally not
    exposed here.
    """
    async with async_session() as db:
        user, _ = await resolve_pat_user(ctx, db)
        if user is None:
            return _UNAUTH

        a = await _resolve_visible_agent(db, user, agent)
        if a is None:
            return "❌ 找不到该 agent，或你无权访问。"

        return (
            f"{a.name}\n"
            f"职责：{a.role_description or '—'}\n"
            f"访问级：{a.access_mode}"
        )


# ── Tool 3: list_sessions ─────────────────────────────────────────────────────


@mcp.tool()
async def list_sessions(  # noqa: D401
    ctx: Context,
    agent: str | None = None,
    channel: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """List chat sessions visible to you, optionally filtered by agent or channel.

    ``agent``: filter to a specific agent (UUID or name).  Omit to see all
    agents you can access.
    ``channel``: one of web/feishu/dingtalk/wecom/slack/discord/teams/whatsapp/
    wechat/mcp (or omit for all channels).
    ``limit`` / ``offset``: pagination (max 50 per page).
    """
    async with async_session() as db:
        user, _ = await resolve_pat_user(ctx, db)
        if user is None:
            return _UNAUTH

        # Validate channel parameter.
        if channel and channel not in _HUMAN_PLUS and channel != "all":
            return "❌ channel 取值非法。有效值：" + "、".join(sorted(_HUMAN_PLUS)) + "。"

        # Resolve agent scope.
        if agent:
            a = await _resolve_visible_agent(db, user, agent)
            if a is None:
                return _DENY
            agent_list = [a]
        else:
            agent_list = list((await db.execute(build_visible_agents_query(user))).scalars())

        # Build per-agent scope predicates.
        preds = []
        for a in agent_list:
            scope = await sq.resolve_human_viewer_access(db, user.id, a)
            # Hardcoded: viewer_id=user.id, agent_id=a.id — never mixed up.
            p = sq.scope_predicate_for(scope, a.id, user.id)
            if p is not None:
                preds.append(p)

        if not preds:
            return "（没有会话）"

        where = or_(*preds)
        rows, total = await sq.fetch_sessions(
            db, where, channel=channel, limit=min(limit, 50), offset=offset
        )
        counts = await sq.count_messages_per_session(db, [r.id for r in rows])

        # Build agent-name lookup for labelling.
        agent_ids = {r.agent_id for r in rows}
        agent_names: dict[_uuid.UUID, str] = {}
        if agent_ids:
            for a in (
                await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
            ).scalars():
                agent_names[a.id] = a.name

        if not rows:
            return "（没有会话）"

        head = f"共 {total} 个会话，显示第 {offset + 1}–{offset + len(rows)} 个："
        lines = [head]
        for i, s in enumerate(rows, start=offset + 1):
            cid = str(s.id)
            title = s.group_name or s.title or "(无标题)"
            n = counts.get(cid, 0)
            last = s.last_message_at.isoformat() if s.last_message_at else "—"
            agent_label = agent_names.get(s.agent_id, str(s.agent_id))
            lines.append(
                f"{i}. [{cid}] agent={agent_label} · {title}"
                f" · 通道={s.source_channel} · {n} 条 · 最后={last}"
            )
        if offset + len(rows) < total:
            lines.append(f"… 还有更多，用 offset={offset + min(limit, 50)} 继续。")
        return "\n".join(lines)


# ── Tool 4: get_session ───────────────────────────────────────────────────────


@mcp.tool()
async def get_session(  # noqa: D401
    ctx: Context,
    session_id: str,
    limit: int = 50,
    before: str | None = None,
) -> str:
    """Read the message history of a specific session.

    ``session_id``: the UUID of the session (from list_sessions output).
    ``limit``: max messages to return (capped at 100).
    ``before``: pagination cursor from a previous call (omit for the latest page).

    Access is denied — with the same message regardless of reason — if the
    session is missing, belongs to another user, or is in a scope you cannot
    access.  This prevents existence disclosure.
    """
    async with async_session() as db:
        user, _ = await resolve_pat_user(ctx, db)
        if user is None:
            return _UNAUTH

        # Parse session id.
        try:
            sid = _uuid.UUID(session_id)
        except (ValueError, TypeError):
            return _DENY

        sess = (
            await db.execute(select(ChatSession).where(ChatSession.id == sid))
        ).scalar_one_or_none()
        if sess is None:
            return _DENY

        # A2A-aware scope check: derive from both sides of the conversation.
        scope = await _session_max_scope(db, user, sess)
        pred = sq.scope_predicate_for(scope, sess.agent_id, user.id)
        if pred is None:
            return _DENY

        # Hit-test: the session must satisfy the predicate.
        hit = (
            await db.execute(
                select(ChatSession.id).where(ChatSession.id == sid, pred)
            )
        ).scalar_one_or_none()
        if hit is None:
            return _DENY

        cur = sq.decode_cursor(before)
        msgs = await sq.fetch_session_messages(
            db, str(sid), limit=min(limit, 100), before=cur
        )
        senders = await sq.resolve_senders(db, msgs)

        from app.services.tools.session_introspection.formatting import render_messages

        return render_messages(msgs, senders, more_available=(len(msgs) >= min(limit, 100)))


# ── Tool 5: chat_with_agent ───────────────────────────────────────────────────


@mcp.tool()
async def chat_with_agent(  # noqa: D401
    ctx: Context,
    message: str,
    agent: str | None = None,
    new_conversation: bool = False,
    session_id: str | None = None,
) -> str:
    """Send a message to a digital-employee agent and wait for the reply.

    ``agent``: agent UUID or name.  Required when ``session_id`` is omitted.
    ``new_conversation``: set to true to always start a fresh conversation.
    ``session_id``: jump to a specific existing conversation (UUID from
    list_sessions).  When supplied the conversation continues where it left
    off regardless of channel.

    Session-selection priority:
    1. If ``session_id`` is given → resume that exact session (plain SELECT,
       never rewrites the existing session's user_id).
    2. If ``new_conversation=True`` → create a new mcp session.
    3. Default → continue the most-recent mcp session for (agent, user), or
       create one if none exists yet.
    """
    from app.services.channel_llm import _call_agent_llm, broadcast_channel_user_message
    from app.services.chat_history import load_history_for_llm, persist_assistant_reply

    async with async_session() as db:
        user, _ = await resolve_pat_user(ctx, db)
        if user is None:
            return _UNAUTH

        # Resolve agent (required unless a session_id is given).
        a = await _resolve_visible_agent(db, user, agent) if agent else None
        if agent and a is None:
            return "❌ 找不到该 agent，或你无权访问。"
        if a is None and not session_id:
            return "❌ 请指定 agent（name 或 UUID）。"

        # Three-path session resolution.
        sess, err = await _resolve_mcp_session(
            db,
            user,
            a,
            new_conversation=new_conversation,
            session_id=session_id,
        )
        if err == "deny":
            return _DENY
        if err in ("bad_id", "mismatch"):
            return "❌ session_id 无效或与 agent 不一致。"

        # If we came in via session_id without an agent reference, resolve now.
        if a is None:
            a = (
                await db.execute(select(Agent).where(Agent.id == sess.agent_id))
            ).scalar_one_or_none()
            if a is None:
                return _DENY

        conv_id = str(sess.id)
        target_agent_id = sess.agent_id  # canonical agent for this session

        # Load history before writing the user message so the new row is
        # excluded from the history slice (the LLM gets a clean window).
        history = await load_history_for_llm(
            db,
            agent_id=target_agent_id,
            conversation_id=conv_id,
            ctx_size=100,
            is_group=False,
        )

        # Persist the user message and update session timestamp.
        db.add(
            ChatMessage(
                agent_id=target_agent_id,
                user_id=user.id,
                role="user",
                content=message,
                conversation_id=conv_id,
            )
        )
        sess.last_message_at = datetime.now(timezone.utc)
        await db.commit()

        # Mirror the inbound message to any web client watching this session.
        await broadcast_channel_user_message(
            target_agent_id,
            conv_id,
            content=message,
            sender_name=user.display_name,
            user_id=user.id,
        )

        # Progress callback — best-effort, never crash the tool.
        # Tool args are intentionally not echoed to avoid leaking sensitive params.
        async def on_tool_call(evt: dict) -> None:
            try:
                if evt.get("status") == "running":
                    await ctx.report_progress(
                        progress=0.0,
                        total=1.0,
                        message=f"调用工具 {evt.get('name', '')}…",
                    )
            except Exception:  # noqa: BLE001
                pass

        # Execute the LLM call.  recovery_hint=None disables the IM-specific
        # "/new 开启新对话" suffix on error replies — not applicable for MCP.
        reply = await _call_agent_llm(
            db,
            target_agent_id,
            message,
            session_id=conv_id,
            user_id=user.id,
            history=history,
            on_tool_call=on_tool_call,
            is_group=False,
            recovery_hint=None,
        )

    # Persist the assistant reply in its own session so created_at is stamped
    # AFTER the tool-call rows, keeping the message order correct.
    await persist_assistant_reply(
        async_session,
        agent_id=target_agent_id,
        user_id=user.id,
        conversation_id=conv_id,
        content=reply,
    )

    return reply
