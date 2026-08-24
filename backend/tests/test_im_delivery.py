"""Observable message-lifecycle behavior for normalized IM delivery receipts."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_compaction import ChatCompaction
from app.models.chat_session import ChatSession
from app.models.mcp_server import MCPServer  # noqa: F401 - register Tool FK target
from app.models.participant import Participant  # noqa: F401 - register ChatMessage FK target
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.services import agent_tools, im_delivery
from app.services.chat_history import build_llm_messages_from_rows, load_messages_for_session
from app.services.chat_message_serializer import serialize_chat_message_for_client
from app.services.channel_commands import prepare_channel_command_reply
from app.services.im_delivery import IMDeliveryPart, IMDeliveryResult, PartRecallResult
from app.services.llm.compactor import serialize_span_for_summary
from app.services.llm.utils import convert_chat_messages_to_llm_format
from app.scripts.rollback_im_recall import remove_builtin_tool

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_messages_and_engine():
    async with async_session() as db:
        await db.execute(delete(ChatMessage))
        await db.commit()
    yield
    await engine.dispose()


async def _seed_agent() -> tuple[Agent, User]:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"IM delivery {suffix}", slug=f"im-delivery-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"im_delivery_{suffix}",
            email=f"im-delivery-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="IM Delivery Tester",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"Delivery Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        await db.refresh(user)
        return agent, user


async def _seed_message(
    agent: Agent,
    user: User,
    result: IMDeliveryResult,
) -> ChatMessage:
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="IM delivery test",
            source_channel="web",
            external_conv_id=f"web_{uuid.uuid4().hex}",
        )
        db.add(session)
        await db.flush()
        row = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="assistant",
            content="the original visible content",
            conversation_id=str(session.id),
            thinking="private chain-of-thought summary",
            message_meta={"delivery": result.to_meta()},
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _recall(agent: Agent, user: User, row: ChatMessage) -> dict:
    return await im_delivery.recall_message(
        agent_id=agent.id,
        message_id=row.id,
        user_id=user.id,
        current_session_id=row.conversation_id,
    )


async def test_command_reply_is_pending_before_provider_send_and_reuses_session():
    agent, user = await _seed_agent()
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="Command lifecycle",
            source_channel="slack",
            external_conv_id="slack_D_COMMAND",
        )
        db.add(session)
        await db.commit()

        result = await prepare_channel_command_reply(
            db,
            command="/help",
            agent_id=agent.id,
            user_id=user.id,
            external_user_id="U_COMMAND",
            external_conv_id="slack_D_COMMAND",
            source_channel="slack",
            provider_event_id="event-command-1",
        )
        await db.commit()

    assert result["conversation_id"] == str(session.id)
    assert result["should_deliver"] is True
    assert result["replayed"] is False
    async with async_session() as db:
        row = await db.get(ChatMessage, uuid.UUID(result["message_id"]))
    assert row.role == "assistant"
    assert row.message_meta["artifact_role"] == "command_reply"
    assert row.message_meta["delivery"]["status"] == "pending"
    assert row.external_event_key.startswith(f"channel-command:slack:{agent.id}:")


async def test_proactive_channel_claim_is_single_sender_and_sanitizes_every_boundary(
    monkeypatch,
):
    from app.services import dingtalk_service

    agent, user = await _seed_agent()
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="dingtalk",
                app_id=f"app-{uuid.uuid4().hex}",
                app_secret="secret",
                is_configured=True,
                extra_config={"agent_id": "provider-agent"},
            )
        )
        await db.commit()

    async def resolve_platform_user(**_kwargs):
        return user

    entered = asyncio.Event()
    release = asyncio.Event()
    provider_messages: list[str] = []

    async def send_provider(**kwargs):
        provider_messages.append(kwargs["message"])
        entered.set()
        await release.wait()
        return {"errcode": 0, "processQueryKey": "provider-process-key"}

    monkeypatch.setattr(
        agent_tools,
        "get_platform_user_by_org_member",
        resolve_platform_user,
    )
    monkeypatch.setattr(dingtalk_service, "send_dingtalk_message", send_provider)

    forbidden = "cLaW" + "iTh"
    raw_message = f"before【{forbidden}】after"
    member = SimpleNamespace(
        external_id="staff-proactive",
        unionid=None,
        open_id=None,
    )

    async def resolve_route(*_args, **_kwargs):
        return SimpleNamespace(member=member, user=user, channel="dingtalk")

    monkeypatch.setattr(
        agent_tools,
        "resolve_human_channel_recipient",
        resolve_route,
    )
    turn_anchor_id = uuid.uuid4()
    kwargs = {
        "origin_session_id": "origin-session",
        "origin_user_id": user.id,
        "tool_call_id": "same-proactive-call",
        "origin_turn_anchor_id": turn_anchor_id,
    }

    arguments = {
        "user_id": str(user.id),
        "message": raw_message,
        "channel": "dingtalk",
    }
    first = asyncio.create_task(
        agent_tools._send_channel_message(agent.id, arguments, **kwargs)
    )
    await entered.wait()
    second = await agent_tools._send_channel_message(agent.id, arguments, **kwargs)
    release.set()
    first_result = await first

    assert "message_id" in first_result
    assert "will not be sent twice" in second
    assert len(provider_messages) == 1
    assert forbidden.casefold() not in provider_messages[0].casefold()

    async with async_session() as db:
        rows = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.agent_id == agent.id,
                    ChatMessage.external_event_key.is_not(None),
                )
            )
        ).scalars().all()
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent.id,
                    ChatSession.external_conv_id == "dingtalk_p2p_staff-proactive",
                )
            )
        ).scalar_one()

    assert len(rows) == 1
    assert forbidden.casefold() not in rows[0].content.casefold()
    assert forbidden.casefold() not in session.title.casefold()


async def test_reset_reply_anchors_to_archived_session_without_creating_active_ghost():
    agent, user = await _seed_agent()
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="Old session",
            source_channel="dingtalk",
            external_conv_id="dingtalk_p2p_staff-command",
        )
        db.add(session)
        await db.commit()

        result = await prepare_channel_command_reply(
            db,
            command="/new",
            agent_id=agent.id,
            user_id=user.id,
            external_user_id="staff-command",
            external_conv_id="dingtalk_p2p_staff-command",
            source_channel="dingtalk",
            provider_event_id="event-reset-1",
        )
        await db.commit()

        active = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent.id,
                    ChatSession.source_channel == "dingtalk",
                    ChatSession.external_conv_id == "dingtalk_p2p_staff-command",
                )
            )
        ).scalar_one_or_none()
        archived = await db.get(ChatSession, session.id)

    assert active is None
    assert result["conversation_id"] == str(session.id)
    assert "__archived_" in archived.external_conv_id


async def test_duplicate_reset_event_archives_once_and_never_requests_redelivery():
    agent, user = await _seed_agent()
    external_conv_id = "dingtalk_p2p_duplicate-reset"
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="Old session",
            source_channel="dingtalk",
            external_conv_id=external_conv_id,
        )
        db.add(session)
        await db.commit()
        session_id = session.id

    async def invoke() -> dict:
        async with async_session() as command_db:
            result = await prepare_channel_command_reply(
                command_db,
                command="/new",
                agent_id=agent.id,
                user_id=user.id,
                external_user_id="duplicate-reset",
                external_conv_id=external_conv_id,
                source_channel="dingtalk",
                provider_event_id="same-provider-event",
            )
            await command_db.commit()
            return result

    results = await asyncio.gather(invoke(), invoke())
    first = next(result for result in results if result["should_deliver"])
    replay = next(result for result in results if result["replayed"])
    provider_sends = [result["message"] for result in results if result["should_deliver"]]
    async with async_session() as db:
        archived = await db.get(ChatSession, session_id)
        command_rows = list((await db.execute(
            select(ChatMessage).where(
                ChatMessage.message_meta["artifact_role"].as_string() == "command_reply"
            )
        )).scalars())

    assert first["should_deliver"] is True
    assert first["replayed"] is False
    assert replay["should_deliver"] is False
    assert replay["replayed"] is True
    assert replay["message_id"] == first["message_id"]
    assert replay["external_event_key"] == first["external_event_key"]
    assert archived.external_conv_id.count("__archived_") == 1
    assert [str(row.id) for row in command_rows] == [first["message_id"]]
    assert provider_sends == [first["message"]]


async def test_persisted_part_survives_second_part_timeout_as_partial_unknown():
    from app.services.im_delivery import PersistedDeliveryRecorder

    agent, user = await _seed_agent()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("slack"))
    recorder = PersistedDeliveryRecorder(row.id, "slack")
    await recorder.append(IMDeliveryPart(
        transport="slack",
        provider_message_id="first-ts",
        conversation_ref="C123",
        artifact_role="chunk",
    ))
    await recorder.failed(TimeoutError())

    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
    delivery = stored.message_meta["delivery"]
    assert delivery["status"] == "partial"
    assert delivery["uncertain"] is True
    assert [part["provider_message_id"] for part in delivery["parts"]] == ["first-ts"]
    assert delivery["parts"][0]["recall_status"] == "available"


async def test_persisted_delivery_rejects_wrong_conversation_before_provider(monkeypatch):
    from app.services import turn_runtime
    from app.services.turn_runtime import TurnRuntime

    agent, user = await _seed_agent()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("slack"))
    provider = pytest.fail
    monkeypatch.setattr(turn_runtime, "deliver_message_with_receipt", provider)

    with pytest.raises(RuntimeError, match="conversation_mismatch"):
        await im_delivery.deliver_persisted_message(
            message_id=row.id,
            agent_id=agent.id,
            runtime=TurnRuntime(
                session_found=True,
                source_channel="slack",
                conversation_id=str(uuid.uuid4()),
                external_conv_id="slack_wrong",
                is_group=False,
            ),
            message=row.content,
        )


async def test_persisted_delivery_rejects_terminal_receipt_before_provider(monkeypatch):
    from app.services import turn_runtime
    from app.services.turn_runtime import TurnRuntime

    agent, user = await _seed_agent()
    row = await _seed_message(
        agent,
        user,
        IMDeliveryResult.unsupported_delivery("web", "websocket"),
    )
    provider = pytest.fail
    monkeypatch.setattr(turn_runtime, "deliver_message_with_receipt", provider)

    with pytest.raises(RuntimeError, match="not_pending"):
        await im_delivery.deliver_persisted_message(
            message_id=row.id,
            agent_id=agent.id,
            runtime=TurnRuntime(
                session_found=True,
                source_channel="web",
                conversation_id=row.conversation_id,
                external_conv_id="web_terminal",
                is_group=False,
            ),
            message=row.content,
        )


async def test_persisted_delivery_claim_allows_only_one_provider_call(monkeypatch):
    from app.services import turn_runtime
    from app.services.turn_runtime import TurnRuntime

    agent, user = await _seed_agent()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("slack"))
    entered = asyncio.Event()
    release = asyncio.Event()
    first_claim_read = asyncio.Event()
    second_claim_read = asyncio.Event()
    release_claim = asyncio.Event()
    claim_entries = 0
    provider_calls = 0

    async def observe_claim(_message_id):
        nonlocal claim_entries
        claim_entries += 1
        if claim_entries == 1:
            first_claim_read.set()
            await release_claim.wait()
        else:
            second_claim_read.set()

    async def provider(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        entered.set()
        await release.wait()
        return IMDeliveryResult.unsupported_delivery("slack", "test_transport")

    monkeypatch.setattr(turn_runtime, "deliver_message_with_receipt", provider)
    runtime = TurnRuntime(
        session_found=True,
        source_channel="slack",
        conversation_id=row.conversation_id,
        external_conv_id="slack_claim",
        is_group=False,
    )
    observer_token = im_delivery.delivery_claim_observer.set(observe_claim)
    first = asyncio.create_task(
        im_delivery.deliver_persisted_message(
            message_id=row.id,
            agent_id=agent.id,
            runtime=runtime,
            message=row.content,
        )
    )
    await first_claim_read.wait()
    second = asyncio.create_task(
        im_delivery.deliver_persisted_message(
            message_id=row.id,
            agent_id=agent.id,
            runtime=runtime,
            message=row.content,
        )
    )
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(second_claim_read.wait(), timeout=0.1)
        release_claim.set()
        await entered.wait()
        with pytest.raises(RuntimeError, match="delivery_in_progress"):
            await second
    finally:
        release_claim.set()
        release.set()
        im_delivery.delivery_claim_observer.reset(observer_token)
    assert (await first).ok is True
    assert claim_entries == 1
    assert provider_calls == 1


async def test_incremental_part_keeps_delivery_claim_until_provider_finishes(monkeypatch):
    from app.services import turn_runtime
    from app.services.turn_runtime import TurnRuntime

    agent, user = await _seed_agent()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("slack"))
    part_recorded = asyncio.Event()
    release = asyncio.Event()
    provider_calls = 0

    async def provider(*, on_part, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        part = IMDeliveryPart(
            transport="slack",
            provider_message_id="remote-first-part",
            conversation_ref="channel-1",
            artifact_role="chunk",
        )
        await on_part(part)
        part_recorded.set()
        await release.wait()
        return IMDeliveryResult.sent("slack", part)

    monkeypatch.setattr(turn_runtime, "deliver_message_with_receipt", provider)
    runtime = TurnRuntime(
        session_found=True,
        source_channel="slack",
        conversation_id=row.conversation_id,
        external_conv_id="slack_incremental_claim",
        is_group=False,
    )
    first = asyncio.create_task(
        im_delivery.deliver_persisted_message(
            message_id=row.id,
            agent_id=agent.id,
            runtime=runtime,
            message=row.content,
        )
    )
    await part_recorded.wait()
    try:
        with pytest.raises(RuntimeError, match="delivery_in_progress"):
            await im_delivery.deliver_persisted_message(
                message_id=row.id,
                agent_id=agent.id,
                runtime=runtime,
                message=row.content,
            )
    finally:
        release.set()
    assert (await first).ok is True
    assert provider_calls == 1


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
