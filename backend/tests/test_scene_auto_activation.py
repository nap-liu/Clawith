"""Automatic scenes through PostgreSQL, ingress, and WebSocket presentation."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.websocket import WebSocketChatHandler
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.user import User
from app.models.project import Project, ProjectMemberSnapshot
from app.schemas.scene import ScenePublishRequest, SceneSaveRequest
from app.services.channel_commands import handle_channel_command
from app.services.chat_history import ingest_incoming_chat_message
from app.services.scene_activation import resolve_session_scene, snapshot_project_scene
from app.services.scene_service import (
    delete_scene, get_scene, publish_scene,
    rollback_scene, save_scene, serialize_scene_manifest,
)
from app.services.scene_targets import conversation_options, session_target_identity, encode_target
from test_im_scene_command_integration import _seed_scene_runtime

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def dispose_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def setup_target(db, user_id, agent_id, *, group=False, channel="web"):
    agent = await db.get(Agent, agent_id)
    if channel != "web":
        db.add(ChannelConfig(agent_id=agent_id, channel_type=channel, is_configured=True))
    session = ChatSession(
        agent_id=agent_id, user_id=None if group else user_id,
        source_channel=channel, is_group=group, group_name="Support" if group else None,
        external_conv_id="support-room" if channel != "web" else None,
    )
    db.add(session)
    await db.flush()
    identity = await session_target_identity(db, agent, session)
    target = {"target_ref": encode_target(identity), "label": "Support"}
    return agent, session, target


async def save_auto(db, agent, user_id, target, *, key="warranty", revision=1, enabled=True):
    return await save_scene(db, agent_id=agent.id, tenant_id=agent.tenant_id, scene_key=key,
        created_by_user_id=user_id, data=SceneSaveRequest(
            name="Support scene", expected_revision=revision, enabled=enabled,
            welcome_message="Must never be sent automatically",
            system_prompts=[{"id": "policy", "name": "Policy", "content": "Check warranty first."}],
            auto_activation={"enabled": True, "targets": [target]},
        ))


async def publish(db, agent, user_id, *, key="warranty", revision=1):
    return await publish_scene(db, agent_id=agent.id, scene_key=key,
        data=ScenePublishRequest(expected_revision=revision), created_by_user_id=user_id)


@pytest.mark.parametrize("group,channel", [(False, "web"), (False, "feishu"), (True, "feishu")])
async def test_latest_session_and_silent_published_activation(group, channel):
    agent_id, user_id = await _seed_scene_runtime()
    async with async_session() as db:
        agent, session, target = await setup_target(db, user_id, agent_id, group=group, channel=channel)
        await save_auto(db, agent, user_id, target)
        assert await resolve_session_scene(db, agent_id, session) is None
        await publish(db, agent, user_id)
        manifest = await resolve_session_scene(db, agent_id, session)
        assert manifest["scene_key"] == "warranty"
        assert manifest["activation_source"] == "automatic"
        assert manifest["welcome_message"] == ""
        user = await db.get(User, user_id)
        options = await conversation_options(db, agent, user)
        assert len(options["items"]) == 1
        original_ref = options["items"][0]["target_ref"]
        if session.external_conv_id:
            session.external_conv_id += "__archived_20260908"
            await db.flush()
        newer = ChatSession(agent_id=agent_id, user_id=session.user_id, source_channel=channel,
            is_group=group, external_conv_id="support-room" if channel != "web" else None,
            created_at=datetime.now(UTC) + timedelta(seconds=1))
        db.add(newer)
        await db.flush()
        assert await resolve_session_scene(db, agent_id, session) is None
        assert (await resolve_session_scene(db, agent_id, newer))["scene_key"] == "warranty"
        options = await conversation_options(db, agent, user)
        assert len(options["items"]) == 1
        assert options["items"][0]["target_ref"] == original_ref
        assert options["items"][0]["current_session_id"] == str(newer.id)
        assert await db.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.agent_id == agent_id)) == 0


async def test_conflicts_rollback_disabled_and_deletion():
    agent_id, user_id = await _seed_scene_runtime()
    async with async_session() as db:
        agent, session, target = await setup_target(db, user_id, agent_id)
        await save_auto(db, agent, user_id, target)
        await publish(db, agent, user_id)
        await save_auto(db, agent, user_id, target, key="other", revision=0)
        await db.commit()
        with pytest.raises(HTTPException) as conflict:
            await publish(db, agent, user_id, key="other", revision=0)
        assert conflict.value.status_code == 409
        await db.rollback()
        agent = await db.get(Agent, agent_id)
        await save_auto(db, agent, user_id, target, revision=2, enabled=False)
        await publish(db, agent, user_id, revision=2)
        await publish(db, agent, user_id, key="other", revision=0)
        with pytest.raises(HTTPException) as conflict:
            await rollback_scene(db, agent_id=agent_id, scene_key="warranty", target_revision=2,
                expected_revision=3, created_by_user_id=user_id)
        assert conflict.value.status_code == 409
        await db.rollback()


async def test_websocket_auto_welcome_and_ingress_snapshot():
    agent_id, user_id = await _seed_scene_runtime()
    async with async_session() as db:
        agent, session, target = await setup_target(db, user_id, agent_id)
        await save_auto(db, agent, user_id, target)
        await publish(db, agent, user_id)
        handler = WebSocketChatHandler(AsyncMock(), agent_id, "unused")
        handler.conv_id = str(session.id)
        handler.history_messages = []
        await handler._load_scene_manifest(db)
        assert handler.scene_manifest["activation_source"] == "automatic"
        assert handler._resolve_onboarding_required(True) is False
        await handler._prepare_initial_greeting(db, user_id)
        assert handler.pending_initial_assistant is None
        assert handler._channel_context()["scene_system_prompts"][0]["content"] == "Check warranty first."
        result = await ingest_incoming_chat_message(db, session=session, agent_id=agent_id,
            user_id=user_id, source_channel="web", content="Question",
            message_meta=handler._scene_message_meta())
        assert result.message.message_meta["scene_revision"] == 2
        found = await get_scene(db, agent_id, "warranty")
        assert "auto_activation" not in serialize_scene_manifest(*found)
        await delete_scene(db, agent_id, "warranty")
        assert await resolve_session_scene(db, agent_id, session) is None


async def test_im_off_overrides_auto_without_welcome():
    agent_id, user_id = await _seed_scene_runtime()
    async with async_session() as db:
        agent, session, target = await setup_target(db, user_id, agent_id, channel="feishu", group=True)
        await save_auto(db, agent, user_id, target)
        await publish(db, agent, user_id)
        result = await handle_channel_command(db=db, agent_id=agent_id, user_id=user_id,
            external_conv_id="support-room", source_channel="feishu", command="/scene off", is_group=True)
        assert result["action"] == "scene_off"
        assert await resolve_session_scene(db, agent_id, session) is None
        assert session.im_config["scene_disabled"] is True


async def test_reject_foreign_targets_and_preserve_omitted_configuration():
    agent_id, user_id = await _seed_scene_runtime()
    foreign_agent, foreign_user = await _seed_scene_runtime()
    async with async_session() as db:
        agent, session, target = await setup_target(db, user_id, agent_id)
        other, _, other_target = await setup_target(db, foreign_user, foreign_agent)
        with pytest.raises(HTTPException):
            await save_auto(db, agent, user_id, other_target)
        await save_auto(db, agent, user_id, target)
        await publish(db, agent, user_id)
        saved = await save_scene(db, agent_id=agent_id, tenant_id=agent.tenant_id, scene_key="warranty",
            created_by_user_id=user_id, data=SceneSaveRequest(name="Renamed", expected_revision=2))
        assert saved["auto_activation"]["enabled"]
        assert len(saved["auto_activation"]["targets"]) == 1


async def test_project_group_uses_member_scene_and_enforces_project_access():
    agent_id, user_id = await _seed_scene_runtime()
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        leader = Agent(name="Leader", tenant_id=agent.tenant_id, creator_id=user_id)
        project = Project(name="Support project", tenant_id=agent.tenant_id, owner_user_id=user_id)
        db.add_all([leader, project])
        await db.flush()
        member = ProjectMemberSnapshot(tenant_id=agent.tenant_id, project_id=project.id,
            agent_id=agent_id, name_snapshot=agent.name, is_enabled=True)
        session = ChatSession(project_id=project.id, agent_id=leader.id, source_channel="project",
            is_group=True, group_name="Support project")
        db.add_all([member, session])
        await db.flush()
        user = await db.get(User, user_id)
        options = await conversation_options(db, agent, user)
        assert len(options["items"]) == 1
        target = {key: value for key, value in options["items"][0].items()
                  if key in {"target_ref", "label", "source_channel", "is_group"}}
        await save_auto(db, agent, user_id, target)
        await publish(db, agent, user_id)
        metadata = {}
        await snapshot_project_scene(db, agent_id, session, metadata)
        assert metadata["scene_key"] == "warranty"
        assert metadata["scene_revision"] == 2
        assert session.im_config == {}
        private = ChatSession(project_id=project.id, agent_id=agent_id, user_id=user_id,
            source_channel="web", is_group=False)
        db.add(private)
        await db.flush()
        options = await conversation_options(db, agent, user)
        assert len(options["items"]) == 2
        private_identity = await session_target_identity(db, agent, private)
        assert private_identity["project_id"] == str(project.id)
        assert private_identity["user_id"] == str(user_id)
        member.is_enabled = False
        await db.flush()
        assert await resolve_session_scene(db, agent_id, session) is None
        assert not (await conversation_options(db, agent, user))["items"]
