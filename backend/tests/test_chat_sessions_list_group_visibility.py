"""Integration tests for chat_sessions.list_sessions group visibility under scope=mine.

A group chat session must surface in a user's own session list when the user has
spoken in it (matches mainstream messenger UX). Before this fix, the scope=mine
query hard-filtered ``is_group == False`` and groups were only reachable via
admin scope=all.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage  # noqa: F401
from app.models.chat_session import ChatSession
from app.models.identity import IdentityProvider, SSOScanSession  # noqa: F401
from app.models.participant import Participant  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.models.user import Identity, User  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_user(suffix: str, display_name: str, role: str = "member") -> User:
    async with async_session() as db:
        tenant_slug = f"chat-list-{suffix[-8:]}"
        tenant = (
            await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
        ).scalar_one_or_none()
        if not tenant:
            tenant = Tenant(name=f"Chat list {suffix[-8:]}", slug=tenant_slug)
            db.add(tenant)
            await db.flush()
        ident = Identity(
            username=f"u_{suffix}",
            email=f"u_{suffix}@test.local",
            password_hash="x",
        )
        db.add(ident)
        await db.flush()
        user = User(
            identity_id=ident.id,
            tenant_id=tenant.id,
            display_name=display_name,
            role=role,
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def _seed_agent(
    creator_user_id,
    *,
    company_access_level: str = "use",
) -> uuid.UUID:
    async with async_session() as db:
        tenant_id = await db.scalar(select(User.tenant_id).where(User.id == creator_user_id))
        agent = Agent(
            name="ListTestAgent",
            creator_id=creator_user_id,
            tenant_id=tenant_id,
            company_access_level=company_access_level,
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id


async def _seed_group_session(
    agent_id,
    owner_user_id,
    conv_id_str: str,
    title: str = "Group",
    group_name: str = "Group",
) -> uuid.UUID:
    sess_id = uuid.uuid4()
    async with async_session() as db:
        session = ChatSession(
            id=sess_id,
            agent_id=agent_id,
            user_id=owner_user_id,
            title=title,
            source_channel="dingtalk",
            external_conv_id=conv_id_str,
            is_group=True,
            group_name=group_name,
        )
        db.add(session)
        await db.commit()
    return sess_id


async def _seed_p2p_session(agent_id, owner_user_id, conv_id_str: str) -> uuid.UUID:
    sess_id = uuid.uuid4()
    async with async_session() as db:
        session = ChatSession(
            id=sess_id,
            agent_id=agent_id,
            user_id=owner_user_id,
            title="P2P",
            source_channel="web",
            external_conv_id=conv_id_str,
            is_group=False,
        )
        db.add(session)
        await db.commit()
    return sess_id


async def _insert_messages_bypass_fk(rows: list[dict]) -> None:
    """Insert chat_messages with FK checks disabled (postgres-only).

    Same pattern as test_chat_sessions_messages_group.py — keeps the test
    self-contained without seeding a full Agent + Tenant + Participant chain
    on every chat_messages row.
    """
    from sqlalchemy import text

    async with async_session() as db:
        await db.execute(text("SET session_replication_role = replica"))
        for row in rows:
            await db.execute(
                text(
                    "INSERT INTO chat_messages"
                    " (id, agent_id, user_id, role, content, conversation_id, created_at)"
                    " VALUES (:id, :agent_id, :user_id, :role, :content, :conv_id, :created_at)"
                ),
                {
                    "id": str(row["id"]),
                    "agent_id": str(row["agent_id"]),
                    "user_id": str(row["user_id"]),
                    "role": row["role"],
                    "content": row["content"],
                    "conv_id": row["conv_id"],
                    "created_at": row.get("created_at") or datetime.now(timezone.utc),
                },
            )
        await db.commit()
        await db.execute(text("SET session_replication_role = DEFAULT"))
        await db.commit()


async def test_group_session_visible_in_scope_mine_when_user_has_user_message():
    """When Alice has a user-role message in a group session, list_sessions(scope=mine)
    MUST return that group with is_group=True / participant_type=group / unread=0."""
    from app.api.chat_sessions import list_sessions

    run = uuid.uuid4().hex[:8]
    owner = await _seed_user(f"own_{run}", "Owner")  # agent creator + session anchor
    alice = await _seed_user(f"alice_{run}", "Alice", role="org_admin")  # so check_agent_access passes

    agent_id = await _seed_agent(owner.id)
    sess_id = await _seed_group_session(
        agent_id,
        owner.id,
        f"dingtalk_group_{run}",
        title="Owner,Alice",
        group_name="Owner,Alice",
    )

    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk([
        {
            "id": uuid.uuid4(), "agent_id": agent_id, "user_id": alice.id,
            "role": "user", "content": "hi", "conv_id": str(sess_id),
            "created_at": now - timedelta(seconds=200),
        },
        {
            "id": uuid.uuid4(), "agent_id": agent_id, "user_id": alice.id,
            "role": "assistant", "content": "hey alice", "conv_id": str(sess_id),
            "created_at": now - timedelta(seconds=100),
        },
    ])

    async with async_session() as db:
        out = await list_sessions(
            agent_id=agent_id, scope="mine", current_user=alice, db=db,
        )

    matches = [s for s in out if s.id == str(sess_id)]
    assert len(matches) == 1, f"alice should see the group session in scope=mine; got: {[s.id for s in out]}"
    s = matches[0]
    assert s.is_group is True
    assert s.participant_type == "group"
    assert s.group_name == "Owner,Alice"
    assert s.username == "Owner,Alice"
    # Group last_read_at_by_user is shared, so per-user unread is undefined and forced to 0.
    assert s.unread_count == 0
    # Message count includes both user + assistant rows.
    assert s.message_count == 2


async def test_group_session_hidden_in_scope_mine_when_user_has_no_messages():
    """Bob never posted in the group → list_sessions(scope=mine) MUST NOT return it,
    even though Bob can access the agent (org_admin)."""
    from app.api.chat_sessions import list_sessions

    run = uuid.uuid4().hex[:8]
    owner = await _seed_user(f"own_{run}", "Owner")
    alice = await _seed_user(f"alice_{run}", "Alice")
    bob = await _seed_user(f"bob_{run}", "Bob", role="org_admin")

    agent_id = await _seed_agent(owner.id)
    sess_id = await _seed_group_session(
        agent_id,
        owner.id,
        f"dingtalk_group_{run}",
        title="Owner,Alice",
        group_name="Owner,Alice",
    )

    now = datetime.now(timezone.utc)
    # Only Alice posts. Bob has no rows in this conversation.
    await _insert_messages_bypass_fk([
        {
            "id": uuid.uuid4(), "agent_id": agent_id, "user_id": alice.id,
            "role": "user", "content": "alice msg", "conv_id": str(sess_id),
            "created_at": now,
        },
    ])

    async with async_session() as db:
        out = await list_sessions(
            agent_id=agent_id, scope="mine", current_user=bob, db=db,
        )

    assert all(s.id != str(sess_id) for s in out), \
        "bob is not a member; he must not see the group in scope=mine"


@pytest.mark.parametrize("role", ["platform_admin", "org_admin", "agent_admin"])
async def test_governing_admin_can_list_and_read_group_without_membership(role: str):
    """Governance access is Agent-scoped, not conditional on IM membership."""
    from app.api.chat_sessions import get_session_messages, list_sessions

    run = uuid.uuid4().hex[:8]
    owner = await _seed_user(f"owner_{run}", "Owner")
    speaker = await _seed_user(f"speaker_{run}", "Speaker")
    admin = await _seed_user(f"admin_{run}", "Admin", role=role)
    agent_id = await _seed_agent(owner.id, company_access_level="manage")
    session_id = await _seed_group_session(
        agent_id,
        owner.id,
        f"dingtalk_group_{run}",
        group_name="Governed group",
    )
    await _insert_messages_bypass_fk([{
        "id": uuid.uuid4(),
        "agent_id": agent_id,
        "user_id": speaker.id,
        "role": "user",
        "content": "governance audit message",
        "conv_id": str(session_id),
    }])

    async with async_session() as db:
        page = await list_sessions(
            agent_id=agent_id,
            scope="all",
            exclude_mine=True,
            current_user=admin,
            db=db,
        )
        messages = await get_session_messages(
            agent_id=agent_id,
            session_id=session_id,
            limit=20,
            before=None,
            current_user=admin,
            db=db,
        )

    assert [item.id for item in page] == [str(session_id)]
    assert [message["content"] for message in messages] == [
        "governance audit message"
    ]


async def test_agent_admin_without_manage_access_cannot_view_all_sessions():
    from fastapi import HTTPException
    from app.api.chat_sessions import list_sessions

    run = uuid.uuid4().hex[:8]
    owner = await _seed_user(f"owner_{run}", "Owner")
    admin = await _seed_user(f"admin_{run}", "Agent Admin", role="agent_admin")
    agent_id = await _seed_agent(owner.id, company_access_level="use")

    with pytest.raises(HTTPException) as exc:
        async with async_session() as db:
            await list_sessions(
                agent_id=agent_id,
                scope="all",
                current_user=admin,
                db=db,
            )

    assert exc.value.status_code == 403


async def test_p2p_session_still_visible_in_scope_mine_for_owner():
    """Regression baseline: a P2P session owned by the current user remains visible
    in scope=mine after the group-membership rewrite of the WHERE clause."""
    from app.api.chat_sessions import list_sessions

    run = uuid.uuid4().hex[:8]
    owner = await _seed_user(f"own_{run}", "Owner")
    agent_id = await _seed_agent(owner.id)
    p2p_id = await _seed_p2p_session(agent_id, owner.id, f"web_{run}")

    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk([
        {
            "id": uuid.uuid4(), "agent_id": agent_id, "user_id": owner.id,
            "role": "user", "content": "p2p hi", "conv_id": str(p2p_id),
            "created_at": now,
        },
    ])

    async with async_session() as db:
        out = await list_sessions(
            agent_id=agent_id, scope="mine", current_user=owner, db=db,
        )

    matches = [s for s in out if s.id == str(p2p_id)]
    assert len(matches) == 1, f"owner should still see their P2P session; got: {[s.id for s in out]}"
    s = matches[0]
    assert s.is_group is False
    assert s.participant_type == "user"


async def test_other_session_pages_filter_viewer_before_offset_and_load_without_gaps():
    from app.api.chat_sessions import list_sessions

    run = uuid.uuid4().hex[:8]
    admin = await _seed_user(f"admin_{run}", "Admin", role="org_admin")
    other_user = await _seed_user(f"other_{run}", "Other")
    agent_id = await _seed_agent(admin.id)

    own_session_ids = [
        await _seed_p2p_session(agent_id, admin.id, f"own_{run}_{index}")
        for index in range(5)
    ]
    other_session_ids = [
        await _seed_p2p_session(agent_id, other_user.id, f"other_{run}_{index}")
        for index in range(5)
    ]
    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk([
        {
            "id": uuid.uuid4(),
            "agent_id": agent_id,
            "user_id": admin.id if session_id in own_session_ids else other_user.id,
            "role": "user",
            "content": "page me",
            "conv_id": str(session_id),
            "created_at": now + timedelta(seconds=index),
        }
        for index, session_id in enumerate([*own_session_ids, *other_session_ids])
    ])

    async with async_session() as db:
        loaded_mine_ids: list[str] = []
        mine_cursor = None
        for expected_has_more in (True, True, False):
            page = await list_sessions(
                agent_id=agent_id,
                scope="mine",
                limit=2,
                cursor=mine_cursor,
                paginated=True,
                current_user=admin,
                db=db,
            )
            assert page.has_more is expected_has_more
            assert page.next_offset is None
            loaded_mine_ids.extend(item.id for item in page.items)
            mine_cursor = page.next_cursor

        loaded_ids: list[str] = []
        cursor = None
        for expected_has_more in (True, True, False):
            page = await list_sessions(
                agent_id=agent_id,
                scope="all",
                limit=2,
                cursor=cursor,
                paginated=True,
                exclude_mine=True,
                current_user=admin,
                db=db,
            )
            assert page.has_more is expected_has_more
            assert page.next_offset is None
            loaded_ids.extend(item.id for item in page.items)
            cursor = page.next_cursor

    assert len(loaded_mine_ids) == 5
    assert len(set(loaded_mine_ids)) == 5
    assert set(loaded_mine_ids) == {str(session_id) for session_id in own_session_ids}
    assert len(loaded_ids) == 5
    assert len(set(loaded_ids)) == 5
    assert set(loaded_ids) == {str(session_id) for session_id in other_session_ids}


async def test_session_cursor_snapshot_is_stable_when_activity_changes_between_pages():
    from app.api.chat_sessions import list_sessions

    run = uuid.uuid4().hex[:8]
    owner = await _seed_user(f"cursor_{run}", "Cursor Owner")
    agent_id = await _seed_agent(owner.id)
    initial_ids = [
        await _seed_p2p_session(agent_id, owner.id, f"cursor_{run}_{index}")
        for index in range(6)
    ]
    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk([
        {
            "id": uuid.uuid4(), "agent_id": agent_id, "user_id": owner.id,
            "role": "user", "content": "cursor page", "conv_id": str(session_id),
            "created_at": now + timedelta(seconds=index),
        }
        for index, session_id in enumerate(initial_ids)
    ])

    async with async_session() as db:
        first = await list_sessions(
            agent_id=agent_id, scope="mine", limit=2, paginated=True,
            current_user=owner, db=db,
        )
        assert first.has_more is True
        assert first.next_cursor
        assert first.next_offset is None
        loaded_ids = [item.id for item in first.items]
        unseen_id = next(session_id for session_id in initial_ids if str(session_id) not in loaded_ids)
        await db.execute(update(ChatSession).where(ChatSession.id == unseen_id).values(
            last_message_at=now + timedelta(days=1),
        ))
        await db.commit()

        inserted_id = await _seed_p2p_session(agent_id, owner.id, f"cursor_new_{run}")
        await _insert_messages_bypass_fk([{
            "id": uuid.uuid4(), "agent_id": agent_id, "user_id": owner.id,
            "role": "user", "content": "new after snapshot", "conv_id": str(inserted_id),
            "created_at": now + timedelta(days=2),
        }])

        cursor = first.next_cursor
        while cursor:
            page = await list_sessions(
                agent_id=agent_id, scope="mine", limit=2, cursor=cursor,
                paginated=True, current_user=owner, db=db,
            )
            loaded_ids.extend(item.id for item in page.items)
            cursor = page.next_cursor

    assert len(loaded_ids) == len(set(loaded_ids)) == len(initial_ids)
    assert set(loaded_ids) == {str(session_id) for session_id in initial_ids}
    assert str(inserted_id) not in loaded_ids


async def test_other_sessions_exclude_groups_the_viewer_has_joined():
    from app.api.chat_sessions import list_sessions

    run = uuid.uuid4().hex[:8]
    admin = await _seed_user(f"group_admin_{run}", "Group Admin", role="org_admin")
    other = await _seed_user(f"group_other_{run}", "Group Other")
    agent_id = await _seed_agent(admin.id)
    own_group_id = await _seed_group_session(agent_id, admin.id, f"own_group_{run}")
    other_group_id = await _seed_group_session(agent_id, other.id, f"other_group_{run}")
    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk([
        {
            "id": uuid.uuid4(), "agent_id": agent_id, "user_id": admin.id,
            "sender_user_id": admin.id, "role": "user", "content": "my group message",
            "conv_id": str(own_group_id), "created_at": now,
        },
        {
            "id": uuid.uuid4(), "agent_id": agent_id, "user_id": other.id,
            "sender_user_id": other.id, "role": "user", "content": "other group message",
            "conv_id": str(other_group_id), "created_at": now + timedelta(seconds=1),
        },
    ])

    async with async_session() as db:
        page = await list_sessions(
            agent_id=agent_id,
            scope="all",
            limit=2,
            offset=0,
            paginated=True,
            exclude_mine=True,
            current_user=admin,
            db=db,
        )

    assert [item.id for item in page.items] == [str(other_group_id)]
