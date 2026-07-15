"""Permission edge cases for chat_sessions endpoints under the group-membership rewrite.

Phase 2 #1 + the follow-up fixes (5ed65b12, a66fd556) loosened READ access
(scope=mine list + messages) so group members see / read their own group's
data. WRITE access (rename, delete) was intentionally NOT relaxed — those
operations affect every member of the shared group. These tests pin that
asymmetry down so a future refactor can't silently grant destructive rights
to every speaker in a group.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.user import Identity, User  # noqa: F401
from app.models.agent import Agent
from app.models.tenant import Tenant  # noqa: F401
from app.models.identity import IdentityProvider, SSOScanSession  # noqa: F401
from app.models.participant import Participant  # noqa: F401
from app.models.audit import ChatMessage  # noqa: F401
from app.models.chat_session import ChatSession
from app.database import async_session, engine

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        tenant = Tenant(name="Perm Edge", slug=f"perm-edge-{uuid.uuid4().hex[:10]}")
        db.add(tenant)
        await db.commit()
        await db.refresh(tenant)
        return tenant


async def _seed_user(
    suffix: str,
    display_name: str,
    tenant_id: uuid.UUID,
    role: str = "member",
) -> User:
    async with async_session() as db:
        ident = Identity(
            username=f"u_{suffix}",
            email=f"u_{suffix}@test.local",
            password_hash="x",
        )
        db.add(ident)
        await db.flush()
        user = User(
            identity_id=ident.id,
            tenant_id=tenant_id,
            display_name=display_name,
            role=role,
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def _seed_agent(creator_user_id, tenant_id: uuid.UUID) -> uuid.UUID:
    async with async_session() as db:
        agent = Agent(
            name="PermEdgeAgent",
            creator_id=creator_user_id,
            tenant_id=tenant_id,
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id


async def _seed_group_session(agent_id, owner_user_id, conv_id_str: str) -> uuid.UUID:
    sess_id = uuid.uuid4()
    async with async_session() as db:
        session = ChatSession(
            id=sess_id,
            agent_id=agent_id,
            user_id=None,
            title="Perm Edge Group",
            source_channel="dingtalk",
            external_conv_id=conv_id_str,
            is_group=True,
            group_name="Perm Edge Group",
        )
        db.add(session)
        await db.commit()
    return sess_id


async def _insert_user_msg(agent_id, session_id, user_id) -> None:
    """Mark `user_id` as a group member by inserting a single user-role row."""
    from sqlalchemy import text

    async with async_session() as db:
        await db.execute(text("SET session_replication_role = replica"))
        await db.execute(
            text(
                "INSERT INTO chat_messages"
                " (id, agent_id, user_id, role, content, conversation_id, created_at)"
                " VALUES (:id, :agent_id, :user_id, 'user', 'hi', :conv_id, :created_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "agent_id": str(agent_id),
                "user_id": str(user_id),
                "conv_id": str(session_id),
                "created_at": datetime.now(timezone.utc),
            },
        )
        await db.commit()
        await db.execute(text("SET session_replication_role = DEFAULT"))
        await db.commit()


def _patch_check_agent_access(monkeypatch, creator_id):
    """Bypass the agent-access gate so each test can isolate the per-session check."""
    from app.api import chat_sessions as chat_sessions_mod

    async def _fake(_db, _user, agent_id):
        return SimpleNamespace(id=agent_id, creator_id=creator_id), "use"

    monkeypatch.setattr(chat_sessions_mod, "check_agent_access", _fake)


# --------------------------- READ vs WRITE asymmetry ---------------------------


async def test_group_member_cannot_rename_session(monkeypatch):
    """A non-owner / non-admin user who IS a group member can read the group, but
    must NOT be able to rename it — rename mutates state visible to every member."""
    from app.api.chat_sessions import rename_session, PatchSessionIn

    run = uuid.uuid4().hex[:8]
    tenant = await _seed_tenant()
    owner = await _seed_user(f"po_{run}", "Owner", tenant.id)
    member = await _seed_user(f"pm_{run}", "Member", tenant.id)  # role=member, not creator

    agent_id = await _seed_agent(owner.id, tenant.id)
    sess_id = await _seed_group_session(agent_id, owner.id, f"dingtalk_group_{run}")
    await _insert_user_msg(agent_id, sess_id, member.id)  # qualifies as member

    _patch_check_agent_access(monkeypatch, creator_id=owner.id)

    with pytest.raises(HTTPException) as exc:
        async with async_session() as db:
            await rename_session(
                agent_id=agent_id,
                session_id=sess_id,
                body=PatchSessionIn(title="hijacked"),
                current_user=member,
                db=db,
            )
    assert exc.value.status_code == 403


async def test_group_member_cannot_delete_session(monkeypatch):
    """Same asymmetry: a group member must NOT be able to delete the shared session."""
    from app.api.chat_sessions import delete_session

    run = uuid.uuid4().hex[:8]
    tenant = await _seed_tenant()
    owner = await _seed_user(f"do_{run}", "Owner", tenant.id)
    member = await _seed_user(f"dm_{run}", "Member", tenant.id)

    agent_id = await _seed_agent(owner.id, tenant.id)
    sess_id = await _seed_group_session(agent_id, owner.id, f"dingtalk_group_{run}")
    await _insert_user_msg(agent_id, sess_id, member.id)

    _patch_check_agent_access(monkeypatch, creator_id=owner.id)

    with pytest.raises(HTTPException) as exc:
        async with async_session() as db:
            await delete_session(
                agent_id=agent_id,
                session_id=sess_id,
                current_user=member,
                db=db,
            )
    assert exc.value.status_code == 403

    # Sanity: session still exists in DB after the rejected delete.
    async with async_session() as db:
        from sqlalchemy import select
        r = await db.execute(select(ChatSession).where(ChatSession.id == sess_id))
        assert r.scalar_one_or_none() is not None


async def test_admin_can_rename_group_session(monkeypatch):
    """Sanity that the admin / agent-creator path still works — _can_view_all_agent_chat_sessions
    short-circuits the strict owner check, so admins keep their override on group sessions."""
    from app.api.chat_sessions import rename_session, PatchSessionIn

    run = uuid.uuid4().hex[:8]
    tenant = await _seed_tenant()
    owner = await _seed_user(f"ao_{run}", "Owner", tenant.id)
    admin = await _seed_user(f"aa_{run}", "Admin", tenant.id, role="org_admin")

    agent_id = await _seed_agent(owner.id, tenant.id)
    sess_id = await _seed_group_session(agent_id, owner.id, f"dingtalk_group_{run}")

    _patch_check_agent_access(monkeypatch, creator_id=owner.id)

    async with async_session() as db:
        out = await rename_session(
            agent_id=agent_id,
            session_id=sess_id,
            body=PatchSessionIn(title="renamed by admin"),
            current_user=admin,
            db=db,
        )
    assert out["title"] == "renamed by admin"


# --------------------------- scope=all access gating ---------------------------


async def test_scope_all_rejected_for_non_admin_group_member(monkeypatch):
    """Group membership grants READ access to one's own groups via scope=mine,
    but it does NOT promote a regular member into the admin scope=all view.
    A non-admin requesting scope=all must still get 403 even if they have spoken
    in a group attached to that agent."""
    from app.api.chat_sessions import list_sessions

    run = uuid.uuid4().hex[:8]
    tenant = await _seed_tenant()
    owner = await _seed_user(f"so_{run}", "Owner", tenant.id)
    member = await _seed_user(f"sm_{run}", "Member", tenant.id)  # role=member, not creator, not admin

    agent_id = await _seed_agent(owner.id, tenant.id)
    sess_id = await _seed_group_session(agent_id, owner.id, f"dingtalk_group_{run}")
    await _insert_user_msg(agent_id, sess_id, member.id)  # establishes membership

    _patch_check_agent_access(monkeypatch, creator_id=owner.id)

    with pytest.raises(HTTPException) as exc:
        async with async_session() as db:
            await list_sessions(
                agent_id=agent_id, scope="all",
                current_user=member, db=db,
            )
    assert exc.value.status_code == 403
