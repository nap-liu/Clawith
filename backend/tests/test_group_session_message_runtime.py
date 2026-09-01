"""Mechanical continuation of exact-Session delivery contract tests."""

import pytest

from tests.test_group_session_message import (
    Agent,
    AgentRelationship,
    AgentTool,
    ChannelConfig,
    ChatMessage,
    ChatSession,
    DeliveryReceiptPersistenceError,
    IMDeliveryPart,
    IMDeliveryResult,
    Identity,
    MentionIntent,
    SimpleNamespace,
    Tenant,
    Tool,
    TurnRuntime,
    User,
    _isolate_messages_and_engine,
    _seed_agents,
    _seed_related_user,
    _seed_session,
    agent_tools,
    asyncio,
    async_session,
    create_async_engine,
    datetime,
    delete,
    engine,
    httpx,
    json,
    seed_builtin_tools,
    select,
    text,
    timedelta,
    timezone,
    turn_runtime,
    uuid,
)

pytestmark = pytest.mark.asyncio

async def test_dingtalk_runtime_uses_interactive_card_for_native_mentions_without_webhook(
    monkeypatch,
):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        external_conv_id="dingtalk_group_open-conversation-exact",
    )
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=f"ding-app-{uuid.uuid4().hex}",
                app_secret="ding-secret",
                is_configured=True,
            )
        )
        await db.commit()
    captured = {}

    async def fake_card(**kwargs):
        captured.update(kwargs)
        return kwargs["out_track_id"]

    async def fail_proactive(**_kwargs):
        raise AssertionError("native mention must use the interactive card transport")

    async def fake_tool_config(*_args, **_kwargs):
        return {"card_template_id": "message-template"}

    recorded_parts = []

    async def record_part(part):
        recorded_parts.append(part)

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_tool_config)
    monkeypatch.setattr("app.services.dingtalk_card.send_message_card", fake_card)
    monkeypatch.setattr(turn_runtime, "_send_dingtalk_group_markdown", fail_proactive)
    result = await turn_runtime.deliver_message_with_receipt(
        agent_id=owner.id,
        runtime=TurnRuntime(
            session_found=True,
            source_channel="dingtalk",
            conversation_id=str(target.id),
            external_conv_id=target.external_conv_id,
            is_group=True,
        ),
        message="请确认",
        mention=MentionIntent(
            scope="users",
            target_ids=("staff-zhangsan",),
            target_names=("张三",),
        ),
        on_part=record_part,
    )

    assert result.ok is True
    assert result.parts[0].transport == "dingtalk_interactive_card"
    assert result.parts[0].recallable is False
    assert result.parts[0].provider_message_id == captured["out_track_id"]
    assert recorded_parts == [result.parts[0]]
    assert captured["card_template_id"] == "message-template"
    assert captured["content"] == "请确认"
    assert captured["external_conv_id"] == target.external_conv_id
    assert captured["at_user_ids"] == {"staff-zhangsan": "张三"}


async def test_dingtalk_card_provider_success_does_not_retry_when_part_persistence_fails(
    monkeypatch,
):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=f"ding-app-{uuid.uuid4().hex}",
                app_secret="ding-secret",
                is_configured=True,
            )
        )
        await db.commit()

    provider_calls = []

    async def fake_tool_config(*_args, **_kwargs):
        return {"card_template_id": "message-template"}

    async def fake_card(**kwargs):
        provider_calls.append(kwargs)
        return kwargs["out_track_id"]

    async def fail_part(_part):
        raise DeliveryReceiptPersistenceError("database unavailable")

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_tool_config)
    monkeypatch.setattr("app.services.dingtalk_card.send_message_card", fake_card)

    with pytest.raises(DeliveryReceiptPersistenceError):
        await turn_runtime.deliver_message_with_receipt(
            agent_id=owner.id,
            runtime=TurnRuntime(
                session_found=True,
                source_channel="dingtalk",
                conversation_id=str(target.id),
                external_conv_id=target.external_conv_id,
                is_group=True,
            ),
            message="请确认",
            mention=MentionIntent(scope="all"),
            on_part=fail_part,
        )

    assert len(provider_calls) == 1


async def test_dingtalk_proactive_text_reports_provider_part_immediately(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=f"ding-app-{uuid.uuid4().hex}",
                app_secret="ding-secret",
                is_configured=True,
            )
        )
        await db.commit()

    provider_calls = []
    recorded_parts = []

    async def fake_send(**kwargs):
        provider_calls.append(kwargs)
        return {"errcode": 0, "processQueryKey": "text-process-key"}

    async def record_part(part):
        recorded_parts.append(part)

    monkeypatch.setattr(turn_runtime, "send_dingtalk_proactive_markdown", fake_send)
    result = await turn_runtime.deliver_message_with_receipt(
        agent_id=owner.id,
        runtime=TurnRuntime(
            session_found=True,
            source_channel="dingtalk",
            conversation_id=str(target.id),
            external_conv_id=target.external_conv_id,
            is_group=True,
        ),
        message="普通消息",
        on_part=record_part,
    )

    assert len(provider_calls) == 1
    assert recorded_parts == [result.parts[0]]
    assert result.parts[0].provider_message_id == "text-process-key"


async def test_dingtalk_runtime_mention_all_uses_card_without_webhook(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=f"ding-app-{uuid.uuid4().hex}",
                app_secret="ding-secret",
                is_configured=True,
            )
        )
        await db.commit()

    captured = {}

    async def fake_tool_config(*_args, **_kwargs):
        return {"card_template_id": "message-template"}

    async def fake_card(**kwargs):
        captured.update(kwargs)
        return kwargs["out_track_id"]

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_tool_config)
    monkeypatch.setattr("app.services.dingtalk_card.send_message_card", fake_card)
    result = await turn_runtime.deliver_message_with_receipt(
        agent_id=owner.id,
        runtime=TurnRuntime(
            session_found=True,
            source_channel="dingtalk",
            conversation_id=str(target.id),
            external_conv_id=target.external_conv_id,
            is_group=True,
        ),
        message="今晚十点发布",
        mention=MentionIntent(scope="all"),
    )

    assert result.ok is True
    assert captured["at_user_ids"] == {"@ALL": "@ALL"}


async def test_dingtalk_runtime_rejects_native_mention_without_card_template(
    monkeypatch,
):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=f"ding-app-{uuid.uuid4().hex}",
                app_secret="ding-secret",
                is_configured=True,
            )
        )
        await db.commit()

    async def missing_tool_config(*_args, **_kwargs):
        return {}

    async def fail_card(**_kwargs):
        raise AssertionError("card provider must not be called without a template")

    monkeypatch.setattr(agent_tools, "_get_tool_config", missing_tool_config)
    monkeypatch.setattr("app.services.dingtalk_card.send_message_card", fail_card)
    result = await turn_runtime.deliver_message_with_receipt(
        agent_id=owner.id,
        runtime=TurnRuntime(
            session_found=True,
            source_channel="dingtalk",
            conversation_id=str(target.id),
            external_conv_id=target.external_conv_id,
            is_group=True,
        ),
        message="不应发送",
        mention=MentionIntent(scope="all"),
    )

    assert result.ok is False
    assert result.error == "dingtalk_message_card_template_unavailable"


async def test_dingtalk_card_delivery_failure_finishes_receipt_failed_without_webhook(
    monkeypatch,
):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=f"ding-app-{uuid.uuid4().hex}",
                app_secret="ding-secret",
                is_configured=True,
            )
        )
        await db.commit()

    async def fake_tool_config(*_args, **_kwargs):
        return {"card_template_id": "message-template"}

    async def fail_card(**_kwargs):
        return None

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_tool_config)
    monkeypatch.setattr("app.services.dingtalk_card.send_message_card", fail_card)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)
    result = await agent_tools._send_group_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "不应发送",
            "mention_all": True,
        },
        origin_session_id=str(uuid.uuid4()),
        tool_call_id="mention-card-provider-failure",
        origin_turn_anchor_id=uuid.uuid4(),
    )

    assert result.startswith("❌ Group message delivery failed via dingtalk")
    async with async_session() as db:
        receipts = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.conversation_id == str(target.id))
            )
        ).scalars().all()
    assert len(receipts) == 1
    assert receipts[0].message_meta["mentions"] == {"scope": "all"}
    assert receipts[0].message_meta["delivery"]["status"] == "failed"
    assert (
        receipts[0].message_meta["delivery"]["error"]
        == "dingtalk_message_card_delivery_failed"
    )


@pytest.mark.parametrize(
    "runtime",
    [
        TurnRuntime(
            session_found=True,
            source_channel="dingtalk",
            conversation_id="p2p",
            external_conv_id="dingtalk_p2p_user-1",
            is_group=False,
        ),
        TurnRuntime(
            session_found=True,
            source_channel="feishu",
            conversation_id="group",
            external_conv_id="feishu_group_chat-1",
            is_group=True,
        ),
    ],
)
async def test_runtime_rejects_unsupported_native_mentions(runtime):
    result = await turn_runtime.deliver_message_with_receipt(
        agent_id=uuid.uuid4(),
        runtime=runtime,
        message="不应静默发送",
        mention=MentionIntent(scope="all"),
    )

    assert result.ok is False
    assert result.error in {"mention_requires_group", "native_mention_not_supported"}


async def test_feishu_runtime_delivers_to_exact_group_conversation(monkeypatch):
    owner, _ = await _seed_agents()
    app_id = f"feishu-app-{uuid.uuid4().hex}"
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="feishu",
                app_id=app_id,
                app_secret="feishu-secret",
                is_configured=True,
            )
        )
        await db.commit()
    captured = {}

    async def fake_send_message(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"code": 0}

    monkeypatch.setattr("app.services.feishu_service.feishu_service.send_message", fake_send_message)
    runtime = TurnRuntime(
        session_found=True,
        source_channel="feishu",
        conversation_id=str(uuid.uuid4()),
        external_conv_id="feishu_group_exact-chat-id",
        is_group=True,
    )

    sent = await turn_runtime.deliver_message_to_runtime(
        agent_id=owner.id,
        runtime=runtime,
        message="exact feishu target",
    )

    assert sent is True
    assert captured["args"] == (
        app_id,
        "feishu-secret",
        "exact-chat-id",
        "text",
        json.dumps({"text": "exact feishu target"}, ensure_ascii=False),
    )
    assert captured["kwargs"] == {"receive_id_type": "chat_id"}


async def test_unconfigured_channel_is_not_used(monkeypatch):
    owner, _ = await _seed_agents()
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=f"ding-app-{uuid.uuid4().hex}",
                app_secret="ding-secret",
                is_configured=False,
            )
        )
        await db.commit()

    async def fail_if_called(**_kwargs):
        raise AssertionError("unconfigured transport must not be called")

    monkeypatch.setattr(turn_runtime, "_send_dingtalk_group_markdown", fail_if_called)
    sent = await turn_runtime.deliver_message_to_runtime(
        agent_id=owner.id,
        runtime=TurnRuntime(
            session_found=True,
            source_channel="dingtalk",
            conversation_id=str(uuid.uuid4()),
            external_conv_id="dingtalk_group_not-configured",
            is_group=True,
        ),
        message="do not send",
    )
    assert sent is False


async def test_wecom_group_failure_never_falls_back_to_personal_message(monkeypatch):
    owner, _ = await _seed_agents()
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="wecom",
                app_id="corp-id",
                app_secret="corp-secret",
                is_configured=True,
                extra_config={"wecom_agent_id": "10001"},
            )
        )
        await db.commit()

    async def fake_token(*_args, **_kwargs):
        return {}

    async def fail_personal_send(*_args, **_kwargs):
        raise AssertionError("group delivery must not fall back to a personal recipient")

    monkeypatch.setattr("app.services.wecom_service.get_wecom_access_token", fake_token)
    monkeypatch.setattr("app.services.wecom_service.send_wecom_message", fail_personal_send)
    sent = await turn_runtime.deliver_message_to_runtime(
        agent_id=owner.id,
        runtime=TurnRuntime(
            session_found=True,
            source_channel="wecom",
            conversation_id=str(uuid.uuid4()),
            external_conv_id="wecom_group_exact-chat-id",
            is_group=True,
        ),
        message="group only",
        origin_actor_ref="personal-user-id",
        allow_wecom_group_actor_fallback=False,
    )
    assert sent is False


async def test_seeded_tool_is_visible_with_the_exact_runtime_schema():
    owner, _ = await _seed_agents()
    async with async_session() as db:
        existing_tool_ids = (
            await db.execute(
                select(Tool.id).where(
                    Tool.name.in_(
                        {
                            "send_channel_file",
                            "send_group_session_message",
                            "recall_message",
                        }
                    )
                )
            )
        ).scalars().all()
        if existing_tool_ids:
            await db.execute(delete(AgentTool).where(AgentTool.tool_id.in_(existing_tool_ids)))
            await db.execute(delete(Tool).where(Tool.id.in_(existing_tool_ids)))
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=f"ding-app-{uuid.uuid4().hex}",
                app_secret="ding-secret",
                is_configured=True,
            )
        )
        await db.commit()

    await seed_builtin_tools()
    async with async_session() as db:
        session_message_config_schema = (
            await db.execute(
                select(Tool.config_schema).where(Tool.name == "send_session_message")
            )
        ).scalar_one()
        seeded_file_schema = (
            await db.execute(
                select(Tool.parameters_schema).where(Tool.name == "send_channel_file")
            )
        ).scalar_one()
        assigned_names = set(
            (
                await db.execute(
                    select(Tool.name)
                    .select_from(AgentTool)
                    .join(Tool, Tool.id == AgentTool.tool_id)
                    .where(
                        AgentTool.agent_id == owner.id,
                        AgentTool.enabled.is_(True),
                        Tool.name.in_({"send_group_session_message", "recall_message"}),
                    )
                )
            ).scalars().all()
        )
    assert session_message_config_schema["fields"][0]["key"] == "card_template_id"
    assert session_message_config_schema["fields"][0].get("agent_only") is not True
    assert assigned_names == {"send_group_session_message", "recall_message"}

    tools = await agent_tools.get_agent_tools_for_llm(owner.id)
    file_tool = next(tool for tool in tools if tool["function"]["name"] == "send_channel_file")
    assert file_tool["function"]["parameters"] == seeded_file_schema
    assert seeded_file_schema["properties"]["session_id"]["type"] == "string"
    runtime_tool = next(tool for tool in tools if tool["function"]["name"] == "send_group_session_message")
    assert runtime_tool["function"]["parameters"] == {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Exact group ChatSession UUID returned by list_sessions/search_sessions.",
            },
            "message": {
                "type": "string",
                "description": (
                    "Business text to send. When a native mention option is present, do not prefix "
                    "@names, @everyone, or external IDs; the transport renders the mention exactly once."
                ),
            },
            "mention_user_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
                "description": (
                    "Optional canonical platform user_ids to mention natively in the target group. "
                    "Each person must be a member of the target group, and the bound channel must "
                    "support native group mentions."
                ),
            },
            "mention_all": {
                "type": "boolean",
                "description": (
                    "Optionally mention all members of the target group natively. "
                    "Cannot be combined with mention_user_ids."
                ),
            },
        },
        "required": ["session_id", "message"],
        "additionalProperties": False,
    }
    session_tool = next(tool for tool in tools if tool["function"]["name"] == "send_session_message")
    assert session_tool["function"]["parameters"] == {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Exact human ChatSession UUID returned by list_sessions/search_sessions.",
            },
            "message": {
                "type": "string",
                "description": (
                    "Business text to send. When a native mention option is present, do not prefix "
                    "@names, @everyone, or external IDs; the transport renders the mention exactly once."
                ),
            },
            "mention_user_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
                "description": (
                    "Optional canonical platform user_ids to mention natively in the target group. "
                    "Each person must be a member of the target group, and the bound channel must "
                    "support native group mentions."
                ),
            },
            "mention_all": {
                "type": "boolean",
                "description": (
                    "Optionally mention all members of the target group natively. "
                    "Cannot be combined with mention_user_ids."
                ),
            },
        },
        "required": ["session_id", "message"],
        "additionalProperties": False,
    }
    recall_tool = next(tool for tool in tools if tool["function"]["name"] == "recall_message")
    assert recall_tool["function"]["parameters"] == {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "Exact local ChatMessage UUID of the outbound assistant message.",
            },
        },
        "required": ["message_id"],
        "additionalProperties": False,
    }
