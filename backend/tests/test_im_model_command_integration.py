"""PostgreSQL behavior tests for shared Web/IM temporary model selection."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.mcp_server import MCPServer as _MCPServer  # register Tool FK target
from app.models.participant import Participant as _Participant  # register chat FK target
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services import channel_llm
from app.services.channel_commands import handle_channel_command
from app.services.channel_session import find_or_create_channel_session
from app.services.chat_history import ingest_incoming_chat_message, persist_assistant_reply_row

pytestmark = pytest.mark.asyncio
_REGISTERED_FK_TARGETS = (_MCPServer, _Participant)


@pytest.fixture(autouse=True)
async def _isolate_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_model_runtime() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(
            id=uuid.uuid4(),
            name=f"Model Tenant {suffix}",
            slug=f"model-{suffix}",
            im_provider="web_only",
        )
        identity = Identity(
            id=uuid.uuid4(),
            email=f"model-{suffix}@example.com",
            username=f"model-{suffix}",
        )
        user = User(
            id=uuid.uuid4(),
            identity=identity,
            tenant_id=tenant.id,
            display_name="Model User",
            is_active=True,
        )
        default_model = LLMModel(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            provider="openai",
            model="default-internal-model",
            api_key_encrypted="encrypted-test-key",
            label="默认生产模型",
            enabled=True,
        )
        selected_model = LLMModel(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            provider="openai",
            model="selected-internal-model",
            api_key_encrypted="encrypted-test-key",
            label="企业 GPT 旗舰版",
            enabled=True,
        )
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Model Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
            primary_model_id=default_model.id,
        )
        db.add_all([tenant, identity])
        await db.flush()
        db.add_all([user, default_model, selected_model])
        await db.flush()
        db.add(agent)
        await db.commit()
        return agent.id, user.id, default_model.id, selected_model.id


async def test_model_command_snapshots_selected_saved_model_for_exact_im_turn(monkeypatch):
    agent_id, user_id, default_model_id, selected_model_id = await _seed_model_runtime()
    external_conv_id = f"dingtalk_p2p_{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        result = await handle_channel_command(
            db=db,
            command="/model 企业 GPT 旗舰版",
            agent_id=agent_id,
            user_id=user_id,
            external_conv_id=external_conv_id,
            source_channel="dingtalk",
        )
        await db.commit()

    assert result["action"] == "model_switched"
    assert "企业 GPT 旗舰版" in result["message"]
    assert str(selected_model_id) not in result["message"]

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
        assert session.im_config["model_id"] == str(selected_model_id)

        same_session = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            user_id=user_id,
            external_conv_id=external_conv_id,
            source_channel="dingtalk",
            first_message_title="执行模型测试",
        )
        ingested = await ingest_incoming_chat_message(
            db,
            session=same_session,
            agent_id=agent_id,
            user_id=user_id,
            content="执行模型测试",
            source_channel="dingtalk",
            provider_event_id=f"event-{uuid.uuid4()}",
            actor_ref="staff-1",
        )
        await db.commit()
        anchor_id = ingested.message.id
        session_id = str(same_session.id)

    captured: dict = {}

    async def fake_scene_context(*_args, **_kwargs):
        return {}

    async def fake_llm(**kwargs):
        captured.update(kwargs)
        return "已使用指定模型"

    monkeypatch.setattr(
        "app.services.scene_service.load_turn_scene_context",
        fake_scene_context,
    )
    monkeypatch.setattr("app.services.llm.call_llm_with_failover", fake_llm)
    monkeypatch.setattr(channel_llm, "is_agent_expired", lambda _agent: False)

    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
        assert anchor.message_meta["model_id"] == str(selected_model_id)

        reply = await channel_llm._call_agent_llm(
            db,
            agent_id,
            "执行模型测试",
            session_id=session_id,
            user_id=user_id,
            turn_anchor_id=anchor_id,
        )
        assert reply == "已使用指定模型"
        assert captured["primary_model"].id == selected_model_id
        assert captured["primary_model"].id != default_model_id

        await persist_assistant_reply_row(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=session_id,
            content=reply,
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
        assert assistant.message_meta["model_id"] == str(selected_model_id)
