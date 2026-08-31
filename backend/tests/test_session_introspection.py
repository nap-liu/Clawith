"""Session-introspection tools: scope/permission core + handlers + dispatch.

Covers the design contract in
docs/specs/2026-06-17-session-introspection-tools-design.md, including the
security regressions surfaced in audit (CRITICAL-1/2, HIGH-1/2/3, A2).

Runs against the real Postgres ``clawith_test`` DB (no sqlite). FK-violating
rows (synthetic compacted_into) use ``SET session_replication_role = replica``
like the existing chat_sessions tests.
"""

from __future__ import annotations

import base64
import uuid
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.database import async_session, engine
from app.models.agent import Agent, AgentPermission
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
from session_introspection_support import (
    _isolate_async_engine_between_tests,
    _raw_insert_compacted_message,
    _seed_agent,
    _seed_legacy_malformed_session,
    _seed_message,
    _seed_session,
    _seed_tenant,
    _seed_user,
)

# asyncio_mode = "auto" (pyproject) auto-collects async tests; no module marker
# needed (and a marker would warn on the sync formatting test).


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
    s_trap = await _seed_legacy_malformed_session(
        other_agent.id,
        owner.id,
        channel="web",
        peer=agent.id,
    )

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
    identity_admin = await _seed_user(
        role="member",
        tenant_id=t1.id,
        is_platform_admin=True,
    )
    org_admin = await _seed_user(role="org_admin", tenant_id=t1.id)
    member = await _seed_user(role="member", tenant_id=t1.id)
    cross = await _seed_user(role="platform_admin", tenant_id=(await _seed_tenant()).id)

    async with async_session() as db:
        # creator + platform_admin manage everything (same tenant) -> ALL
        assert await resolve_human_viewer_access(db, creator.id, private_agent) == SCOPE_ALL
        assert await resolve_human_viewer_access(db, admin.id, private_agent) == SCOPE_ALL
        assert await resolve_human_viewer_access(db, identity_admin.id, private_agent) == SCOPE_ALL
        # org_admin governs every standard Agent in the tenant.
        assert await resolve_human_viewer_access(db, org_admin.id, company_agent) == SCOPE_ALL
        assert await resolve_human_viewer_access(db, org_admin.id, private_agent) == SCOPE_ALL
        # plain member on a company agent -> 'use' -> OWN (only own sessions)
        assert await resolve_human_viewer_access(db, member.id, company_agent) == SCOPE_OWN
        # member on others' private agent: no access -> DENY
        assert await resolve_human_viewer_access(db, member.id, private_agent) == SCOPE_DENY
        # cross-tenant even platform_admin -> DENY (tenant check first) — HIGH-2
        assert await resolve_human_viewer_access(db, cross.id, private_agent) == SCOPE_DENY


# ── Task 2: scope resolution + per-scope predicates + message window ────────


async def test_resolve_scope_follows_execution_user_not_channel():
    t = await _seed_tenant()
    admin = await _seed_user(role="platform_admin", tenant_id=t.id)
    agent = await _seed_agent(admin.id, tenant_id=t.id, access_mode="company")
    peer = await _seed_agent(admin.id, tenant_id=t.id, name="Peer")
    web = await _seed_session(agent.id, admin.id, channel="web")
    a2a = await _seed_session(agent.id, admin.id, channel="agent", peer=peer.id)
    trig = await _seed_session(agent.id, admin.id, channel="trigger")

    async with async_session() as db:
        assert (await resolve_scope(db, agent, str(web.id), admin.id))[0] == SCOPE_ALL
        # Background/A2A turns use the same permissions as their resolved user.
        assert (await resolve_scope(db, agent, str(a2a.id), admin.id))[0] == SCOPE_ALL
        assert (await resolve_scope(db, agent, str(trig.id), admin.id))[0] == SCOPE_ALL
        # Session routing metadata cannot narrow a valid execution identity.
        assert (await resolve_scope(db, agent, str(uuid.uuid4()), admin.id))[0] == SCOPE_ALL
        # Only a genuinely absent execution user falls back to autonomous scope.
        assert (await resolve_scope(db, agent, str(a2a.id), None))[0] == SCOPE_AUTONOMOUS


async def test_trigger_execution_user_with_agent_manage_access_sees_all_groups():
    """Regression: a member who manages the Agent keeps full scope in a trigger turn."""
    t = await _seed_tenant()
    creator = await _seed_user(tenant_id=t.id, name="Creator")
    manager = await _seed_user(tenant_id=t.id, name="宋柯")
    agent = await _seed_agent(
        creator.id,
        tenant_id=t.id,
        access_mode="custom",
        name="123",
    )
    async with async_session() as db:
        db.add(
            AgentPermission(
                agent_id=agent.id,
                scope_type="user",
                scope_id=manager.id,
                access_level="manage",
            )
        )
        await db.commit()

    shandong = await _seed_session(
        agent.id,
        None,
        channel="dingtalk",
        group=True,
        title="山东群",
        group_name="山东群",
    )
    northeast = await _seed_session(
        agent.id,
        None,
        channel="dingtalk",
        group=True,
        title="东北群",
        group_name="东北群",
    )
    trigger = await _seed_session(agent.id, None, channel="trigger")

    out = await handle_list_sessions(
        agent.id,
        manager.id,
        str(trigger.id),
        {"is_group": True, "limit": 50},
    )
    assert str(shandong.id) in out
    assert str(northeast.id) in out


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


# ── Task 3: formatting (pure, no DB) ───────────────────────────────────────


def test_render_messages_truncates_and_caps():
    from app.services.tools.session_introspection.formatting import (
        TOTAL_CHARS,
        render_messages,
    )

    class _M:
        def __init__(self, content, ca, mid):
            self.role = "user"
            self.content = content
            self.created_at = ca
            self.id = mid
            self.user_id = None
            self.agent_id = None
            self.participant_id = None

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    msgs = [_M("x" * 5000, base + timedelta(seconds=i), uuid.uuid4()) for i in range(50)]
    out = render_messages(msgs, {"users": {}, "agents": {}, "participants": {}}, more_available=True)

    assert len(out) <= TOTAL_CHARS + 3000  # total cap honored (with format slack)
    assert "truncated" in out               # per-message truncation marker
    assert "before=" in out                 # older-page cursor hint


def test_message_cursor_round_trip_is_unchanged():
    message = type(
        "Message",
        (),
        {
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "id": uuid.uuid4(),
        },
    )()
    assert sq.decode_cursor(sq.encode_cursor(message)) == (
        message.created_at,
        message.id,
    )


# ── Task 4 + 7: handlers + permission matrix (security regressions) ─────────

from app.services.session_query import DENIAL_MSG  # noqa: E402
from app.services.tools.session_introspection import (  # noqa: E402
    handle_list_sessions,
    handle_read_session_messages,
    handle_search_sessions,
)


async def test_member_list_sees_only_own_human_sessions():
    t = await _seed_tenant()
    owner = await _seed_user(role="member", tenant_id=t.id)  # agent creator (manage)
    member = await _seed_user(role="member", tenant_id=t.id)  # plain user (use -> OWN)
    other = await _seed_user(role="member", tenant_id=t.id)
    agent = await _seed_agent(owner.id, tenant_id=t.id, access_mode="company")
    peer = await _seed_agent(owner.id, tenant_id=t.id, name="Peer")
    mine = await _seed_session(agent.id, member.id, channel="web")
    theirs = await _seed_session(agent.id, other.id, channel="web")
    a2a = await _seed_session(agent.id, member.id, channel="agent", peer=peer.id)
    trig = await _seed_session(agent.id, member.id, channel="trigger")

    out = await handle_list_sessions(agent.id, member.id, str(mine.id), {})
    assert str(mine.id) in out
    assert str(theirs.id) not in out
    assert str(a2a.id) not in out and str(trig.id) not in out


async def test_admin_list_sees_all_including_a2a_and_trigger():
    t = await _seed_tenant()
    admin = await _seed_user(role="platform_admin", tenant_id=t.id)
    member = await _seed_user(role="member", tenant_id=t.id)
    agent = await _seed_agent(admin.id, tenant_id=t.id, access_mode="company")
    peer = await _seed_agent(admin.id, tenant_id=t.id, name="Peer")
    admin_web = await _seed_session(agent.id, admin.id, channel="web")
    member_web = await _seed_session(agent.id, member.id, channel="web")
    a2a = await _seed_session(agent.id, admin.id, channel="agent", peer=peer.id)
    trig = await _seed_session(agent.id, admin.id, channel="trigger")

    out = await handle_list_sessions(agent.id, admin.id, str(admin_web.id), {"limit": 50})
    for s in (admin_web, member_web, a2a, trig):
        assert str(s.id) in out


async def test_list_sessions_scene_matches_active_and_historical_snapshots():
    t = await _seed_tenant()
    admin = await _seed_user(role="platform_admin", tenant_id=t.id)
    agent = await _seed_agent(admin.id, tenant_id=t.id, access_mode="company")
    active = await _seed_session(
        agent.id,
        admin.id,
        title="Active scene",
        im_config={"scene_key": "warranty"},
    )
    historical = await _seed_session(agent.id, admin.id, title="Historical scene")
    unrelated = await _seed_session(agent.id, admin.id, title="Other scene")
    await _seed_message(
        agent.id,
        admin.id,
        historical.id,
        "user",
        "scene turn",
        message_meta={"scene_key": "warranty", "scene_revision": 1},
    )

    out = await handle_list_sessions(
        agent.id,
        admin.id,
        str(active.id),
        {"scene": "warranty", "limit": 50},
    )

    assert str(active.id) in out
    assert str(historical.id) in out
    assert str(unrelated.id) not in out


async def test_list_sessions_filters_counterpart_exact_and_fuzzy_for_people_and_a2a():
    t = await _seed_tenant()
    admin = await _seed_user(role="platform_admin", tenant_id=t.id, name="Viewer")
    alice = await _seed_user(tenant_id=t.id, name="Alice Zhang")
    bob = await _seed_user(tenant_id=t.id, name="Bob")
    agent = await _seed_agent(admin.id, tenant_id=t.id, access_mode="company")
    peer = await _seed_agent(admin.id, tenant_id=t.id, name="Finance Copilot")
    ctx = await _seed_session(agent.id, admin.id, title="ctx")
    alice_chat = await _seed_session(agent.id, alice.id, title="alice")
    bob_chat = await _seed_session(agent.id, bob.id, title="bob")
    peer_chat = await _seed_session(agent.id, None, channel="agent", peer=peer.id, title="peer")

    fuzzy = await handle_list_sessions(
        agent.id,
        admin.id,
        str(ctx.id),
        {"counterpart": "lice zh", "counterpart_match": "fuzzy", "limit": 50},
    )
    assert str(alice_chat.id) in fuzzy
    assert str(bob_chat.id) not in fuzzy

    exact_id = await handle_list_sessions(
        agent.id,
        admin.id,
        str(ctx.id),
        {"counterpart": str(alice.id), "counterpart_match": "exact", "limit": 50},
    )
    assert str(alice_chat.id) in exact_id
    assert str(bob_chat.id) not in exact_id

    a2a = await handle_list_sessions(
        agent.id,
        admin.id,
        str(ctx.id),
        {"counterpart": "Finance Copilot", "counterpart_match": "exact", "limit": 50},
    )
    assert str(peer_chat.id) in a2a


async def test_list_sessions_filters_group_and_group_sender_in_summary_and_raw():
    t = await _seed_tenant()
    admin = await _seed_user(role="platform_admin", tenant_id=t.id, name="Viewer")
    alice = await _seed_user(tenant_id=t.id, name="Alice Zhang")
    agent = await _seed_agent(admin.id, tenant_id=t.id, access_mode="company")
    ctx = await _seed_session(agent.id, admin.id, title="ctx")
    target = await _seed_session(
        agent.id,
        None,
        channel="feishu",
        group=True,
        title="Quarterly Planning",
        group_name="North Region Leaders",
        external_conv_id="oc_north_leaders",
    )
    unrelated = await _seed_session(
        agent.id,
        None,
        channel="feishu",
        group=True,
        title="Engineering",
        group_name="Platform Team",
        external_conv_id="oc_platform",
    )
    await _seed_message(agent.id, alice.id, target.id, "user", "hello")

    group_result = await handle_list_sessions(
        agent.id,
        admin.id,
        str(ctx.id),
        {"is_group": True, "group": "north region", "group_match": "fuzzy", "limit": 50},
    )
    assert str(target.id) in group_result
    assert str(unrelated.id) not in group_result

    sender_result = json.loads(
        await handle_list_sessions(
            agent.id,
            admin.id,
            str(ctx.id),
            {
                "raw": True,
                "counterpart": "Alice Zhang",
                "counterpart_match": "exact",
                "is_group": True,
                "limit": 50,
            },
        )
    )
    assert [item["id"] for item in sender_result["items"]] == [str(target.id)]


async def test_list_sessions_rejects_invalid_match_mode_without_querying():
    t = await _seed_tenant()
    admin = await _seed_user(role="platform_admin", tenant_id=t.id)
    agent = await _seed_agent(admin.id, tenant_id=t.id)
    ctx = await _seed_session(agent.id, admin.id)
    out = await handle_list_sessions(
        agent.id,
        admin.id,
        str(ctx.id),
        {"counterpart": "A", "counterpart_match": "regex"},
    )
    assert out == "❌ counterpart_match 仅支持 exact 或 fuzzy"


async def test_list_sessions_raw_cursor_pages_every_session_once():
    t = await _seed_tenant()
    admin = await _seed_user(role="platform_admin", tenant_id=t.id)
    agent = await _seed_agent(admin.id, tenant_id=t.id, access_mode="company")
    sessions = [
        await _seed_session(agent.id, admin.id, title=f"Raw {index}")
        for index in range(3)
    ]

    first = json.loads(
        await handle_list_sessions(
            agent.id,
            admin.id,
            str(sessions[0].id),
            {"raw": True, "limit": 2},
        )
    )
    assert first["page"]["has_more"] is True
    assert first["page"]["next_cursor"]
    assert all("im_config" in item and "external_conv_id" in item for item in first["items"])

    second = json.loads(
        await handle_list_sessions(
            agent.id,
            admin.id,
            str(sessions[0].id),
            {
                "raw": True,
                "limit": 2,
                "cursor": first["page"]["next_cursor"],
            },
        )
    )
    returned = [item["id"] for item in first["items"] + second["items"]]
    expected = {str(session.id) for session in sessions}
    assert expected.issubset(set(returned))
    assert len(returned) == len(set(returned))


async def test_list_sessions_raw_cursor_is_bound_to_filter_set():
    t = await _seed_tenant()
    admin = await _seed_user(role="platform_admin", tenant_id=t.id)
    agent = await _seed_agent(admin.id, tenant_id=t.id, access_mode="company")
    sessions = [
        await _seed_session(agent.id, admin.id, title=f"Bound {index}")
        for index in range(3)
    ]
    first = json.loads(
        await handle_list_sessions(
            agent.id,
            admin.id,
            str(sessions[0].id),
            {"raw": True, "query": "Bound", "limit": 1},
        )
    )
    reused = await handle_list_sessions(
        agent.id,
        admin.id,
        str(sessions[0].id),
        {
            "raw": True,
            "query": "Different",
            "limit": 1,
            "cursor": first["page"]["next_cursor"],
        },
    )
    assert reused == "❌ 无效 cursor"


def test_session_cursor_rejects_naive_timestamps():
    payload = {
        "snapshot_at": "2026-01-01T00:00:00",
        "created_at": "2026-01-01T00:00:00",
        "id": str(uuid.uuid4()),
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    assert sq.decode_session_cursor(raw) is None


async def test_background_context_uses_resolved_admin_execution_user():
    """A trigger using an admin execution user sees that Agent's full archive."""
    t = await _seed_tenant()
    admin = await _seed_user(role="platform_admin", tenant_id=t.id)
    enduser = await _seed_user(role="member", tenant_id=t.id)
    agent = await _seed_agent(admin.id, tenant_id=t.id, access_mode="company")
    human = await _seed_session(agent.id, enduser.id, channel="web")
    trig = await _seed_session(agent.id, admin.id, channel="trigger")

    # Trigger routing does not narrow the resolved execution user's permissions.
    out = await handle_list_sessions(agent.id, admin.id, str(trig.id), {"limit": 50})
    assert str(trig.id) in out
    assert str(human.id) in out

    # A genuinely absent execution user still receives the autonomous minimum.
    autonomous = await handle_list_sessions(agent.id, None, str(trig.id), {"limit": 50})
    assert str(trig.id) in autonomous
    assert str(human.id) not in autonomous


async def test_read_other_agents_session_denied_even_for_admin():
    """Per-agent isolation: a1 cannot read a2's session even as platform_admin."""
    t = await _seed_tenant()
    admin = await _seed_user(role="platform_admin", tenant_id=t.id)
    a1 = await _seed_agent(admin.id, tenant_id=t.id, name="A1")
    a2 = await _seed_agent(admin.id, tenant_id=t.id, name="A2")
    a1_ctx = await _seed_session(a1.id, admin.id, channel="web")
    foreign = await _seed_session(a2.id, admin.id, channel="web")
    await _seed_message(a2.id, admin.id, foreign.id, "user", "secret of a2")

    out = await handle_read_session_messages(
        a1.id, admin.id, str(a1_ctx.id), {"session_id": str(foreign.id)}
    )
    assert out == DENIAL_MSG


async def test_member_cannot_read_other_users_session():
    t = await _seed_tenant()
    owner = await _seed_user(role="member", tenant_id=t.id)
    member = await _seed_user(role="member", tenant_id=t.id)
    other = await _seed_user(role="member", tenant_id=t.id)
    agent = await _seed_agent(owner.id, tenant_id=t.id, access_mode="company")
    mine = await _seed_session(agent.id, member.id, channel="web")
    theirs = await _seed_session(agent.id, other.id, channel="web")
    await _seed_message(agent.id, other.id, theirs.id, "user", "private to other")

    out = await handle_read_session_messages(
        agent.id, member.id, str(mine.id), {"session_id": str(theirs.id)}
    )
    assert out == DENIAL_MSG


async def test_read_in_scope_returns_messages():
    t = await _seed_tenant()
    member = await _seed_user(role="member", tenant_id=t.id)
    agent = await _seed_agent(member.id, tenant_id=t.id, access_mode="company")
    mine = await _seed_session(agent.id, member.id, channel="web")
    base = datetime.now(timezone.utc)
    await _seed_message(agent.id, member.id, mine.id, "user", "hello there", created_at=base)
    await _seed_message(agent.id, member.id, mine.id, "assistant", "general kenobi", created_at=base + timedelta(seconds=1))

    out = await handle_read_session_messages(
        agent.id, member.id, str(mine.id), {"session_id": str(mine.id)}
    )
    assert "hello there" in out and "general kenobi" in out


async def test_search_scoped_to_own_sessions():
    t = await _seed_tenant()
    owner = await _seed_user(role="member", tenant_id=t.id)
    member = await _seed_user(role="member", tenant_id=t.id)
    other = await _seed_user(role="member", tenant_id=t.id)
    agent = await _seed_agent(owner.id, tenant_id=t.id, access_mode="company")
    mine = await _seed_session(agent.id, member.id, channel="web")
    theirs = await _seed_session(agent.id, other.id, channel="web")
    await _seed_message(agent.id, member.id, mine.id, "user", "find the WIDGET here")
    await _seed_message(agent.id, other.id, theirs.id, "user", "another WIDGET secret")

    out = await handle_search_sessions(agent.id, member.id, str(mine.id), {"query": "WIDGET"})
    assert str(mine.id) in out
    assert "通道=web" in out
    assert str(theirs.id) not in out


async def test_search_reports_exact_source_channel_for_each_hit():
    t = await _seed_tenant()
    admin = await _seed_user(role="platform_admin", tenant_id=t.id)
    agent = await _seed_agent(admin.id, tenant_id=t.id, access_mode="company")
    peer = await _seed_agent(admin.id, tenant_id=t.id, name="Peer")
    web = await _seed_session(agent.id, admin.id, channel="web", title="Web thread")
    a2a = await _seed_session(
        agent.id,
        admin.id,
        channel="agent",
        peer=peer.id,
        title="A2A thread",
    )
    await _seed_message(agent.id, admin.id, web.id, "user", "CHANNEL-MARKER")
    await _seed_message(agent.id, admin.id, a2a.id, "assistant", "CHANNEL-MARKER")

    out = await handle_search_sessions(
        agent.id,
        admin.id,
        str(web.id),
        {"query": "CHANNEL-MARKER"},
    )

    assert f"[session {web.id}] Web thread · 通道=web" in out
    assert f"[session {a2a.id}] A2A thread · 通道=agent" in out


# ── Task 5: seeded schema ──────────────────────────────────────────────────


def test_builtin_tools_seeded():
    from app.services.tool_seeder import BUILTIN_TOOLS

    by_name = {t["name"]: t for t in BUILTIN_TOOLS}
    for name in ("list_sessions", "read_session_messages", "search_sessions"):
        assert name in by_name, f"{name} missing from BUILTIN_TOOLS"
        t = by_name[name]
        assert t["is_default"] is True
        assert t["category"] == "discovery"
        assert t["parameters_schema"]["type"] == "object"
    assert by_name["read_session_messages"]["parameters_schema"]["required"] == ["session_id"]
    assert by_name["search_sessions"]["parameters_schema"]["required"] == ["query"]
    assert "source channel" in by_name["search_sessions"]["description"]
    list_properties = by_name["list_sessions"]["parameters_schema"]["properties"]
    assert {
        "scene",
        "raw",
        "cursor",
        "counterpart",
        "counterpart_match",
        "is_group",
        "group",
        "group_match",
    } <= set(list_properties)


# ── Task 6: dispatch routing ───────────────────────────────────────────────


async def test_execute_tool_routes_to_session_handlers(monkeypatch):
    import app.services.tools.session_introspection as pkg

    captured = {}

    async def fake(agent_id, user_id, ctx_session_id, arguments):
        captured.update(agent_id=agent_id, user_id=user_id, ctx=ctx_session_id, args=arguments)
        return "ROUTED-OK"

    monkeypatch.setattr(pkg, "handle_list_sessions", fake)

    from app.services.agent_tools import execute_tool

    t = await _seed_tenant()
    u = await _seed_user(tenant_id=t.id)
    agent = await _seed_agent(u.id, tenant_id=t.id)
    out = await execute_tool(
        "list_sessions", {"limit": 5}, agent_id=agent.id, user_id=u.id, session_id="sess-xyz"
    )
    assert out == "ROUTED-OK"
    assert captured["user_id"] == u.id
    assert captured["ctx"] == "sess-xyz"
    assert captured["args"]["limit"] == 5


async def test_execute_tool_end_to_end_real_path():
    """No monkeypatch: dispatch -> real handler -> real DB -> rendered string."""
    from app.services.agent_tools import execute_tool

    t = await _seed_tenant()
    owner = await _seed_user(role="member", tenant_id=t.id)
    member = await _seed_user(role="member", tenant_id=t.id)
    agent = await _seed_agent(owner.id, tenant_id=t.id, access_mode="company")
    mine = await _seed_session(agent.id, member.id, channel="web", title="My Chat")
    await _seed_message(agent.id, member.id, mine.id, "user", "remember the launch date")

    listed = await execute_tool(
        "list_sessions", {"limit": 50}, agent_id=agent.id, user_id=member.id, session_id=str(mine.id)
    )
    assert "My Chat" in listed and str(mine.id) in listed

    read = await execute_tool(
        "read_session_messages",
        {"session_id": str(mine.id)},
        agent_id=agent.id,
        user_id=member.id,
        session_id=str(mine.id),
    )
    assert "remember the launch date" in read


async def test_execute_tool_distinguishes_legacy_sentinel_from_anonymous():
    """Agent-UUID sentinel uses creator fallback; genuine None stays autonomous."""
    from app.services.agent_tools import execute_tool

    t = await _seed_tenant()
    creator = await _seed_user(role="member", tenant_id=t.id)
    agent = await _seed_agent(creator.id, tenant_id=t.id, access_mode="company")
    group = await _seed_session(
        agent.id,
        None,
        channel="dingtalk",
        group=True,
        title="Creator-visible group",
        group_name="Creator-visible group",
    )
    trigger = await _seed_session(agent.id, None, channel="trigger")

    creator_fallback = await execute_tool(
        "list_sessions",
        {"is_group": True, "limit": 50},
        agent_id=agent.id,
        user_id=agent.id,
        session_id=str(trigger.id),
    )
    assert str(group.id) in creator_fallback

    anonymous = await execute_tool(
        "list_sessions",
        {"is_group": True, "limit": 50},
        agent_id=agent.id,
        user_id=None,
        session_id=str(trigger.id),
    )
    assert str(group.id) not in anonymous
