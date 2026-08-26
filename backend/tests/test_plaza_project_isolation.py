"""Observable Plaza restoration and project-Agent isolation coverage."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from fastapi import BackgroundTasks
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import activity as activity_api
from app.api import notification as notification_api
from app.api import plaza as plaza_api
from app.database import Base
from app.main import app
from app.models.activity_log import AgentActivityLog
from app.models.agent import Agent
from app.models.notification import Notification
from app.models.plaza import PlazaComment, PlazaPost
from app.models.project import Project
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.services import agent_tools, heartbeat, tool_seeder
from app.services.project_member_runtime import PROJECT_AGENT_DEFAULT_TOOL_NAMES

PLAZA_TOOL_NAMES = {
    "plaza_get_new_posts",
    "plaza_create_post",
    "plaza_add_comment",
}


@pytest.fixture
async def plaza_db(monkeypatch: pytest.MonkeyPatch):
    database_url = os.environ.get("PROJECT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("PROJECT_TEST_DATABASE_URL must name an isolated PostgreSQL database")

    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tool_seeder, "async_session", factory)
    monkeypatch.setattr(agent_tools, "async_session", factory)
    monkeypatch.setattr(plaza_api, "async_session", factory)

    from app import database

    monkeypatch.setattr(database, "async_session", factory)
    try:
        yield factory
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


async def _seed_company(factory):
    async with factory() as db:
        tenant = Tenant(name="Plaza Test", slug=f"plaza-{uuid.uuid4().hex[:8]}")
        identity = Identity(username=f"owner-{uuid.uuid4().hex[:8]}")
        db.add_all([tenant, identity])
        await db.flush()
        owner = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Owner",
            role="org_admin",
            is_active=True,
        )
        db.add(owner)
        await db.flush()
        project = Project(
            tenant_id=tenant.id,
            owner_user_id=owner.id,
            name="Isolated project",
            description="",
            goal="",
            success_criteria=[],
            visibility="private",
            status="planning",
            settings={},
        )
        db.add(project)
        await db.flush()
        author = Agent(
            name="PlazaAuthor",
            creator_id=owner.id,
            tenant_id=tenant.id,
            scope="standard",
            status="idle",
            access_mode="company",
            heartbeat_enabled=True,
            heartbeat_active_hours="invalid",
        )
        colleague = Agent(
            name="PlazaColleague",
            creator_id=owner.id,
            tenant_id=tenant.id,
            scope="standard",
            status="idle",
            access_mode="company",
            heartbeat_enabled=False,
        )
        project_agent = Agent(
            name="ProjectWorker",
            creator_id=owner.id,
            tenant_id=tenant.id,
            scope="project",
            project_id=project.id,
            agent_dir=f".agents/{uuid.uuid4()}",
            status="idle",
            access_mode="private",
            heartbeat_enabled=True,
            heartbeat_active_hours="invalid",
        )
        db.add_all([author, colleague, project_agent])
        await db.commit()
        return owner, author, colleague, project_agent


@pytest.mark.asyncio
async def test_standard_plaza_api_tool_runtime_and_project_history_isolation(
    plaza_db,
):
    factory = plaza_db
    owner, author, colleague, project_agent = await _seed_company(factory)

    await tool_seeder.seed_builtin_tools()

    async with factory() as db:
        tools = (await db.execute(select(Tool).where(Tool.name.in_(PLAZA_TOOL_NAMES)))).scalars().all()
        assert {tool.name for tool in tools} == PLAZA_TOOL_NAMES
        assert all(tool.enabled and tool.is_default for tool in tools)
        assignments = (
            await db.execute(
                select(AgentTool.agent_id, Tool.name)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(Tool.name.in_(PLAZA_TOOL_NAMES), AgentTool.enabled == True)
            )
        ).all()
        assert {(agent_id, name) for agent_id, name in assignments} == {
            (agent_id, name) for agent_id in (author.id, colleague.id) for name in PLAZA_TOOL_NAMES
        }
        assert PROJECT_AGENT_DEFAULT_TOOL_NAMES.isdisjoint(PLAZA_TOOL_NAMES)

    standard_catalog = await agent_tools.get_agent_tools_for_llm(author.id)
    project_catalog = await agent_tools.get_agent_tools_for_llm(project_agent.id)
    assert PLAZA_TOOL_NAMES.issubset({item["function"]["name"] for item in standard_catalog})
    assert PLAZA_TOOL_NAMES.isdisjoint({item["function"]["name"] for item in project_catalog})

    publish_result = await agent_tools.execute_tool(
        "plaza_create_post",
        {"content": "Useful public update @PlazaColleague @ProjectWorker"},
        author.id,
        owner.id,
        skip_autonomy=True,
        tools_for_llm=standard_catalog,
    )
    assert "Post published" in publish_result

    blocked_result = await agent_tools.execute_tool(
        "plaza_create_post",
        {"content": "Project-only details"},
        project_agent.id,
        owner.id,
        skip_autonomy=True,
        tools_for_llm=project_catalog,
    )
    assert "published" not in blocked_result.lower()

    async with factory() as db:
        public_post = (await db.execute(select(PlazaPost).where(PlazaPost.author_id == author.id))).scalar_one()
        hidden_post = PlazaPost(
            author_id=project_agent.id,
            author_type="agent",
            author_name=project_agent.name,
            content="Legacy project-only post",
            tenant_id=owner.tenant_id,
        )
        hidden_comment = PlazaComment(
            post_id=public_post.id,
            author_id=project_agent.id,
            author_type="agent",
            author_name=project_agent.name,
            content="Legacy project-only comment",
        )
        public_post.comments_count = 1
        db.add_all([hidden_post, hidden_comment])
        await db.commit()

    app.dependency_overrides[plaza_api.get_current_user] = lambda: owner
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        feed_response = await client.get("/api/plaza/posts")
        detail_response = await client.get(f"/api/plaza/posts/{public_post.id}")
        stats_response = await client.get("/api/plaza/stats")
        hidden_comment_response = await client.post(
            f"/api/plaza/posts/{public_post.id}/comments",
            json={
                "content": "Project-only direct comment",
                "author_id": str(project_agent.id),
                "author_type": "agent",
                "author_name": project_agent.name,
            },
        )
        hidden_like_response = await client.post(
            f"/api/plaza/posts/{public_post.id}/like",
            params={"author_id": str(project_agent.id), "author_type": "agent"},
        )
        hidden_delete_response = await client.delete(f"/api/plaza/posts/{hidden_post.id}")
        public_comment_response = await client.post(
            f"/api/plaza/posts/{public_post.id}/comments",
            json={
                "content": "Useful follow-up",
                "author_id": str(colleague.id),
                "author_type": "agent",
                "author_name": colleague.name,
            },
        )
        public_like_response = await client.post(
            f"/api/plaza/posts/{public_post.id}/like",
            params={"author_id": str(colleague.id), "author_type": "agent"},
        )

    assert feed_response.status_code == 200
    assert [item["author_id"] for item in feed_response.json()] == [str(author.id)]
    assert feed_response.json()[0]["comments_count"] == 0
    assert detail_response.status_code == 200
    assert detail_response.json()["comments"] == []
    assert stats_response.json()["total_posts"] == 1
    assert stats_response.json()["total_comments"] == 0
    assert hidden_comment_response.status_code == 403
    assert hidden_like_response.status_code == 403
    assert hidden_delete_response.status_code == 404
    assert public_comment_response.status_code == 200
    assert public_like_response.status_code == 200
    assert public_like_response.json() == {"liked": True}

    runtime_feed = await agent_tools.execute_tool(
        "plaza_get_new_posts",
        {"limit": 10},
        author.id,
        owner.id,
        skip_autonomy=True,
        tools_for_llm=standard_catalog,
    )
    blocked_comment = await agent_tools.execute_tool(
        "plaza_add_comment",
        {"post_id": str(public_post.id), "content": "Project-only runtime comment"},
        project_agent.id,
        owner.id,
        skip_autonomy=True,
        tools_for_llm=project_catalog,
    )
    assert "Useful public update" in runtime_feed
    assert "Legacy project-only post" not in runtime_feed
    assert "Legacy project-only comment" not in runtime_feed
    assert "commented" not in blocked_comment.lower()

    async with factory() as db:
        db_owner = await db.scalar(select(User).where(User.id == owner.id))
        notifications = (await db.execute(select(Notification).where(Notification.type == "mention"))).scalars().all()
        assert {item.agent_id for item in notifications} == {colleague.id}

        db.add(
            AgentActivityLog(
                agent_id=author.id,
                action_type="plaza_post",
                summary="Published a public update",
            )
        )
        db.add(
            Notification(
                user_id=owner.id,
                type="plaza_reply",
                title="A Plaza reply",
                body="Reply body",
            )
        )
        await db.commit()
        activity = await activity_api.get_agent_activity(author.id, limit=50, current_user=db_owner, db=db)
        social = await notification_api.list_notifications(
            limit=50,
            offset=0,
            unread_only=False,
            category="social",
            current_user=db_owner,
            db=db,
        )
        assert "plaza_post" in {item["action_type"] for item in activity}
        assert {item["type"] for item in social} >= {"plaza_reply"}

        broadcast = await notification_api.broadcast_notification(
            notification_api.BroadcastRequest(title="Company update", body="Public"),
            BackgroundTasks(),
            current_user=db_owner,
            db=db,
        )
        assert broadcast["agents_notified"] == 2
        broadcast_agents = set(
            (await db.execute(select(Notification.agent_id).where(Notification.type == "broadcast"))).scalars()
        )
        assert broadcast_agents == {author.id, colleague.id}


@pytest.mark.asyncio
async def test_heartbeat_drains_plaza_notifications_and_never_schedules_project_agents(
    plaza_db,
    monkeypatch: pytest.MonkeyPatch,
):
    factory = plaza_db
    _owner, author, _colleague, project_agent = await _seed_company(factory)
    async with factory() as db:
        notification = Notification(
            agent_id=author.id,
            type="plaza_reply",
            title="New Plaza reply",
            body="A colleague responded",
            sender_name="Colleague",
        )
        db.add(notification)
        await db.commit()
        prompt = await heartbeat._drain_heartbeat_notifications(db, author.id)
        await db.commit()
        await db.refresh(notification)
        assert "[plaza_reply] New Plaza reply" in prompt
        assert notification.is_read is True

    fired: list[uuid.UUID] = []

    async def fake_execute(agent_id: uuid.UUID):
        fired.append(agent_id)

    async def fake_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(heartbeat, "_execute_heartbeat", fake_execute)
    from app.services import audit_logger

    monkeypatch.setattr(audit_logger, "write_audit_log", fake_audit)
    await heartbeat._heartbeat_tick()
    await asyncio.sleep(0)

    assert fired == [author.id]
    async with factory() as db:
        standard_last = await db.scalar(select(Agent.last_heartbeat_at).where(Agent.id == author.id))
        project_last = await db.scalar(select(Agent.last_heartbeat_at).where(Agent.id == project_agent.id))
        assert standard_last is not None
        assert project_last is None
