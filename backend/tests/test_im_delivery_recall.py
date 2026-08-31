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

async def test_recall_merges_per_part_provider_results_and_tombstones_only_full_success(monkeypatch):
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

    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.sent(
            "slack",
            IMDeliveryPart(
                transport="test_batch_recall",
                provider_message_id="remote-ok",
                conversation_ref="channel-1",
                artifact_role="chunk",
            ),
            IMDeliveryPart(
                transport="test_batch_recall",
                provider_message_id="remote-fail",
                conversation_ref="channel-1",
                artifact_role="chunk",
            ),
        ),
    )

    async def fake_batch_recall(_config, parts):
        return [
            PartRecallResult(
                part_id=str(part["part_id"]),
                status="recalled" if part["provider_message_id"] == "remote-ok" else "failed",
                error=None if part["provider_message_id"] == "remote-ok" else "provider_rejected",
            )
            for part in parts
        ]

    monkeypatch.setitem(im_delivery.IM_RECALL_ADAPTERS, "test_batch_recall", fake_batch_recall)

    result = await _recall(agent, user, row)

    assert result["status"] == "partial"
    assert [part["status"] for part in result["parts"]] == ["recalled", "failed"]
    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
        client_payload = serialize_chat_message_for_client(stored)
    assert client_payload["content"] == "the original visible content"
    assert client_payload["recall_status"] == "partial"


async def test_full_recall_is_idempotent_and_hides_content_from_client_and_llm(monkeypatch):
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
    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.sent(
            "slack",
            IMDeliveryPart(
                transport="test_full_recall",
                provider_message_id="remote-1",
                conversation_ref="channel-1",
            ),
        ),
    )
    calls = 0

    async def fake_recall(_config, parts):
        nonlocal calls
        calls += 1
        return [PartRecallResult(part_id=str(part["part_id"]), status="recalled") for part in parts]

    monkeypatch.setitem(im_delivery.IM_RECALL_ADAPTERS, "test_full_recall", fake_recall)

    first = await _recall(agent, user, row)
    second = await _recall(agent, user, row)

    assert first["status"] == "recalled"
    assert second == {"status": "already_recalled", "message_id": str(row.id)}
    assert calls == 1
    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
        client_payload = serialize_chat_message_for_client(stored)
        llm_payload = convert_chat_messages_to_llm_format([stored])
        authoritative_llm_payload = build_llm_messages_from_rows(
            [stored],
            include_thinking=True,
        )
    assert client_payload["content"] == "该消息已撤回"
    assert client_payload["display_content"] == "该消息已撤回"
    assert client_payload["recall_status"] == "recalled"
    assert "thinking" not in client_payload
    assert llm_payload == [
        {"role": "assistant", "content": "[该消息已撤回，不应视为仍对用户可见]"}
    ]
    assert authoritative_llm_payload == [
        {"role": "assistant", "content": "[该消息已撤回，不应视为仍对用户可见]"}
    ]


async def test_outbound_media_tool_call_uses_the_same_recall_lifecycle(monkeypatch):
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
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="Media recall",
            source_channel="slack",
            external_conv_id="slack_C1",
        )
        db.add(session)
        await db.flush()
        row = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="tool_call",
            content='{"name":"send_media","status":"done","result":"sent"}',
            conversation_id=str(session.id),
            message_meta={
                "direction": "outbound",
                "attachments": [{"path": "workspace/demo.mp3", "name": "demo.mp3"}],
                "delivery_status": "sent",
                "delivery": IMDeliveryResult.sent(
                    "slack",
                    IMDeliveryPart(
                        transport="test_media_recall",
                        provider_message_id="media-1",
                        conversation_ref="C1",
                        artifact_role="media_audio",
                    ),
                ).to_meta(),
            },
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

    async def fake_recall(_config, parts):
        return [PartRecallResult(part_id=str(part["part_id"]), status="recalled") for part in parts]

    monkeypatch.setitem(im_delivery.IM_RECALL_ADAPTERS, "test_media_recall", fake_recall)
    result = await _recall(agent, user, row)

    assert result["status"] == "recalled"
    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
    assert serialize_chat_message_for_client(stored)["attachments"] == []
    assert convert_chat_messages_to_llm_format([stored]) == [
        {"role": "assistant", "content": "[该消息已撤回，不应视为仍对用户可见]"}
    ]
    assert build_llm_messages_from_rows([stored]) == [
        {"role": "assistant", "content": "[该消息已撤回，不应视为仍对用户可见]"}
    ]


async def test_compacted_recall_invalidates_summary_and_rebuilds_from_visible_rows(monkeypatch):
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
    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.sent(
            "slack",
            IMDeliveryPart(
                transport="test_compacted_recall",
                provider_message_id="remote-compacted",
                conversation_ref="channel-1",
            ),
        ),
    )
    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
        visible = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="assistant",
            content="the still-visible decision",
            conversation_id=stored.conversation_id,
            message_meta={
                "delivery": IMDeliveryResult.unsupported_delivery(
                    "slack",
                    "slack_control",
                ).to_meta()
            },
        )
        db.add(visible)
        await db.flush()
        marker = ChatCompaction(
            session_id=stored.conversation_id,
            agent_id=agent.id,
            epoch=1,
            compacted_from_message_id=stored.id,
            compacted_to_message_id=visible.id,
            summary_text="The assistant sent a proposal and made a decision.",
            summary_tokens=8,
            trigger_prompt_tokens=100,
            trigger_ratio=0.9,
            summary_validation_passed=True,
        )
        db.add(marker)
        await db.flush()
        stored.compacted_into = marker.id
        visible.compacted_into = marker.id
        await db.commit()
        visible_id = visible.id
        marker_id = marker.id

    async def fake_recall(_config, parts):
        return [PartRecallResult(part_id=str(part["part_id"]), status="recalled") for part in parts]

    monkeypatch.setitem(im_delivery.IM_RECALL_ADAPTERS, "test_compacted_recall", fake_recall)

    recalled = await _recall(agent, user, row)
    assert recalled["status"] == "recalled"

    async with async_session() as db:
        rows = await load_messages_for_session(
            db,
            agent_id=agent.id,
            conversation_id=row.conversation_id,
            ctx_size=100,
        )
        stored_marker = await db.get(ChatCompaction, marker_id)
        stored = await db.get(ChatMessage, row.id)
        still_visible = await db.get(ChatMessage, visible_id)

    assert len(rows) == 2
    assert stored_marker.summary_validation_passed is False
    assert stored.compacted_into is None
    assert still_visible.compacted_into is None
    assert stored.content == "the original visible content"
    assert convert_chat_messages_to_llm_format(rows) == [
        {"role": "assistant", "content": "[该消息已撤回，不应视为仍对用户可见]"},
        {"role": "assistant", "content": "the still-visible decision"},
    ]
    summary_input = serialize_span_for_summary(rows)
    assert "the original visible content" not in summary_input
    assert "[该消息已撤回，不应视为仍对用户可见]" in summary_input
    assert "the still-visible decision" in summary_input


async def test_concurrent_recall_returns_recalling_while_provider_call_is_in_flight(monkeypatch):
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
    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.sent(
            "slack",
            IMDeliveryPart(
                transport="test_slow_recall",
                provider_message_id="remote-1",
                conversation_ref="channel-1",
            ),
        ),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_recall(_config, parts):
        entered.set()
        await release.wait()
        return [PartRecallResult(part_id=str(part["part_id"]), status="recalled") for part in parts]

    monkeypatch.setitem(im_delivery.IM_RECALL_ADAPTERS, "test_slow_recall", slow_recall)

    first_task = asyncio.create_task(
        _recall(agent, user, row)
    )
    await entered.wait()
    second = await _recall(agent, user, row)
    release.set()
    first = await first_task

    assert second == {"status": "recalling", "message_id": str(row.id)}
    assert first["status"] == "recalled"


async def test_agent_cannot_recall_another_agents_message():
    agent, user = await _seed_agent()
    other_agent, _ = await _seed_agent()
    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.unsupported_delivery("wechat", "wechat_ilink"),
    )

    result = await im_delivery.recall_message(
        agent_id=other_agent.id,
        message_id=row.id,
        user_id=user.id,
        current_session_id=row.conversation_id,
    )

    assert result == {"status": "not_found", "message_id": str(row.id)}


async def test_plain_member_cannot_recall_same_agents_other_users_message():
    agent, owner = await _seed_agent()
    row = await _seed_message(
        agent,
        owner,
        IMDeliveryResult.unsupported_delivery("wechat", "wechat_ilink"),
    )
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        stored_agent = await db.get(Agent, agent.id)
        stored_agent.access_mode = "company"
        identity = Identity(
            username=f"im_delivery_viewer_{suffix}",
            email=f"im-delivery-viewer-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        viewer = User(
            identity_id=identity.id,
            display_name="Limited IM Viewer",
            role="member",
            is_active=True,
            tenant_id=stored_agent.tenant_id,
        )
        db.add(viewer)
        await db.flush()
        viewer_session = ChatSession(
            agent_id=agent.id,
            user_id=viewer.id,
            title="Viewer current session",
            source_channel="web",
            external_conv_id=f"web_{uuid.uuid4().hex}",
        )
        db.add(viewer_session)
        await db.commit()
        viewer_id = viewer.id
        viewer_session_id = viewer_session.id

    result = await im_delivery.recall_message(
        agent_id=agent.id,
        message_id=row.id,
        user_id=viewer_id,
        current_session_id=viewer_session_id,
    )

    assert result == {"status": "not_found", "message_id": str(row.id)}
