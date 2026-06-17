"""Tests for app/mcp_server/tools.py — the 5 MCP tool functions.

Security invariants tested:
- list_agents: only returns agents visible to the authenticated user (cross-tenant isolation).
- list_sessions (SCOPE_OWN): member with 'use' access sees only their own sessions,
  NOT another user's P2P session with the same agent.
- get_session: own session OK; other-user session → _DENY; cross-agent session → _DENY;
  malformed id → _DENY.
- chat_with_agent: three session-path semantics; persistence; no /new hint on LLM error.

asyncio_mode = "auto" (pyproject); no @pytest.mark.asyncio needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction  # noqa: F401 — register FK target table
from app.models.chat_session import ChatSession
from app.models.participant import Participant  # noqa: F401 — register for mapper config
from app.models.tenant import Tenant
from app.models.user import Identity, User


# ── engine isolation ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


# ── fake MCP Context ──────────────────────────────────────────────────────────


class _FakeTransport:
    def __init__(self, headers: dict):
        self.headers = headers


class _FakeRequestContext:
    def __init__(self, headers: dict):
        self.transport = _FakeTransport(headers)


class _FakeCtx:
    """Minimal stand-in for mcp.server.fastmcp.Context."""

    def __init__(self, headers: dict | None = None):
        self.request_context = _FakeRequestContext(headers or {})
        self._progress_calls: list = []

    async def report_progress(self, *, progress: float, total: float, message: str = "") -> None:
        self._progress_calls.append({"progress": progress, "total": total, "message": message})


# ── seed helpers ──────────────────────────────────────────────────────────────


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def _seed_user(tenant_id=None, role: str = "member", name: str = "U") -> User:
    async with async_session() as db:
        ident = Identity(
            username=f"u_{uuid.uuid4().hex[:12]}",
            email=f"{uuid.uuid4().hex[:12]}@t.local",
            password_hash="x",
        )
        db.add(ident)
        await db.flush()
        u = User(
            identity_id=ident.id,
            display_name=name,
            role=role,
            is_active=True,
            tenant_id=tenant_id,
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u


async def _seed_agent(
    creator_id, tenant_id=None, access_mode: str = "company", name: str = "A"
) -> Agent:
    async with async_session() as db:
        a = Agent(name=name, creator_id=creator_id, tenant_id=tenant_id, access_mode=access_mode)
        db.add(a)
        await db.commit()
        await db.refresh(a)
        return a


async def _seed_session(
    agent_id,
    user_id,
    *,
    channel: str = "web",
    peer=None,
    group: bool = False,
    title: str = "t",
    external_conv_id: str | None = None,
) -> ChatSession:
    async with async_session() as db:
        s = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            source_channel=channel,
            peer_agent_id=peer,
            is_group=group,
            title=title,
            external_conv_id=external_conv_id,
            last_message_at=datetime.now(timezone.utc),
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
        return s


async def _seed_message(agent_id, user_id, conv_id, role, content, *, created_at=None) -> uuid.UUID:
    async with async_session() as db:
        m = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            role=role,
            content=content,
            conversation_id=str(conv_id),
            created_at=created_at or datetime.now(timezone.utc),
        )
        db.add(m)
        await db.commit()
        await db.refresh(m)
        return m.id


async def _issue_pat_for(user: User) -> str:
    """Issue a PAT and return the plaintext token."""
    from app.services.pat_service import issue_pat

    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="test-mcp")
    return token


def _ctx(token: str) -> _FakeCtx:
    return _FakeCtx(headers={"authorization": f"Bearer {token}"})


def _unauth_ctx() -> _FakeCtx:
    return _FakeCtx(headers={})


# ── Task 6: list_agents ───────────────────────────────────────────────────────


async def test_list_agents_returns_only_visible_agents():
    """list_agents must not leak cross-tenant agents."""
    from app.mcp_server.tools import list_agents

    tenant_a = await _seed_tenant()
    tenant_b = await _seed_tenant()

    user = await _seed_user(tenant_id=tenant_a.id)
    token = await _issue_pat_for(user)

    # Agent in the user's own tenant — must appear.
    await _seed_agent(user.id, tenant_id=tenant_a.id, name="OwnAgent")
    # Agent in a different tenant — must NOT appear (cross-tenant isolation).
    creator_b = await _seed_user(tenant_id=tenant_b.id)
    _agent_other_tenant = await _seed_agent(creator_b.id, tenant_id=tenant_b.id, name="XTenantAgent")

    result = await list_agents(_ctx(token))

    assert "OwnAgent" in result
    assert "XTenantAgent" not in result


async def test_list_agents_unauth_returns_unauth_message():
    from app.mcp_server.tools import list_agents, _UNAUTH

    result = await list_agents(_unauth_ctx())
    assert result == _UNAUTH


async def test_list_agents_keyword_filters():
    """list_agents with keyword only returns matching agents."""
    from app.mcp_server.tools import list_agents

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)

    await _seed_agent(user.id, tenant_id=t.id, name="AlphaBot")
    await _seed_agent(user.id, tenant_id=t.id, name="BetaBot")

    result = await list_agents(_ctx(token), keyword="Alpha")
    assert "AlphaBot" in result
    assert "BetaBot" not in result


# ── Task 7: get_agent_info ────────────────────────────────────────────────────


async def test_get_agent_info_by_name_returns_profile():
    from app.mcp_server.tools import get_agent_info

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)
    await _seed_agent(user.id, tenant_id=t.id, name="InfoAgent")

    result = await get_agent_info(_ctx(token), agent="InfoAgent")
    assert "InfoAgent" in result


async def test_get_agent_info_cross_tenant_not_visible():
    """A user must not be able to get info on an agent from another tenant."""
    from app.mcp_server.tools import get_agent_info

    t_a = await _seed_tenant()
    t_b = await _seed_tenant()
    user_a = await _seed_user(tenant_id=t_a.id)
    token = await _issue_pat_for(user_a)

    creator_b = await _seed_user(tenant_id=t_b.id)
    _foreign_agent = await _seed_agent(creator_b.id, tenant_id=t_b.id, name="ForeignAgent")

    result = await get_agent_info(_ctx(token), agent="ForeignAgent")
    # Should be the "not found" denial, not a profile
    assert "ForeignAgent" not in result or "无权" in result or "找不到" in result


async def test_get_agent_info_unauth():
    from app.mcp_server.tools import get_agent_info, _UNAUTH

    result = await get_agent_info(_unauth_ctx(), agent="anyone")
    assert result == _UNAUTH


# ── Task 8: list_sessions ─────────────────────────────────────────────────────


async def test_list_sessions_member_cannot_see_other_users_session():
    """CRITICAL security test: member with 'use' access to a company agent must
    only see sessions where THEY personally participated, not other users' P2P
    sessions with that agent."""
    from app.mcp_server.tools import list_sessions

    t = await _seed_tenant()

    # The user who will call list_sessions
    viewer = await _seed_user(tenant_id=t.id, role="member", name="Viewer")
    viewer_token = await _issue_pat_for(viewer)

    # A different user in the same tenant
    other_user = await _seed_user(tenant_id=t.id, role="member", name="OtherUser")

    # Agent created by a third party (not viewer, not other_user) — viewer has 'use' access only
    agent_creator = await _seed_user(tenant_id=t.id, role="member", name="AgentCreator")
    agent = await _seed_agent(agent_creator.id, tenant_id=t.id, access_mode="company", name="SvcAgent")

    # Seed a session that belongs to 'viewer' with this agent
    viewer_sess = await _seed_session(agent.id, viewer.id, channel="web", title="ViewerConvo")
    await _seed_message(agent.id, viewer.id, viewer_sess.id, "user", "hello from viewer")

    # Seed a session that belongs to 'other_user' with the same agent
    other_sess = await _seed_session(agent.id, other_user.id, channel="web", title="OtherConvo")
    await _seed_message(agent.id, other_user.id, other_sess.id, "user", "hello from other")

    result = await list_sessions(_ctx(viewer_token), agent="SvcAgent")

    # viewer's session must appear; other user's session must NOT appear
    assert str(viewer_sess.id) in result
    assert str(other_sess.id) not in result


async def test_list_sessions_admin_sees_all_sessions():
    """org_admin has manage access → SCOPE_ALL → sees all sessions."""
    from app.mcp_server.tools import list_sessions

    t = await _seed_tenant()
    admin = await _seed_user(tenant_id=t.id, role="org_admin", name="Admin")
    admin_token = await _issue_pat_for(admin)

    member = await _seed_user(tenant_id=t.id, role="member", name="Member")
    agent = await _seed_agent(admin.id, tenant_id=t.id, name="AdminAgent")

    member_sess = await _seed_session(agent.id, member.id, channel="web", title="MemberConvo")
    await _seed_message(agent.id, member.id, member_sess.id, "user", "hi")

    result = await list_sessions(_ctx(admin_token), agent="AdminAgent")
    assert str(member_sess.id) in result


async def test_list_sessions_invalid_channel_returns_error():
    from app.mcp_server.tools import list_sessions

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)
    _agent = await _seed_agent(user.id, tenant_id=t.id, name="ChanAgent")

    result = await list_sessions(_ctx(token), channel="totally_invalid_channel")
    assert "非法" in result or "invalid" in result.lower() or "❌" in result


async def test_list_sessions_unauth():
    from app.mcp_server.tools import list_sessions, _UNAUTH

    result = await list_sessions(_unauth_ctx())
    assert result == _UNAUTH


# ── Task 9: get_session ───────────────────────────────────────────────────────


async def test_get_session_own_session_ok():
    """User can read a session they personally participated in."""
    from app.mcp_server.tools import get_session

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)
    agent = await _seed_agent(user.id, tenant_id=t.id, name="GSAgent")

    sess = await _seed_session(agent.id, user.id, channel="web")
    await _seed_message(agent.id, user.id, sess.id, "user", "hello")
    await _seed_message(agent.id, user.id, sess.id, "assistant", "hi there")

    result = await get_session(_ctx(token), session_id=str(sess.id))
    assert "hello" in result or "hi there" in result


async def test_get_session_other_users_session_is_denied():
    """User must NOT be able to read another user's session — denial without leaking existence."""
    from app.mcp_server.tools import get_session
    from app.mcp_server.tools import _DENY

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id, role="member")
    token = await _issue_pat_for(user)

    other_user = await _seed_user(tenant_id=t.id, role="member")
    agent = await _seed_agent(other_user.id, tenant_id=t.id, access_mode="company")

    other_sess = await _seed_session(agent.id, other_user.id, channel="web")
    await _seed_message(agent.id, other_user.id, other_sess.id, "user", "secret content")

    result = await get_session(_ctx(token), session_id=str(other_sess.id))
    assert result == _DENY
    assert "secret content" not in result


async def test_get_session_malformed_id_is_denied():
    """Garbage session_id → _DENY (no crash, no info leak)."""
    from app.mcp_server.tools import get_session, _DENY

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)

    result = await get_session(_ctx(token), session_id="not-a-valid-uuid!!!")
    assert result == _DENY


async def test_get_session_nonexistent_id_is_denied():
    """A valid UUID that doesn't exist in DB → _DENY (existence non-revealing)."""
    from app.mcp_server.tools import get_session, _DENY

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)

    result = await get_session(_ctx(token), session_id=str(uuid.uuid4()))
    assert result == _DENY


async def test_get_session_cross_agent_session_is_denied():
    """Session belonging to a different agent the user cannot access → _DENY."""
    from app.mcp_server.tools import get_session, _DENY

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id, role="member")
    token = await _issue_pat_for(user)

    # Create an agent owned by another user with private access mode
    other_creator = await _seed_user(tenant_id=t.id)
    private_agent = await _seed_agent(other_creator.id, tenant_id=t.id, access_mode="private")

    private_sess = await _seed_session(private_agent.id, other_creator.id, channel="web")

    result = await get_session(_ctx(token), session_id=str(private_sess.id))
    assert result == _DENY


async def test_get_session_unauth():
    from app.mcp_server.tools import get_session, _UNAUTH

    result = await get_session(_unauth_ctx(), session_id=str(uuid.uuid4()))
    assert result == _UNAUTH


# ── Task 10: chat_with_agent ──────────────────────────────────────────────────

# All chat tests patch _call_agent_llm to avoid real LLM calls.
_FAKE_REPLY = "I am a mock reply."


async def test_chat_default_path_continues_same_mcp_session():
    """Two consecutive chats with the same agent (no new_conversation) → same session."""
    from app.mcp_server.tools import chat_with_agent

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)
    agent = await _seed_agent(user.id, tenant_id=t.id, name="ChatAgent")

    with patch("app.services.channel_llm._call_agent_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _FAKE_REPLY
        reply1 = await chat_with_agent(_ctx(token), message="first", agent="ChatAgent")
        reply2 = await chat_with_agent(_ctx(token), message="second", agent="ChatAgent")

    assert reply1 == _FAKE_REPLY
    assert reply2 == _FAKE_REPLY

    # Both chats should land in the same mcp session for this (agent, user) pair
    from sqlalchemy import select

    async with async_session() as db:
        sessions = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent.id,
                    ChatSession.user_id == user.id,
                    ChatSession.source_channel == "mcp",
                )
            )
        ).scalars().all()
    assert len(sessions) == 1, "Two consecutive chats must reuse the same mcp session"


async def test_chat_new_conversation_creates_new_session():
    """new_conversation=True always creates a fresh session."""
    from app.mcp_server.tools import chat_with_agent

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)
    agent = await _seed_agent(user.id, tenant_id=t.id, name="NewConvAgent")

    with patch("app.services.channel_llm._call_agent_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _FAKE_REPLY
        await chat_with_agent(_ctx(token), message="first", agent="NewConvAgent")
        await chat_with_agent(
            _ctx(token), message="second", agent="NewConvAgent", new_conversation=True
        )

    from sqlalchemy import select

    async with async_session() as db:
        sessions = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent.id,
                    ChatSession.user_id == user.id,
                    ChatSession.source_channel == "mcp",
                )
            )
        ).scalars().all()
    assert len(sessions) == 2, "new_conversation=True must create a new session"


async def test_chat_session_id_path_does_not_rewrite_user_id():
    """Jumping to an existing session (e.g. a feishu session) must NOT change user_id.

    This is the critical safety property: find_or_create_channel_session would
    re-attribute user_id on P2P sessions; the session_id path must use plain SELECT.
    """
    from app.mcp_server.tools import chat_with_agent

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    original_owner = await _seed_user(tenant_id=t.id)
    agent = await _seed_agent(user.id, tenant_id=t.id, name="JumpAgent")

    # Create a feishu session owned by original_owner (the creator of the agent here is user,
    # but the session is "owned" by original_owner as the chat participant).
    # We use the agent's creator as the session user so the MCP user (same tenant admin)
    # can access it via SCOPE_ALL (org_admin access).
    admin_user = await _seed_user(tenant_id=t.id, role="org_admin")
    admin_token = await _issue_pat_for(admin_user)

    feishu_sess = await _seed_session(
        agent.id, original_owner.id, channel="feishu", title="Feishu Convo"
    )
    original_user_id = feishu_sess.user_id

    with patch("app.services.channel_llm._call_agent_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _FAKE_REPLY
        await chat_with_agent(
            _ctx(admin_token),
            message="hello",
            agent="JumpAgent",
            session_id=str(feishu_sess.id),
        )

    # Check that user_id was NOT changed
    from sqlalchemy import select

    async with async_session() as db:
        refreshed = (
            await db.execute(select(ChatSession).where(ChatSession.id == feishu_sess.id))
        ).scalar_one()
    assert refreshed.user_id == original_user_id, (
        "chat_with_agent session_id path must not rewrite user_id of the existing session"
    )


async def test_chat_persists_user_and_assistant_messages():
    """After one chat round, DB must have both role='user' and role='assistant' rows,
    with assistant.created_at >= user.created_at (ordering invariant)."""
    from app.mcp_server.tools import chat_with_agent

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)
    agent = await _seed_agent(user.id, tenant_id=t.id, name="PersistAgent")

    with patch("app.services.channel_llm._call_agent_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "I am the reply"
        await chat_with_agent(_ctx(token), message="ping", agent="PersistAgent")

    from sqlalchemy import select

    async with async_session() as db:
        sess = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent.id,
                    ChatSession.user_id == user.id,
                    ChatSession.source_channel == "mcp",
                )
            )
        ).scalars().first()
        assert sess is not None
        msgs = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(sess.id),
                    ChatMessage.compacted_into.is_(None),
                )
            )
        ).scalars().all()

    roles = {m.role for m in msgs}
    assert "user" in roles, "Must persist a user message row"
    assert "assistant" in roles, "Must persist an assistant message row"

    user_msg = next(m for m in msgs if m.role == "user")
    asst_msg = next(m for m in msgs if m.role == "assistant")
    # assistant must be stamped at or after user (persist_assistant_reply opens its own session)
    assert asst_msg.created_at >= user_msg.created_at, (
        "assistant row created_at must be >= user row created_at"
    )


async def test_chat_error_reply_has_no_recovery_hint():
    """When LLM returns [Error] ..., the MCP reply must NOT contain the IM /new hint.

    This validates recovery_hint=None is wired through correctly.
    """
    from app.mcp_server.tools import chat_with_agent
    from app.services.channel_llm import _IM_LLM_RECOVERY_HINT

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)
    await _seed_agent(user.id, tenant_id=t.id, name="ErrorAgent")

    with patch("app.services.channel_llm._call_agent_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "[Error] something went wrong"
        result = await chat_with_agent(_ctx(token), message="break", agent="ErrorAgent")

    assert "/new" not in result
    assert _IM_LLM_RECOVERY_HINT not in result
    assert result.startswith("[Error]")


async def test_chat_unauth():
    from app.mcp_server.tools import chat_with_agent, _UNAUTH

    result = await chat_with_agent(_unauth_ctx(), message="hello", agent="anyone")
    assert result == _UNAUTH


async def test_chat_no_agent_no_session_returns_error():
    """chat_with_agent with no agent and no session_id must return an error."""
    from app.mcp_server.tools import chat_with_agent

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)

    result = await chat_with_agent(_ctx(token), message="hello")
    assert "❌" in result


async def test_chat_unknown_agent_returns_error():
    """chat_with_agent with an agent name that doesn't exist → error."""
    from app.mcp_server.tools import chat_with_agent

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)

    result = await chat_with_agent(_ctx(token), message="hello", agent="GhostAgent")
    assert "❌" in result


async def test_chat_session_id_deny_wrong_user():
    """chat_with_agent with session_id belonging to a different user → _DENY."""
    from app.mcp_server.tools import chat_with_agent, _DENY

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id, role="member")
    token = await _issue_pat_for(user)

    other_user = await _seed_user(tenant_id=t.id, role="member")
    agent = await _seed_agent(other_user.id, tenant_id=t.id, access_mode="company")

    # Session belonging to other_user
    other_sess = await _seed_session(agent.id, other_user.id, channel="mcp")

    result = await chat_with_agent(
        _ctx(token), message="hi", agent=agent.name, session_id=str(other_sess.id)
    )
    # user is a plain member on a company agent → SCOPE_OWN → can't access other user's session
    assert result == _DENY


# ── A2A two-sided permission (the subtlest get_session logic) ──────────────────


async def test_get_session_a2a_peer_side_manage_grants_access():
    """A2A session anchored on agent X (viewer has NO access) but peer is agent Y
    the viewer manages → _session_max_scope picks the peer side → read allowed."""
    from app.mcp_server.tools import get_session, _DENY

    tenant = await _seed_tenant()
    viewer = await _seed_user(tenant_id=tenant.id, name="Viewer")
    other = await _seed_user(tenant_id=tenant.id, name="Other")
    token = await _issue_pat_for(viewer)

    # Anchor X: created by other, private → viewer access = None.
    agent_x = await _seed_agent(other.id, tenant_id=tenant.id, access_mode="private", name="AnchorX")
    # Peer Y: created by viewer → viewer manages it.
    agent_y = await _seed_agent(viewer.id, tenant_id=tenant.id, access_mode="private", name="PeerY")

    sess = await _seed_session(agent_x.id, other.id, channel="agent", peer=agent_y.id)
    await _seed_message(agent_x.id, other.id, sess.id, "user", "a2a hello world")

    result = await get_session(_ctx(token), str(sess.id))
    assert result != _DENY, "manage on the peer-side agent must grant read access"
    assert "a2a hello world" in result


async def test_get_session_a2a_no_access_either_side_denied():
    """A2A session where the viewer manages neither side → uniform _DENY."""
    from app.mcp_server.tools import get_session, _DENY

    tenant = await _seed_tenant()
    viewer = await _seed_user(tenant_id=tenant.id, name="Viewer")
    other = await _seed_user(tenant_id=tenant.id, name="Other")
    token = await _issue_pat_for(viewer)

    agent_x = await _seed_agent(other.id, tenant_id=tenant.id, access_mode="private", name="X2")
    agent_y = await _seed_agent(other.id, tenant_id=tenant.id, access_mode="private", name="Y2")
    sess = await _seed_session(agent_x.id, other.id, channel="agent", peer=agent_y.id)
    await _seed_message(agent_x.id, other.id, sess.id, "user", "secret a2a")

    result = await get_session(_ctx(token), str(sess.id))
    assert result == _DENY


# ── chat_with_agent cross-agent session_id mismatch ───────────────────────────


async def test_chat_with_agent_cross_agent_session_id_mismatch(monkeypatch):
    """Explicit agent that doesn't match the session's agent → mismatch error,
    and the LLM is never invoked."""
    from app.mcp_server import tools

    tenant = await _seed_tenant()
    viewer = await _seed_user(tenant_id=tenant.id, name="V")
    creator = await _seed_user(tenant_id=tenant.id, name="C")
    token = await _issue_pat_for(viewer)

    # AgentX must exist & be visible so it resolves by name (then mismatches the session's agent).
    await _seed_agent(creator.id, tenant_id=tenant.id, access_mode="company", name="AgentX")
    agent_y = await _seed_agent(creator.id, tenant_id=tenant.id, access_mode="company", name="AgentY")
    # Session owned by Y that the viewer participated in (so scope passes before mismatch).
    sess_y = await _seed_session(agent_y.id, viewer.id, channel="web")

    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        return "must not be called"

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", _boom)

    result = await tools.chat_with_agent(
        _ctx(token), message="hi", agent="AgentX", session_id=str(sess_y.id)
    )
    assert "不一致" in result
    assert called["n"] == 0, "LLM must not run when session_id's agent != requested agent"
