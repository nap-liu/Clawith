"""Session-introspection tools: scope/permission core + handlers + dispatch.

Covers the design contract in
docs/specs/2026-06-17-session-introspection-tools-design.md, including the
security regressions surfaced in audit (CRITICAL-1/2, HIGH-1/2/3, A2).

Runs against the real Postgres ``clawith_test`` DB (no sqlite). FK-violating
rows (synthetic compacted_into) use ``SET session_replication_role = replica``
like the existing chat_sessions tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction  # noqa: F401 — register FK target table
from app.models.chat_session import ChatSession
from app.models.participant import Participant  # noqa: F401 — register for mapper config
from app.models.tenant import Tenant
from app.models.user import Identity, User

from app.services import session_query as sq
from app.services.session_query import (
    SCOPE_ALL,
    SCOPE_AUTONOMOUS,
    SCOPE_DENY,
    SCOPE_OWN,
    build_owned_sessions_predicate,
    resolve_human_viewer_access,
    resolve_scope,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


# ── seed helpers (FK-safe: real tenant/identity/user/agent rows) ───────────


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def _seed_user(role: str = "member", tenant_id=None, name: str = "U") -> User:
    async with async_session() as db:
        ident = Identity(
            username=f"u_{uuid.uuid4().hex[:12]}",
            email=f"{uuid.uuid4().hex[:12]}@t.local",
            password_hash="x",
        )
        db.add(ident)
        await db.flush()
        u = User(identity_id=ident.id, display_name=name, role=role, is_active=True, tenant_id=tenant_id)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u


async def _seed_agent(creator_id, tenant_id=None, access_mode: str = "company", name: str = "A") -> Agent:
    async with async_session() as db:
        a = Agent(name=name, creator_id=creator_id, tenant_id=tenant_id, access_mode=access_mode)
        db.add(a)
        await db.commit()
        await db.refresh(a)
        return a


async def _seed_session(
    agent_id, user_id, *, channel: str = "web", peer=None, group: bool = False, title: str = "t"
) -> ChatSession:
    async with async_session() as db:
        s = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            source_channel=channel,
            peer_agent_id=peer,
            is_group=group,
            title=title,
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


async def _raw_insert_compacted_message(agent_id, user_id, conv_id) -> None:
    """Insert a message already folded into a compaction (FK-bypass to fake id)."""
    async with async_session() as db:
        await db.execute(text("SET session_replication_role = replica"))
        await db.execute(
            text(
                "INSERT INTO chat_messages"
                " (id, agent_id, user_id, role, content, conversation_id, compacted_into, created_at)"
                " VALUES (:id, :agent_id, :user_id, 'assistant', 'folded', :conv, :comp, :ca)"
            ),
            {
                "id": str(uuid.uuid4()),
                "agent_id": str(agent_id),
                "user_id": str(user_id),
                "conv": str(conv_id),
                "comp": str(uuid.uuid4()),
                "ca": datetime.now(timezone.utc),
            },
        )
        await db.commit()
        await db.execute(text("SET session_replication_role = DEFAULT"))
        await db.commit()


# ── Task 1: OWNED predicate ────────────────────────────────────────────────


async def test_owned_predicate_covers_three_kinds_and_excludes_others():
    t = await _seed_tenant()
    owner = await _seed_user(tenant_id=t.id)
    agent = await _seed_agent(owner.id, tenant_id=t.id)
    other_agent = await _seed_agent(owner.id, tenant_id=t.id, name="Other")
    peer = await _seed_agent(owner.id, tenant_id=t.id, name="Peer")

    s_web = await _seed_session(agent.id, owner.id, channel="web")
    s_trig = await _seed_session(agent.id, owner.id, channel="trigger")
    s_a2a_self = await _seed_session(agent.id, owner.id, channel="agent", peer=peer.id)
    s_a2a_peer = await _seed_session(peer.id, owner.id, channel="agent", peer=agent.id)
    s_foreign = await _seed_session(other_agent.id, owner.id, channel="web")
    # peer_agent_id == agent but channel != 'agent' -> must NOT be owned
    s_trap = await _seed_session(other_agent.id, owner.id, channel="web", peer=agent.id)

    async with async_session() as db:
        ids = set(
            (await db.execute(select(ChatSession.id).where(build_owned_sessions_predicate(agent.id)))).scalars()
        )
    assert {s_web.id, s_trig.id, s_a2a_self.id, s_a2a_peer.id} <= ids
    assert s_foreign.id not in ids
    assert s_trap.id not in ids


async def test_human_viewer_access_maps_from_authoritative_helper():
    t1 = await _seed_tenant()
    creator = await _seed_user(role="member", tenant_id=t1.id)
    private_agent = await _seed_agent(creator.id, tenant_id=t1.id, access_mode="private")
    company_agent = await _seed_agent(creator.id, tenant_id=t1.id, access_mode="company")
    admin = await _seed_user(role="platform_admin", tenant_id=t1.id)
    org_admin = await _seed_user(role="org_admin", tenant_id=t1.id)
    member = await _seed_user(role="member", tenant_id=t1.id)
    cross = await _seed_user(role="platform_admin", tenant_id=(await _seed_tenant()).id)

    async with async_session() as db:
        # creator + platform_admin manage everything (same tenant) -> ALL
        assert await resolve_human_viewer_access(db, creator.id, private_agent) == SCOPE_ALL
        assert await resolve_human_viewer_access(db, admin.id, private_agent) == SCOPE_ALL
        # org_admin manages company agents (ALL) but has NO access to others' private (DENY) — HIGH-1
        assert await resolve_human_viewer_access(db, org_admin.id, company_agent) == SCOPE_ALL
        assert await resolve_human_viewer_access(db, org_admin.id, private_agent) == SCOPE_DENY
        # plain member on a company agent -> 'use' -> OWN (only own sessions)
        assert await resolve_human_viewer_access(db, member.id, company_agent) == SCOPE_OWN
        # member on others' private agent: no access -> DENY
        assert await resolve_human_viewer_access(db, member.id, private_agent) == SCOPE_DENY
        # cross-tenant even platform_admin -> DENY (tenant check first) — HIGH-2
        assert await resolve_human_viewer_access(db, cross.id, private_agent) == SCOPE_DENY


# ── Task 2: scope resolution + per-scope predicates + message window ────────


async def test_resolve_scope_discriminates_by_channel():
    t = await _seed_tenant()
    admin = await _seed_user(role="platform_admin", tenant_id=t.id)
    agent = await _seed_agent(admin.id, tenant_id=t.id, access_mode="company")
    peer = await _seed_agent(admin.id, tenant_id=t.id, name="Peer")
    web = await _seed_session(agent.id, admin.id, channel="web")
    a2a = await _seed_session(agent.id, admin.id, channel="agent", peer=peer.id)
    trig = await _seed_session(agent.id, admin.id, channel="trigger")

    async with async_session() as db:
        assert (await resolve_scope(db, agent, str(web.id), admin.id))[0] == SCOPE_ALL
        # non-human turns are autonomous EVEN THOUGH user_id is an admin creator
        assert (await resolve_scope(db, agent, str(a2a.id), admin.id))[0] == SCOPE_AUTONOMOUS
        assert (await resolve_scope(db, agent, str(trig.id), admin.id))[0] == SCOPE_AUTONOMOUS
        # missing ctx -> autonomous minimum
        assert (await resolve_scope(db, agent, str(uuid.uuid4()), admin.id))[0] == SCOPE_AUTONOMOUS


async def test_scope_predicates_select_right_sessions():
    t = await _seed_tenant()
    u = await _seed_user(role="member", tenant_id=t.id)
    u2 = await _seed_user(role="member", tenant_id=t.id)
    agent = await _seed_agent(u.id, tenant_id=t.id, access_mode="company")
    peer = await _seed_agent(u.id, tenant_id=t.id, name="Peer")
    mine = await _seed_session(agent.id, u.id, channel="web")
    others = await _seed_session(agent.id, u2.id, channel="web")
    a2a = await _seed_session(agent.id, u.id, channel="agent", peer=peer.id)
    trig = await _seed_session(agent.id, u.id, channel="trigger")

    async with async_session() as db:
        own_ids = set(
            (await db.execute(select(ChatSession.id).where(sq._own_participated_where(agent.id, u.id)))).scalars()
        )
        assert mine.id in own_ids
        assert others.id not in own_ids
        assert a2a.id not in own_ids and trig.id not in own_ids

        auto_ids = set(
            (await db.execute(select(ChatSession.id).where(sq._autonomous_where(agent.id, str(a2a.id))))).scalars()
        )
        assert a2a.id in auto_ids and trig.id in auto_ids
        assert mine.id not in auto_ids and others.id not in auto_ids


async def test_fetch_messages_by_conversation_id_excludes_tool_call_and_compacted():
    t = await _seed_tenant()
    u = await _seed_user(tenant_id=t.id)
    a_small, a_big = sorted([await _seed_agent(u.id, tenant_id=t.id), await _seed_agent(u.id, tenant_id=t.id)], key=lambda x: str(x.id))
    conv = await _seed_session(a_small.id, u.id, channel="agent", peer=a_big.id)
    base = datetime.now(timezone.utc)
    # A2A rows are all written under the normalized (smaller-UUID) agent id
    await _seed_message(a_small.id, u.id, conv.id, "user", "hello", created_at=base)
    await _seed_message(a_small.id, u.id, conv.id, "assistant", "hi back", created_at=base + timedelta(seconds=1))
    await _seed_message(a_small.id, u.id, conv.id, "tool_call", '{"name":"x"}', created_at=base + timedelta(seconds=2))
    await _raw_insert_compacted_message(a_small.id, u.id, conv.id)

    async with async_session() as db:
        msgs = await sq.fetch_session_messages(db, str(conv.id), limit=10, include_tool_calls=False)
    roles = [m.role for m in msgs]
    assert roles == ["user", "assistant"]  # ascending, tool_call + compacted excluded
    assert all("folded" not in m.content for m in msgs)
