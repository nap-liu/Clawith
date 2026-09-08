"""Ordinary Web history must not discover project-scoped conversations."""

import uuid

import app.models.registry  # noqa: F401
from app.api.activity import list_conversations
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.project import Project
from app.models.tenant import Tenant
from app.models.user import User


async def test_ordinary_activity_history_excludes_project_sessions():
    await engine.dispose()
    try:
        async with async_session() as db:
            tenant = Tenant(name="History test", slug=f"history-{uuid.uuid4().hex}")
            db.add(tenant)
            await db.flush()
            user = User(tenant_id=tenant.id, display_name="Owner", role="org_admin")
            db.add(user)
            await db.flush()
            agent = Agent(tenant_id=tenant.id, creator_id=user.id, name="History agent")
            project = Project(tenant_id=tenant.id, owner_user_id=user.id, name="Project")
            db.add_all([agent, project])
            await db.flush()
            ordinary = ChatSession(agent_id=agent.id, user_id=user.id, source_channel="web")
            scoped = ChatSession(agent_id=agent.id, user_id=user.id,
                                 source_channel="web", project_id=project.id)
            db.add_all([ordinary, scoped])
            await db.flush()
            db.add_all([ChatMessage(
                agent_id=agent.id, user_id=user.id, conversation_id=str(session.id),
                role="user", content=content,
            ) for session, content in ((ordinary, "Ordinary message"), (scoped, "Project message"))])
            await db.flush()

            conversations = await list_conversations(agent_id=agent.id, current_user=user, db=db)

            assert [item["conv_id"] for item in conversations] == [str(ordinary.id)]
            assert conversations[0]["last_message"] == "Ordinary message"
            assert conversations[0]["message_count"] == 1
            await db.rollback()
    finally:
        await engine.dispose()
