"""PostgreSQL behavior tests for IM scene activation and turn snapshots."""

import uuid

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.mcp_server import MCPServer as _MCPServer  # register Tool's FK target
from app.models.participant import Participant as _Participant  # register session FK target
from app.models.scene import AgentScene, AgentSceneRevision
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.services.channel_commands import handle_channel_command
from app.services.channel_session import find_or_create_channel_session
from app.services.chat_history import ingest_incoming_chat_message, persist_assistant_reply_row
from app.services.scene_service import load_turn_scene_context

pytestmark = pytest.mark.asyncio
_REGISTERED_FK_TARGETS = (_MCPServer, _Participant)


@pytest.fixture(autouse=True)
async def _isolate_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_scene_runtime() -> tuple[uuid.UUID, uuid.UUID]:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(
            id=uuid.uuid4(),
            name=f"Scene Tenant {suffix}",
            slug=f"scene-{suffix}",
            im_provider="web_only",
        )
        identity = Identity(
            id=uuid.uuid4(),
            email=f"scene-{suffix}@example.com",
            username=f"scene-{suffix}",
        )
        user = User(
            id=uuid.uuid4(),
            identity=identity,
            tenant_id=tenant.id,
            display_name="Scene User",
            is_active=True,
        )
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Scene Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add_all([tenant, identity, user, agent])
        await db.flush()

        tool = (await db.execute(select(Tool).where(Tool.name == "manage_scene"))).scalar_one_or_none()
        if tool is None:
            tool = Tool(
                name="manage_scene",
                display_name="场景配置",
                description="Manage scenes",
                type="builtin",
                enabled=True,
            )
            db.add(tool)
            await db.flush()
        else:
            tool.enabled = True
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))

        scene = AgentScene(
            tenant_id=tenant.id,
            agent_id=agent.id,
            scene_key="warranty",
            name="售后咨询",
            enabled=True,
            current_revision=1,
        )
        db.add(scene)
        await db.flush()
        db.add(
            AgentSceneRevision(
                scene_id=scene.id,
                revision=1,
                config={
                    "name": "售后咨询",
                    "enabled": True,
                    "welcome_message": "",
                    "system_prompts": [
                        {
                            "id": "policy",
                            "name": "售后规则",
                            "content": "优先核对保修政策。",
                            "enabled": True,
                        }
                    ],
                    "quick_actions": [],
                },
            )
        )
        await db.commit()
        return agent.id, user.id


async def test_scene_command_persists_session_and_snapshots_exact_turn_revision():
    agent_id, user_id = await _seed_scene_runtime()
    external_conv_id = f"dingtalk_p2p_{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        result = await handle_channel_command(
            db=db,
            command="/scene warranty",
            agent_id=agent_id,
            user_id=user_id,
            external_conv_id=external_conv_id,
            source_channel="dingtalk",
        )
        await db.commit()

    assert result["action"] == "scene_activated"
    assert "售后咨询" in result["message"]

    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.source_channel == "dingtalk",
                    ChatSession.external_conv_id == external_conv_id,
                )
            )
        ).scalar_one()
        assert session.im_config == {"scene_key": "warranty"}

        same_session = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            user_id=user_id,
            external_conv_id=external_conv_id,
            source_channel="dingtalk",
            first_message_title="处理售后",
        )
        ingested = await ingest_incoming_chat_message(
            db,
            session=same_session,
            agent_id=agent_id,
            user_id=user_id,
            content="处理售后",
            source_channel="dingtalk",
            provider_event_id=f"event-{uuid.uuid4()}",
            actor_ref="staff-1",
        )
        await db.commit()
        anchor_id = ingested.message.id
        session_id = str(same_session.id)

    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
        assert anchor.message_meta["scene_key"] == "warranty"
        assert anchor.message_meta["scene_revision"] == 1

        context = await load_turn_scene_context(
            db,
            agent_id=agent_id,
            session_id=session_id,
            turn_anchor_id=anchor_id,
        )
        assert context["source_channel"] == "dingtalk"
        assert context["scene_key"] == "warranty"
        assert context["scene_revision"] == 1
        assert context["scene_system_prompts"][0]["content"] == "优先核对保修政策。"

        await persist_assistant_reply_row(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=session_id,
            content="已按售后场景处理。",
            turn_anchor_id=anchor_id,
        )
        await db.commit()

    async with async_session() as db:
        assistant = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == session_id,
                    ChatMessage.role == "assistant",
                )
            )
        ).scalar_one()
        assert assistant.message_meta["scene_key"] == "warranty"
        assert assistant.message_meta["scene_revision"] == 1
