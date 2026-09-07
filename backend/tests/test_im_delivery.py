"""Observable message-lifecycle behavior for normalized IM delivery receipts."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC as UTC, datetime as datetime, timedelta as timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_compaction import ChatCompaction as ChatCompaction
from app.models.chat_session import ChatSession
from app.models.mcp_server import MCPServer  # noqa: F401 - register Tool FK target
from app.models.participant import Participant  # noqa: F401 - register ChatMessage FK target
from app.models.tenant import Tenant
from app.models.tool import AgentTool as AgentTool, Tool as Tool
from app.models.user import Identity, User
from app.scripts.rollback_im_recall import remove_builtin_tool as remove_builtin_tool
from app.services import agent_tools, im_delivery
from app.services.channel_commands import prepare_channel_command_reply
from app.services.chat_history import build_llm_messages_from_rows as build_llm_messages_from_rows, load_messages_for_session as load_messages_for_session
from app.services.chat_message_serializer import serialize_chat_message_for_client as serialize_chat_message_for_client
from app.services.im_delivery import IMDeliveryPart, IMDeliveryResult, PartRecallResult as PartRecallResult
from app.services.llm.compactor import serialize_span_for_summary as serialize_span_for_summary
from app.services.llm.utils import convert_chat_messages_to_llm_format as convert_chat_messages_to_llm_format

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
        active = await db.scalar(select(ChatSession).where(
            ChatSession.agent_id == agent.id,
            ChatSession.source_channel == "dingtalk",
            ChatSession.external_conv_id == external_conv_id,
        ))
        command_rows = list((await db.execute(
            select(ChatMessage).where(
                ChatMessage.message_meta["artifact_role"].as_string() == "command_reply"
            )
        )).scalars())

    assert active is None
    assert first["conversation_id"] == str(session_id)
    assert first["should_deliver"] is True
    assert first["replayed"] is False
    assert replay["should_deliver"] is False
    assert replay["replayed"] is True
    assert replay["message_id"] == first["message_id"]
    assert replay["external_event_key"] == first["external_event_key"]
    assert archived.external_conv_id.count("__archived_") == 1
    assert [str(row.id) for row in command_rows] == [first["message_id"]]
    assert provider_sends == [first["message"]]


async def test_reset_releases_identity_locks_before_locking_chat_session(monkeypatch):
    """A command and ordinary ingress must never acquire User/Session inversely."""
    agent, user = await _seed_agent()
    external_conv_id = f"im_p2p_lock-order-{uuid.uuid4().hex}"
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="Lock order regression",
            source_channel="slack",
            external_conv_id=external_conv_id,
        )
        db.add(session)
        await db.commit()
        session_id = session.id

    stop_entered = asyncio.Event()
    ingress_locked_session = asyncio.Event()

    async def pause_stop_tree(**_kwargs):
        stop_entered.set()
        await ingress_locked_session.wait()
        return SimpleNamespace(stopped=False)

    async def no_local_turn(_lock_key: str) -> bool:
        return False

    monkeypatch.setattr(
        "app.services.turn_control.stop_session_turn_tree",
        pause_stop_tree,
    )
    monkeypatch.setattr(
        "app.services.channel_commands.cancel_running_turn",
        no_local_turn,
    )

    async def reset_command() -> dict:
        async with async_session() as command_db:
            # Model identity enrichment performed by any IM adapter: this
            # transaction owns a User lock before entering the command path.
            await command_db.execute(
                select(User).where(User.id == user.id).with_for_update()
            )
            result = await prepare_channel_command_reply(
                command_db,
                command="/new",
                agent_id=agent.id,
                user_id=user.id,
                external_user_id="lock-order-user",
                external_conv_id=external_conv_id,
                source_channel="slack",
                provider_event_id=f"lock-order-command-{uuid.uuid4().hex}",
            )
            await command_db.commit()
            return result

    async def ordinary_ingress() -> None:
        await stop_entered.wait()
        async with async_session() as ingress_db:
            await ingress_db.execute(
                select(ChatSession)
                .where(ChatSession.id == session_id)
                .with_for_update()
            )
            ingress_locked_session.set()
            ingress_db.add(
                ChatMessage(
                    agent_id=agent.id,
                    user_id=user.id,
                    role="user",
                    content="message racing with reset",
                    conversation_id=str(session_id),
                    external_event_key=(
                        f"lock-order-ingress-{uuid.uuid4().hex}"
                    ),
                )
            )
            await ingress_db.commit()

    command_result, _ = await asyncio.wait_for(
        asyncio.gather(reset_command(), ordinary_ingress()),
        timeout=5,
    )

    async with async_session() as db:
        archived = await db.get(ChatSession, session_id)
        ingress_row = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.content == "message racing with reset"
                )
            )
        ).scalar_one()

    assert command_result["action"] == "new_session"
    assert "__archived_" in archived.external_conv_id
    assert ingress_row.user_id == user.id


async def test_stop_releases_identity_locks_before_unified_turn_tree_stop(monkeypatch):
    """IM /stop must not retain a User lock while shared STOP waits on Session."""
    agent, user = await _seed_agent()
    external_conv_id = f"im_p2p_stop-lock-order-{uuid.uuid4().hex}"
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="Shared stop lock order regression",
            source_channel="teams",
            external_conv_id=external_conv_id,
        )
        db.add(session)
        await db.commit()
        session_id = session.id

    stop_entered = asyncio.Event()
    ingress_locked_session = asyncio.Event()

    async def unified_stop(**_kwargs):
        stop_entered.set()
        await ingress_locked_session.wait()
        async with async_session() as stop_db:
            await stop_db.execute(
                select(ChatSession)
                .where(ChatSession.id == session_id)
                .with_for_update()
            )
            await stop_db.commit()
        return SimpleNamespace(stopped=True)

    async def no_local_turn(_lock_key: str) -> bool:
        return False

    monkeypatch.setattr(
        "app.services.turn_control.stop_session_turn_tree",
        unified_stop,
    )
    monkeypatch.setattr(
        "app.services.channel_commands.cancel_running_turn",
        no_local_turn,
    )

    async def stop_command() -> dict:
        async with async_session() as command_db:
            await command_db.execute(
                select(User).where(User.id == user.id).with_for_update()
            )
            result = await prepare_channel_command_reply(
                command_db,
                command="/stop",
                agent_id=agent.id,
                user_id=user.id,
                external_user_id="lock-order-user",
                external_conv_id=external_conv_id,
                source_channel="teams",
                provider_event_id=f"lock-order-stop-{uuid.uuid4().hex}",
            )
            await command_db.commit()
            return result

    async def ordinary_ingress() -> None:
        await stop_entered.wait()
        async with async_session() as ingress_db:
            await ingress_db.execute(
                select(ChatSession)
                .where(ChatSession.id == session_id)
                .with_for_update()
            )
            ingress_locked_session.set()
            ingress_db.add(
                ChatMessage(
                    agent_id=agent.id,
                    user_id=user.id,
                    role="user",
                    content="message racing with stop",
                    conversation_id=str(session_id),
                    external_event_key=f"stop-ingress-{uuid.uuid4().hex}",
                )
            )
            await ingress_db.commit()

    command_result, _ = await asyncio.wait_for(
        asyncio.gather(stop_command(), ordinary_ingress()),
        timeout=5,
    )

    async with async_session() as db:
        current = await db.get(ChatSession, session_id)

    assert command_result["action"] == "stop_turn"
    assert command_result["message"] == "已请求停止当前工作。"
    assert current.external_conv_id == external_conv_id


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


async def test_lifecycle_terminal_delivery_does_not_publish_second_web_done(monkeypatch):
    from app.services import turn_runtime
    from app.services.turn_runtime import TurnRuntime

    agent, user = await _seed_agent()
    row = await _seed_message(agent, user, IMDeliveryResult.pending("web"))
    async with async_session() as db:
        stored = await db.get(ChatMessage, row.id)
        stored.message_meta = {
            **dict(stored.message_meta or {}),
            "turn_terminal_published_by_lifecycle": True,
        }
        await db.commit()
    monkeypatch.setattr(
        turn_runtime,
        "deliver_message_with_receipt",
        pytest.fail,
    )

    result = await im_delivery.deliver_persisted_message(
        message_id=row.id,
        agent_id=agent.id,
        runtime=TurnRuntime(
            session_found=True,
            source_channel="web",
            conversation_id=row.conversation_id,
            external_conv_id="web_lifecycle_terminal",
            is_group=False,
        ),
        message=row.content,
    )

    assert result.channel == "web"
    assert result.parts[0].transport == "websocket"


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
