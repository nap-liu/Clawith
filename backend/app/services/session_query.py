"""Session-introspection query & scope primitives.

Pure-ish SQLAlchemy building blocks shared by the session-introspection
builtin tools (``list_sessions`` / ``read_session_messages`` /
``search_sessions``). Keeping these here (not in ``core/permissions.py``)
preserves that module's "human-vs-agent RBAC" focus while letting both the
tools and — later — REST import a single source of truth for "which
conversations does this agent participate in".

Design contract: see docs/specs/2026-06-17-session-introspection-tools-design.md

Key invariants enforced structurally here:

- **per-agent isolation**: every scope is a subset of ``build_owned_sessions_predicate``
  (sessions the agent is a party to). No caller-supplied parameter can widen it
  to another agent.
- **A2A message rows**: A2A ``chat_messages`` are all written under the
  normalized ``session_agent_id`` (``min(a, b)``), so messages are *always*
  fetched by ``conversation_id`` — never ``ChatMessage.agent_id == self`` (that
  would return zero rows for the larger-UUID side of every A2A pair).
- **non-human context**: when the current turn has no human in the loop
  (source_channel ``agent``/``trigger``), the agent gets only its autonomous-side
  sessions — it never inherits a creator's cross-user admin reach.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String, and_, cast, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import get_agent_access_level_for_user_id
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.participant import Participant
from app.models.user import User

# Every IM/web channel where a real human is the conversation partner. Anything
# not in this set (``agent``, ``trigger``) is a non-human / unattended turn.
HUMAN_CHANNELS = frozenset(
    {"web", "feishu", "dingtalk", "wecom", "slack", "discord", "teams", "whatsapp", "wechat"}
)

# Scope kinds returned by resolve_scope.
SCOPE_ALL = "all"          # human manage: every session this agent owns
SCOPE_OWN = "own"          # human non-manage: only the viewer's own human sessions
SCOPE_AUTONOMOUS = "auto"  # non-human turn: autonomous-side sessions only
SCOPE_DENY = "deny"        # no access

# Unified, existence-non-revealing denial (read of a session outside scope / cross-agent / missing).
DENIAL_MSG = "❌ 无法访问：该会话不存在，或你无权查看。"

# Message roles surfaced to the introspecting LLM by default (tool_call rows are
# raw JSON noise; system rows are internal).
_VISIBLE_ROLES = ("user", "assistant")


def encode_cursor(msg) -> str:
    """``before`` cursor for read pagination: ``<iso_created_at>|<message_uuid>``."""
    return f"{msg.created_at.isoformat()}|{msg.id}"


def decode_cursor(value: str | None):
    """Parse a cursor back to ``(created_at, id)``; None on anything malformed."""
    if not value:
        return None
    try:
        ts, mid = value.split("|", 1)
        return (datetime.fromisoformat(ts), uuid.UUID(mid))
    except (ValueError, AttributeError, TypeError):
        return None


def _as_uuid(value) -> uuid.UUID | None:
    """Best-effort str/UUID -> UUID. Returns None on anything unparseable so
    callers degrade to 'no match' rather than raising on attacker-controlled input."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def build_owned_sessions_predicate(agent_id: uuid.UUID):
    """Sessions this agent is a party to — the per-agent isolation boundary.

    Covers: web/IM (agent_id == self), trigger reflection (agent_id == self),
    and A2A in both normalized directions (agent_id == self, or peer_agent_id ==
    self when the row is an 'agent' channel session).
    """
    return or_(
        ChatSession.agent_id == agent_id,
        and_(
            ChatSession.peer_agent_id == agent_id,
            ChatSession.source_channel == "agent",
        ),
    )


# ── Scope predicates (all subsets of OWNED) ────────────────────────────────


def _all_sessions_where(agent_id: uuid.UUID):
    return build_owned_sessions_predicate(agent_id)


def _own_participated_where(agent_id: uuid.UUID, viewer_id):
    """Only human sessions the viewer personally took part in (mirrors the REST
    ``scope=mine`` rule). Excludes A2A and trigger — the viewer is not a party."""
    group_member = (
        exists()
        .where(
            and_(
                ChatMessage.conversation_id == cast(ChatSession.id, String),
                ChatMessage.role == "user",
                ChatMessage.user_id == viewer_id,
            )
        )
    )
    return and_(
        ChatSession.agent_id == agent_id,
        ChatSession.source_channel.notin_(["agent", "trigger"]),
        or_(
            and_(ChatSession.is_group.is_(False), ChatSession.user_id == viewer_id),
            and_(ChatSession.is_group.is_(True), group_member),
        ),
    )


def scope_predicate_for(scope_kind: str, agent_id, viewer_id):
    """Public entry: map a SCOPE_* kind to its session WHERE predicate for ONE agent.

    ``SCOPE_ALL`` -> every session the agent owns; ``SCOPE_OWN`` -> only sessions
    the viewer personally participated in; ``SCOPE_DENY`` (and any unknown kind) -> None.

    Lets cross-module callers (the MCP channel — always a human viewer — and
    future REST) compose scope predicates without importing the private
    ``_*_where`` helpers. ``SCOPE_AUTONOMOUS`` is intentionally not exposed here:
    it is an agent-tool concept (see ``resolve_scope``), not a human-viewer scope.
    """
    if scope_kind == SCOPE_ALL:
        return _all_sessions_where(agent_id)
    if scope_kind == SCOPE_OWN:
        return _own_participated_where(agent_id, viewer_id)
    return None


def _autonomous_where(agent_id: uuid.UUID, ctx_session_id):
    """Non-human turn: the agent's own trigger reflections + its A2A sessions +
    whatever the current turn's session is. NOT the human conversation archive."""
    base = or_(
        and_(ChatSession.source_channel == "trigger", ChatSession.agent_id == agent_id),
        and_(ChatSession.source_channel == "agent", build_owned_sessions_predicate(agent_id)),
    )
    cu = _as_uuid(ctx_session_id)
    if cu is not None:
        return or_(base, and_(ChatSession.id == cu, build_owned_sessions_predicate(agent_id)))
    return base


async def resolve_human_viewer_access(db: AsyncSession, user_id, agent: Agent) -> str:
    """Map the authoritative access level to a scope kind for a human viewer.

    Reuses ``permissions.get_agent_access_level_for_user_id`` which already:
    enforces tenant match (incl. platform_admin), honors access_mode (org_admin
    cannot read others' private agents), ignores the unrecognized ``agent_admin``
    role, and is HTTP-exception free.
    """
    level = await get_agent_access_level_for_user_id(db, _as_uuid(user_id), agent)
    if level == "manage":
        return SCOPE_ALL
    if level:  # 'use'/other: has access but not manage
        return SCOPE_OWN
    return SCOPE_DENY


async def resolve_scope(db: AsyncSession, agent: Agent, ctx_session_id: str, user_id):
    """Return ``(scope_kind, session_where_predicate)``.

    Discriminates by the *current* session's source_channel — the only reliable
    "is a human in the loop?" signal at the tool layer (``user_id`` degrades to a
    creator in A2A/trigger/cron paths and must not be trusted for escalation).
    """
    ctx = None
    cu = _as_uuid(ctx_session_id)
    if cu is not None:
        ctx = (
            await db.execute(select(ChatSession).where(ChatSession.id == cu))
        ).scalar_one_or_none()
    channel = ctx.source_channel if ctx else None

    if channel in HUMAN_CHANNELS:
        scope = await resolve_human_viewer_access(db, user_id, agent)
        if scope == SCOPE_DENY:
            return SCOPE_DENY, None
        if scope == SCOPE_ALL:
            return SCOPE_ALL, _all_sessions_where(agent.id)
        return SCOPE_OWN, _own_participated_where(agent.id, _as_uuid(user_id))

    # Non-human channel ('agent'/'trigger') or missing ctx -> autonomous minimum.
    return SCOPE_AUTONOMOUS, _autonomous_where(agent.id, ctx_session_id)


# ── Message window ─────────────────────────────────────────────────────────


def build_session_messages_query(
    conversation_id: str,
    *,
    limit: int,
    before: tuple | None = None,
    include_tool_calls: bool = False,
):
    """Latest-N messages of one session, newest first (caller reverses to asc).

    Filtered by conversation_id ONLY (never agent_id — see module docstring),
    excluding compacted-away rows and, by default, tool_call noise.
    ``before`` is a ``(created_at, id)`` cursor selecting strictly-older rows.
    """
    q = select(ChatMessage).where(
        ChatMessage.conversation_id == str(conversation_id),
        ChatMessage.compacted_into.is_(None),
    )
    if not include_tool_calls:
        q = q.where(ChatMessage.role.in_(_VISIBLE_ROLES))
    if before is not None:
        bc, bi = before
        q = q.where(
            or_(
                ChatMessage.created_at < bc,
                and_(ChatMessage.created_at == bc, ChatMessage.id < bi),
            )
        )
    return q.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(limit)


async def fetch_session_messages(
    db: AsyncSession,
    conversation_id: str,
    *,
    limit: int,
    before: tuple | None = None,
    include_tool_calls: bool = False,
) -> list[ChatMessage]:
    """Return the page in ascending (chronological) order."""
    rows = (
        await db.execute(
            build_session_messages_query(
                conversation_id,
                limit=limit,
                before=before,
                include_tool_calls=include_tool_calls,
            )
        )
    ).scalars().all()
    return list(reversed(rows))


# ── Batch identity resolution (avoid N+1) ──────────────────────────────────


async def resolve_senders(db: AsyncSession, messages: list[ChatMessage]) -> dict:
    """Return ``{"users": {id:name}, "agents": {id:name}, "participants": {pid:name}}``
    for rendering message authors without per-row queries."""
    user_ids, agent_ids, part_ids = set(), set(), set()
    for m in messages:
        if m.user_id:
            user_ids.add(m.user_id)
        if m.agent_id:
            agent_ids.add(m.agent_id)
        if getattr(m, "participant_id", None):
            part_ids.add(m.participant_id)

    users: dict = {}
    agents: dict = {}
    participants: dict = {}

    if user_ids:
        for u in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars():
            users[u.id] = u.display_name or "user"
    # Participants may point at agents (A2A author) — resolve their ref name too.
    parts = []
    if part_ids:
        parts = (await db.execute(select(Participant).where(Participant.id.in_(part_ids)))).scalars().all()
        for p in parts:
            if getattr(p, "ref_id", None) and p.type == "agent":
                agent_ids.add(p.ref_id)
    if agent_ids:
        for a in (await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars():
            agents[a.id] = a.name
    for p in parts:
        ref = getattr(p, "ref_id", None)
        if p.type == "agent" and ref in agents:
            participants[p.id] = agents[ref]
        elif p.type == "user" and ref in users:
            participants[p.id] = users[ref]

    return {"users": users, "agents": agents, "participants": participants}


async def resolve_session_counterparts(
    db: AsyncSession, agent_id: uuid.UUID, sessions: list[ChatSession]
) -> dict:
    """Return ``{session_id: label}`` describing the other party of each session:
    P2P -> user display name, A2A -> the peer agent's name, trigger -> 'self'."""
    user_ids, agent_ids = set(), set()
    for s in sessions:
        if s.source_channel == "trigger":
            continue
        if s.source_channel == "agent":
            other = s.peer_agent_id if s.agent_id == agent_id else s.agent_id
            if other:
                agent_ids.add(other)
        else:
            if s.user_id:
                user_ids.add(s.user_id)

    users, agents = {}, {}
    if user_ids:
        for u in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars():
            users[u.id] = u.display_name or "user"
    if agent_ids:
        for a in (await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars():
            agents[a.id] = a.name

    out: dict = {}
    for s in sessions:
        if s.source_channel == "trigger":
            out[s.id] = "self"
        elif s.source_channel == "agent":
            other = s.peer_agent_id if s.agent_id == agent_id else s.agent_id
            out[s.id] = agents.get(other, "agent")
        else:
            out[s.id] = users.get(s.user_id, "user")
    return out


# ── List & search ──────────────────────────────────────────────────────────


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


async def fetch_sessions(
    db: AsyncSession,
    where,
    *,
    channel: str | None = None,
    title_query: str | None = None,
    since=None,
    until=None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[ChatSession], int]:
    """Return ``(rows, total)`` for the list tool, ordered like the REST UI."""
    conds = [where]
    if channel and channel != "all":
        conds.append(ChatSession.source_channel == channel)
    if title_query:
        pat = f"%{_escape_like(title_query)}%"
        conds.append(
            or_(
                ChatSession.title.ilike(pat, escape="\\"),
                ChatSession.group_name.ilike(pat, escape="\\"),
            )
        )
    if since is not None:
        conds.append(ChatSession.last_message_at >= since)
    if until is not None:
        conds.append(ChatSession.last_message_at <= until)

    from sqlalchemy import func

    total = (
        await db.execute(select(func.count()).select_from(ChatSession).where(*conds))
    ).scalar_one()
    rows = (
        await db.execute(
            select(ChatSession)
            .where(*conds)
            .order_by(
                ChatSession.last_message_at.desc().nulls_last(),
                ChatSession.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return list(rows), int(total)


async def count_messages_per_session(db: AsyncSession, session_ids: list) -> dict:
    """Single grouped aggregate (no N+1). Keyed by conversation_id string."""
    if not session_ids:
        return {}
    from sqlalchemy import func

    str_ids = [str(sid) for sid in session_ids]
    rows = await db.execute(
        select(ChatMessage.conversation_id, func.count(ChatMessage.id))
        .where(
            ChatMessage.conversation_id.in_(str_ids),
            ChatMessage.compacted_into.is_(None),
            ChatMessage.role.in_(_VISIBLE_ROLES),
        )
        .group_by(ChatMessage.conversation_id)
    )
    return {cid: int(n) for cid, n in rows.all()}


async def search_messages(
    db: AsyncSession,
    session_ids: list,
    keyword: str,
    *,
    limit: int = 20,
) -> list[ChatMessage]:
    """ILIKE search over messages of the already-permission-narrowed sessions.

    Caller MUST pass the session_ids from the resolved scope — never the whole
    table. content has no index; narrowing by conversation_id (btree) + hard
    limit keeps this acceptable for MVP (handler logs hit count + latency).
    """
    if not session_ids or not keyword:
        return []
    str_ids = [str(sid) for sid in session_ids]
    pattern = f"%{_escape_like(keyword)}%"
    rows = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.conversation_id.in_(str_ids),
            ChatMessage.compacted_into.is_(None),
            ChatMessage.role.in_(_VISIBLE_ROLES),
            ChatMessage.content.ilike(pattern, escape="\\"),
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())
