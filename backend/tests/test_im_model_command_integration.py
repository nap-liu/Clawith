"""PostgreSQL behavior tests for shared Web/IM temporary model selection."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

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
from app.services.chat_history import ingest_incoming_chat_message

pytestmark = pytest.mark.asyncio
_REGISTERED_FK_TARGETS = (_MCPServer, _Participant)


@pytest.fixture(autouse=True)
async def _isolate_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_model_runtime() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
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
            model="qwen3-max",
            api_key_encrypted="encrypted-test-key",
            label="默认生产模型",
            enabled=True,
        )
        selected_model = LLMModel(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            provider="openai",
            model="qwen3.5-plus",
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
        return agent.id, user.id, tenant.id, default_model.id, selected_model.id


async def test_model_command_snapshots_selected_saved_model_for_exact_im_turn(monkeypatch):
    agent_id, user_id, _tenant_id, default_model_id, selected_model_id = await _seed_model_runtime()
    external_conv_id = f"dingtalk_p2p_{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        result = await handle_channel_command(
            db=db,
            command="/model qwen3.5-plus",
            agent_id=agent_id,
            user_id=user_id,
            external_conv_id=external_conv_id,
            source_channel="dingtalk",
        )
        await db.commit()

    assert result["action"] == "model_switched"
    assert "qwen3.5-plus" in result["message"]
    assert "企业 GPT 旗舰版" not in result["message"]
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


async def test_status_reads_current_session_token_and_cache_usage():
    agent_id, user_id, _tenant_id, _default_model_id, _selected_model_id = await _seed_model_runtime()
    external_conv_id = f"dingtalk_p2p_{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        assert agent is not None
        session = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            user_id=user_id,
            external_conv_id=external_conv_id,
            source_channel="dingtalk",
            first_message_title="状态测试",
        )
        ingested = await ingest_incoming_chat_message(
            db,
            session=session,
            agent_id=agent_id,
            user_id=user_id,
            content="状态测试",
            source_channel="dingtalk",
            provider_event_id=f"event-{uuid.uuid4()}",
            actor_ref="staff-1",
        )
        await db.commit()
        session_id = str(session.id)
        turn_anchor_id = ingested.message.id

    from app.services.session_token_usage import persist_turn_token_usage
    from app.services.token_tracker import TokenUsage

    await persist_turn_token_usage(
        agent_id=agent_id,
        session_id=session_id,
        turn_anchor_id=turn_anchor_id,
        usage=TokenUsage(
            total_tokens=1200,
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=700,
            cache_eligible_input_tokens=1000,
        ),
    )

    async with async_session() as db:
        result = await handle_channel_command(
            db=db,
            command="/status",
            agent_id=agent_id,
            user_id=user_id,
            external_conv_id=external_conv_id,
            source_channel="dingtalk",
        )

    assert result["action"] == "status"
    assert "运行状态：空闲" in result["message"]
    assert "模型：qwen3-max（默认模型）" in result["message"]
    assert "会话：单聊 · 1 条消息" in result["message"]
    assert "输入 1K / 输出 200 / 总计 1.2K / 缓存命中率 70.0%" in result["message"]
    assert "已记录" not in result["message"]


async def test_shared_model_resolver_and_web_path_enforce_catalog_rules():
    from app.api.websocket import WebSocketChatHandler
    from app.services.chat_model_selection import (
        MODEL_OVERRIDE_DISABLED,
        MODEL_OVERRIDE_INVALID,
        MODEL_OVERRIDE_OK,
        MODEL_OVERRIDE_UNAVAILABLE,
        MODEL_STATUS_AMBIGUOUS,
        MODEL_STATUS_DISABLED,
        list_enabled_tenant_models,
        resolve_runtime_models,
        resolve_tenant_model_by_name,
    )

    agent_id, _user_id, tenant_id, default_model_id, selected_model_id = await _seed_model_runtime()
    async with async_session() as db:
        other_tenant = Tenant(
            id=uuid.uuid4(),
            name=f"Other Model Tenant {uuid.uuid4().hex[:8]}",
            slug=f"other-model-{uuid.uuid4().hex[:10]}",
            im_provider="web_only",
        )
        db.add(other_tenant)
        await db.flush()
        disabled = LLMModel(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            provider="openai",
            model="disabled-model",
            api_key_encrypted="encrypted-test-key",
            label="停用模型",
            enabled=False,
        )
        duplicate_one = LLMModel(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            provider="openai",
            model="duplicate-model",
            api_key_encrypted="encrypted-test-key",
            label="重复 模型",
            enabled=True,
        )
        duplicate_two = LLMModel(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            provider="openai",
            model="duplicate-model",
            api_key_encrypted="encrypted-test-key",
            label="重复  模型",
            enabled=True,
        )
        cross_tenant = LLMModel(
            id=uuid.uuid4(),
            tenant_id=other_tenant.id,
            provider="openai",
            model="cross-tenant-model",
            api_key_encrypted="encrypted-test-key",
            label="跨租户模型",
            enabled=True,
        )
        db.add_all([disabled, duplicate_one, duplicate_two, cross_tenant])
        agent = await db.get(Agent, agent_id)
        assert agent is not None
        agent.primary_model_id = disabled.id
        agent.fallback_model_id = default_model_id
        await db.commit()

    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        assert agent is not None

        default_resolution = await resolve_runtime_models(db, agent=agent)
        assert default_resolution.primary_model.id == default_model_id
        assert default_resolution.fallback_model is None

        selected_resolution = await resolve_runtime_models(
            db,
            agent=agent,
            override_model_id=selected_model_id,
        )
        assert selected_resolution.override_status == MODEL_OVERRIDE_OK
        assert selected_resolution.primary_model.id == selected_model_id

        disabled_resolution = await resolve_runtime_models(
            db,
            agent=agent,
            override_model_id=disabled.id,
        )
        assert disabled_resolution.override_status == MODEL_OVERRIDE_DISABLED
        assert disabled_resolution.primary_model.id == default_model_id

        cross_resolution = await resolve_runtime_models(
            db,
            agent=agent,
            override_model_id=cross_tenant.id,
        )
        assert cross_resolution.override_status == MODEL_OVERRIDE_UNAVAILABLE
        assert cross_resolution.primary_model.id == default_model_id

        invalid_resolution = await resolve_runtime_models(
            db,
            agent=agent,
            override_model_id="not-a-uuid",
        )
        assert invalid_resolution.override_status == MODEL_OVERRIDE_INVALID

        disabled_name = await resolve_tenant_model_by_name(
            db,
            tenant_id=tenant_id,
            model_name="disabled-model",
        )
        assert disabled_name.status == MODEL_STATUS_DISABLED
        duplicate_name = await resolve_tenant_model_by_name(
            db,
            tenant_id=tenant_id,
            model_name="duplicate-model",
        )
        assert duplicate_name.status == MODEL_STATUS_AMBIGUOUS
        listed_ids = {model.id for model in await list_enabled_tenant_models(db, tenant_id)}
        assert disabled.id not in listed_ids
        assert cross_tenant.id not in listed_ids

    handler = WebSocketChatHandler(SimpleNamespace(), agent_id, "test-token")
    selected = await handler._resolve_effective_model(str(selected_model_id))
    assert selected is not None and selected.id == selected_model_id
    fallback = await handler._resolve_effective_model(str(disabled.id))
    assert fallback is not None and fallback.id == default_model_id
