"""PostgreSQL seed helpers for session introspection tests."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.database import async_session, engine
from app.models.agent import Agent, AgentPermission
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def _seed_user(
    role: str = "member",
    tenant_id=None,
    name: str = "U",
    *,
    is_platform_admin: bool = False,
) -> User:
    async with async_session() as db:
        ident = Identity(
            username=f"u_{uuid.uuid4().hex[:12]}",
            email=f"{uuid.uuid4().hex[:12]}@t.local",
            password_hash="x",
            is_platform_admin=is_platform_admin,
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
        await db.flush()
        if access_mode == "company":
            db.add(AgentPermission(agent_id=a.id, scope_type="company", access_level="use"))
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
    im_config: dict | None = None,
    group_name: str | None = None,
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
            group_name=group_name,
            external_conv_id=external_conv_id,
            im_config=im_config or {},
            last_message_at=datetime.now(timezone.utc),
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
        return s


async def _seed_legacy_malformed_session(
    agent_id, user_id, *, channel: str, peer
) -> ChatSession:
    """Insert a pre-normalization row so read predicates can prove fail-closed behavior."""
    async with async_session() as db:
        await db.execute(text("SET session_replication_role = replica"))
        s = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            source_channel=channel,
            peer_agent_id=peer,
            is_group=False,
            title="legacy malformed",
            last_message_at=datetime.now(timezone.utc),
        )
        db.add(s)
        await db.commit()
        await db.execute(text("SET session_replication_role = DEFAULT"))
        await db.commit()
        await db.refresh(s)
        return s


async def _seed_message(
    agent_id,
    user_id,
    conv_id,
    role,
    content,
    *,
    created_at=None,
    message_meta: dict | None = None,
) -> uuid.UUID:
    async with async_session() as db:
        m = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            role=role,
            content=content,
            conversation_id=str(conv_id),
            created_at=created_at or datetime.now(timezone.utc),
            message_meta=message_meta or {},
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
