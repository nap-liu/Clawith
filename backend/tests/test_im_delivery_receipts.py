"""Mechanical continuation of normalized IM delivery lifecycle tests."""

import pytest

from tests.test_im_delivery import (
    UTC,
    Agent,
    AgentTool,
    ChannelConfig,
    ChatCompaction,
    ChatMessage,
    ChatSession,
    IMDeliveryPart,
    IMDeliveryResult,
    Identity,
    PartRecallResult,
    SimpleNamespace,
    Tool,
    User,
    _isolate_messages_and_engine,
    _recall,
    _seed_agent,
    _seed_message,
    agent_tools,
    asyncio,
    async_session,
    build_llm_messages_from_rows,
    convert_chat_messages_to_llm_format,
    datetime,
    im_delivery,
    load_messages_for_session,
    remove_builtin_tool,
    select,
    serialize_chat_message_for_client,
    serialize_span_for_summary,
    timedelta,
    uuid,
)

pytestmark = pytest.mark.asyncio

async def test_callback_delivery_commits_sanitized_pending_before_provider_and_each_part():
    agent, user = await _seed_agent()
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="Callback outbox",
            source_channel="feishu",
            external_conv_id="feishu_p2p_callback",
        )
        db.add(session)
        await db.commit()
        session_id = str(session.id)

    forbidden = "cla" + "with"
    observed_message_id = None

    async def deliver(delivery_message, on_part):
        nonlocal observed_message_id
        async with async_session() as db:
            row = (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == session_id,
                        ChatMessage.message_meta["artifact_role"].as_string()
                        == "streaming_card",
                    )
                )
            ).scalar_one()
            observed_message_id = row.id
            assert row.message_meta["delivery"]["status"] == "pending"
            assert forbidden.lower() not in row.content.lower()
            assert forbidden.lower() not in delivery_message.lower()

        first = IMDeliveryPart(
            transport="feishu_message",
            provider_message_id="om_first",
            conversation_ref="ou_target",
            artifact_role="card",
        )
        await on_part(first)
        async with async_session() as db:
            stored = await db.get(ChatMessage, observed_message_id)
            assert [
                part["provider_message_id"]
                for part in stored.message_meta["delivery"]["parts"]
            ] == ["om_first"]

        second = IMDeliveryPart(
            transport="feishu_message",
            provider_message_id="om_second",
            conversation_ref="ou_target",
            artifact_role="fallback",
        )
        await on_part(second)
        return IMDeliveryResult.sent("feishu", first, second)

    message_id, result = await im_delivery.persist_and_deliver_message(
        agent_id=agent.id,
        user_id=user.id,
        conversation_id=session_id,
        channel="feishu",
        message=f"before {forbidden} after",
        artifact_role="streaming_card",
        deliver=deliver,
    )

    assert message_id == observed_message_id
    assert result.status == "sent"
    async with async_session() as db:
        stored = await db.get(ChatMessage, message_id)
    assert stored.message_meta["delivery"]["status"] == "sent"
    assert [part["provider_message_id"] for part in stored.message_meta["delivery"]["parts"]] == [
        "om_first",
        "om_second",
    ]


async def test_callback_delivery_cancellation_marks_unknown():
    agent, user = await _seed_agent()
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="Cancelled callback outbox",
            source_channel="wecom",
            external_conv_id="wecom_p2p_cancelled",
        )
        db.add(session)
        await db.commit()
        session_id = str(session.id)

    message_id = await im_delivery.persist_delivery_anchor(
        agent_id=agent.id,
        user_id=user.id,
        conversation_id=session_id,
        channel="wecom",
        message="processing",
        artifact_role="thinking",
    )

    async def cancelled(_message, _on_part):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await im_delivery.deliver_persisted_with_callback(
            message_id=message_id,
            channel="wecom",
            message="processing",
            deliver=cancelled,
        )

    async with async_session() as db:
        stored = await db.get(ChatMessage, message_id)
    assert stored.message_meta["delivery"]["status"] == "unknown"


async def test_tool_round_progress_reuses_tool_row_without_creating_assistant_history(monkeypatch):
    from app.services import turn_runtime
    from app.services.turn_runtime import TurnRuntime

    agent, user = await _seed_agent()
    conversation_id = str(uuid.uuid4())
    tool_content = (
        '{"name":"read_file","call_id":"call-progress","args":{},'
        '"status":"running","assistant_content":"I will inspect the file."}'
    )
    async with async_session() as db:
        row = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="tool_call",
            content=tool_content,
            conversation_id=conversation_id,
        )
        db.add(row)
        await db.commit()
        row_id = row.id
    assert await im_delivery.register_delivery(
        row_id,
        IMDeliveryResult.pending("slack"),
    )

    async def deliver_message_with_receipt(*, on_part, **_kwargs):
        part = IMDeliveryPart(
            transport="slack",
            provider_message_id="progress-ts",
            conversation_ref="C123",
            artifact_role="chunk",
            recallable=True,
        )
        await on_part(part)
        return IMDeliveryResult.sent("slack", part)

    monkeypatch.setattr(
        turn_runtime,
        "deliver_message_with_receipt",
        deliver_message_with_receipt,
    )
    await im_delivery.deliver_persisted_message(
        message_id=row_id,
        agent_id=agent.id,
        runtime=TurnRuntime(
            session_found=True,
            source_channel="slack",
            conversation_id=conversation_id,
            external_conv_id="slack_C123",
            is_group=False,
        ),
        message="I will inspect the file.",
        receipt_recallable=False,
    )

    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == conversation_id
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1
    assert rows[0].role == "tool_call"
    assert rows[0].content == tool_content
    assert rows[0].message_meta["delivery"]["parts"][0]["recall_status"] == "unsupported"


async def test_durable_thinking_progress_does_not_complete_interrupted_turn():
    from app.services.chat_history import load_recoverable_messages_for_turn
    from app.services.turn_recovery import _latest_row_needs_recovery

    agent, user = await _seed_agent()
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="Recover thinking progress",
            source_channel="slack",
            external_conv_id="slack_recover_thinking",
        )
        db.add(session)
        await db.flush()
        anchor = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="user",
            content="continue after restart",
            conversation_id=str(session.id),
        )
        db.add(anchor)
        await db.flush()
        progress = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="assistant",
            content="still processing",
            conversation_id=str(session.id),
            message_meta={
                "artifact_role": "thinking",
                "turn_anchor_id": str(anchor.id),
                "delivery": IMDeliveryResult.pending("slack").to_meta(),
            },
        )
        db.add(progress)
        await db.commit()

        assert await _latest_row_needs_recovery(db, progress) is True
        rows = await load_recoverable_messages_for_turn(
            db,
            agent_id=agent.id,
            conversation_id=str(session.id),
            turn_anchor_id=anchor.id,
            ctx_size=20,
        )

    assert [row.id for row in rows] == [anchor.id]


async def test_autonomy_notification_is_prepared_in_owning_transaction():
    from app.models.audit import ApprovalRequest
    from app.models.identity import IdentityProvider
    from app.models.notification import Notification
    from app.models.org import OrgMember
    from app.services.autonomy_service import AutonomyService

    agent, user = await _seed_agent()
    async with async_session() as db:
        provider = IdentityProvider(
            provider_type="feishu",
            name=f"Feishu {uuid.uuid4().hex[:8]}",
            tenant_id=user.tenant_id,
            config={},
        )
        db.add(provider)
        await db.flush()
        db.add_all(
            [
                ChannelConfig(
                    agent_id=agent.id,
                    channel_type="feishu",
                    app_id="app-test",
                    app_secret="secret-test",
                    is_configured=True,
                ),
                OrgMember(
                    provider_id=provider.id,
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    name=user.display_name,
                    external_id="creator-feishu-id",
                ),
            ]
        )
        await db.commit()

    async with async_session() as db:
        owned_agent = await db.get(Agent, agent.id)
        result = await AutonomyService().check_and_enforce(
            db,
            owned_agent,
            "dangerous_action",
            {"summary": "needs approval"},
            forced_level="L3",
            idempotency_key=f"approval:{uuid.uuid4()}",
        )
        pending = result["_pending_im_notifications"]
        assert len(pending) == 1
        row = await db.get(ChatMessage, uuid.UUID(pending[0]["message_id"]))
        assert row.message_meta["delivery"]["status"] == "pending"
        approval_id = uuid.UUID(result["approval_id"])
        assert await db.get(ApprovalRequest, approval_id) is not None
        assert (await db.execute(select(Notification))).scalars().all()
        await db.rollback()

    async with async_session() as db:
        assert await db.get(ApprovalRequest, approval_id) is None
        assert await db.get(ChatMessage, uuid.UUID(pending[0]["message_id"])) is None


async def test_builtin_tool_rollback_is_exact_and_idempotent():
    agent, _user = await _seed_agent()
    tool_name = f"test_recall_rollback_{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        tool = Tool(
            name=tool_name,
            display_name="Rollback test",
            description="",
            type="builtin",
            category="communication",
            icon="undo",
            parameters_schema={},
            config={},
            config_schema={},
            enabled=True,
            is_default=True,
            source="builtin",
        )
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        tool_id = tool.id

    assert await remove_builtin_tool(tool_name) == (1, 1)
    assert await remove_builtin_tool(tool_name) == (0, 0)
    async with async_session() as db:
        assert await db.get(Tool, tool_id) is None
        assignments = (
            await db.execute(select(AgentTool).where(AgentTool.tool_id == tool_id))
        ).scalars().all()
    assert assignments == []


async def test_unsupported_transport_recall_does_not_require_channel_config():
    agent, user = await _seed_agent()
    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.sent(
            "whatsapp",
            IMDeliveryPart(
                transport="whatsapp_cloud",
                provider_message_id="wamid-1",
                conversation_ref="15550001111",
                recallable=False,
            ),
        ),
    )

    result = await _recall(agent, user, row)

    assert result == {
        "status": "unsupported",
        "message_id": str(row.id),
        "parts": [
            {
                "part_id": "0",
                "transport": "whatsapp_cloud",
                "status": "unsupported",
            }
        ],
    }


async def test_pending_delivery_is_not_recalled_and_stale_pending_becomes_unknown():
    agent, user = await _seed_agent()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("dingtalk"))

    pending = await _recall(agent, user, row)
    assert pending == {"status": "pending", "message_id": str(row.id)}

    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
        delivery = dict(stored.message_meta["delivery"])
        delivery["updated_at"] = (
            datetime.now(UTC) - im_delivery.DELIVERY_LEASE - timedelta(seconds=1)
        ).isoformat()
        stored.message_meta = {**stored.message_meta, "delivery": delivery}
        await db.commit()

    unknown = await _recall(agent, user, row)
    assert unknown == {"status": "unknown", "message_id": str(row.id)}


async def test_successful_part_survives_later_delivery_failure():
    agent, user = await _seed_agent()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("slack"))
    part = IMDeliveryPart(
        transport="slack",
        provider_message_id="remote-first-chunk",
        conversation_ref="channel-1",
        artifact_role="chunk",
    )

    assert await im_delivery.append_delivery_part(row.id, part) is True
    assert await im_delivery.register_delivery(
        row.id,
        IMDeliveryResult.failed("slack", "second_chunk_rejected"),
    ) is True

    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
    delivery = stored.message_meta["delivery"]
    assert delivery["status"] == "partial"
    assert [item["provider_message_id"] for item in delivery["parts"]] == [
        "remote-first-chunk"
    ]
    assert delivery["recall"]["status"] == "available"


async def test_pending_part_is_updated_in_place_when_provider_send_completes():
    agent, user = await _seed_agent()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("dingtalk"))
    pending_part = IMDeliveryPart(
        transport="dingtalk_interactive_card",
        provider_message_id="card-1",
        conversation_ref="group-1",
        artifact_role="confirmation_card",
        recallable=False,
        send_status="pending",
    )
    sent_part = IMDeliveryPart(
        transport="dingtalk_interactive_card",
        provider_message_id="card-1",
        conversation_ref="group-1",
        artifact_role="confirmation_card",
        recallable=False,
    )

    assert await im_delivery.append_delivery_part(row.id, pending_part)
    assert await im_delivery.register_delivery(
        row.id,
        IMDeliveryResult.sent("dingtalk", sent_part),
    )

    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
    delivery = stored.message_meta["delivery"]
    assert delivery["status"] == "sent"
    assert len(delivery["parts"]) == 1
    assert delivery["parts"][0]["send_status"] == "sent"
    assert delivery["parts"][0]["recall_status"] == "unsupported"


async def test_transport_timeout_maps_to_unknown_but_provider_rejection_maps_to_failed():
    unknown = IMDeliveryResult.from_exception("slack", TimeoutError())
    assert unknown.status == "unknown"
    assert unknown.to_meta()["status"] == "unknown"
    assert im_delivery.attach_delivery_to_meta({}, unknown)["delivery_status"] == "unknown"
    receipt_failure = IMDeliveryResult.from_exception(
        "slack",
        im_delivery.DeliveryReceiptPersistenceError("receipt unavailable"),
    )
    assert receipt_failure.status == "unknown"
    assert IMDeliveryResult.from_exception("slack", RuntimeError("rejected")).status == "failed"


async def test_append_missing_receipt_raises_unknown_delivery_error():
    part = IMDeliveryPart(
        transport="slack",
        provider_message_id="provider-visible",
        conversation_ref="channel-1",
    )

    with pytest.raises(im_delivery.DeliveryReceiptPersistenceError):
        await im_delivery.append_delivery_part(uuid.uuid4(), part)


async def test_stale_pending_with_known_part_can_still_recall_that_part(monkeypatch):
    agent, user = await _seed_agent()
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="slack",
                app_id="app",
                app_secret="token",
                is_configured=True,
            )
        )
        await db.commit()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("slack"))
    await im_delivery.append_delivery_part(
        row.id,
        IMDeliveryPart(
            transport="test_stale_known_part",
            provider_message_id="known-provider-part",
            conversation_ref="channel-1",
        ),
    )
    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
        delivery = dict(stored.message_meta["delivery"])
        delivery["updated_at"] = (
            datetime.now(UTC) - im_delivery.DELIVERY_LEASE - timedelta(seconds=1)
        ).isoformat()
        stored.message_meta = {**stored.message_meta, "delivery": delivery}
        await db.commit()

    recalled_parts = []

    async def fake_recall(_config, parts):
        recalled_parts.extend(parts)
        return [
            PartRecallResult(part_id=str(part["part_id"]), status="recalled")
            for part in parts
        ]

    monkeypatch.setitem(
        im_delivery.IM_RECALL_ADAPTERS,
        "test_stale_known_part",
        fake_recall,
    )

    result = await _recall(agent, user, row)

    assert result["status"] == "partial"
    assert [part["provider_message_id"] for part in recalled_parts] == [
        "known-provider-part"
    ]


async def test_supported_recall_adapters_normalize_provider_success(monkeypatch):
    from app.api import teams as teams_api
    from app.services import feishu_service, wecom_service

    calls: list[tuple[str, str, dict]] = []

    class Response:
        status_code = 200

        def __init__(self, payload: dict, status_code: int = 200):
            self.payload = payload
            self.status_code = status_code

        def json(self):
            return self.payload

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            if "batchRecall" in url:
                return Response({"successResult": ["provider-1"]})
            if "qyapi.weixin.qq.com" in url:
                return Response({"errcode": 0})
            return Response({"ok": True})

        async def delete(self, url, **kwargs):
            calls.append(("DELETE", url, kwargs))
            if "open.feishu.cn" in url:
                return Response({"code": 0})
            return Response({}, 204)

    async def token(*_args, **_kwargs):
        return "token"

    async def wecom_token(*_args, **_kwargs):
        return {"access_token": "token"}

    monkeypatch.setattr(im_delivery.httpx, "AsyncClient", Client)
    monkeypatch.setattr(im_delivery, "_dingtalk_token", token)
    monkeypatch.setattr(
        feishu_service.feishu_service, "get_tenant_access_token", token
    )
    monkeypatch.setattr(wecom_service, "get_wecom_access_token", wecom_token)
    monkeypatch.setattr(teams_api, "_get_teams_access_token", token)

    config = SimpleNamespace(
        app_id="app", app_secret="secret", extra_config={"service_url": "https://teams.test"}
    )
    part = {
        "part_id": "0",
        "provider_message_id": "provider-1",
        "conversation_ref": "conversation-1",
        "metadata": {"service_url": "https://teams.test"},
    }
    adapters = (
        im_delivery._recall_dingtalk_oto,
        im_delivery._recall_feishu,
        im_delivery._recall_wecom_app,
        im_delivery._recall_slack,
        im_delivery._recall_discord_gateway,
        im_delivery._recall_teams,
    )

    for adapter in adapters:
        result = await adapter(config, [part])
        assert [(item.part_id, item.status) for item in result] == [("0", "recalled")]

    assert len(calls) == len(adapters)


async def test_uncertain_partial_delivery_never_becomes_fully_recalled(monkeypatch):
    agent, user = await _seed_agent()
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="slack",
                app_id="app",
                app_secret="token",
                is_configured=True,
            )
        )
        await db.commit()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("slack"))
    await im_delivery.append_delivery_part(
        row.id,
        IMDeliveryPart(
            transport="test_uncertain_recall",
            provider_message_id="known-part",
            conversation_ref="channel-1",
        ),
    )
    await im_delivery.register_delivery(
        row.id,
        IMDeliveryResult.unknown("slack", "second_chunk_timeout"),
    )

    async def fake_recall(_config, parts):
        return [PartRecallResult(part_id=str(part["part_id"]), status="recalled") for part in parts]

    monkeypatch.setitem(
        im_delivery.IM_RECALL_ADAPTERS,
        "test_uncertain_recall",
        fake_recall,
    )

    result = await _recall(agent, user, row)
    assert result["status"] == "partial"
    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
        payload = serialize_chat_message_for_client(stored)
    assert payload["content"] == "the original visible content"
    assert payload["recall_status"] == "partial"


async def test_dingtalk_group_recall_uses_robot_api_and_retries_transient_failure(monkeypatch):
    config = ChannelConfig(
        agent_id=uuid.uuid4(),
        channel_type="dingtalk",
        app_id="robot-code",
        app_secret="secret",
        is_configured=True,
    )
    requests = []

    class FakeResponse:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, json):
            requests.append((url, headers, json))
            if len(requests) == 1:
                return FakeResponse(
                    {"successResult": [], "failedResult": {"process-key": "not_ready"}}
                )
            return FakeResponse({"successResult": ["process-key"], "failedResult": {}})

    async def fake_token(_config):
        return "access-token"

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(im_delivery, "_dingtalk_token", fake_token)
    monkeypatch.setattr(im_delivery.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(im_delivery.asyncio, "sleep", no_sleep)

    result = await im_delivery._recall_dingtalk_group(
        config,
        [
            {
                "part_id": "0",
                "provider_message_id": "process-key",
                "conversation_ref": "open-conversation-id",
            }
        ],
    )

    assert result == [PartRecallResult(part_id="0", status="recalled")]
    assert len(requests) == 2
    assert requests[0][0].endswith("/v1.0/robot/groupMessages/recall")
    assert requests[0][2] == {
        "robotCode": "robot-code",
        "openConversationId": "open-conversation-id",
        "processQueryKeys": ["process-key"],
    }
