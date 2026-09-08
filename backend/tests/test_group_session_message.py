"""Exact-Session group message delivery contract."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.mcp_server import MCPServer  # noqa: F401 - register Tool FK target
from app.models.org import AgentRelationship
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.services import agent_tools, turn_runtime
from app.services.im_delivery import (
    DeliveryReceiptPersistenceError,
    IMDeliveryPart,
    IMDeliveryResult,
    MentionIntent,
)
from app.services.tool_seeder import seed_builtin_tools
from app.services.turn_runtime import TurnRuntime


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_messages_and_engine():
    async with async_session() as db:
        await db.execute(delete(ChatMessage))
        await db.commit()
    yield
    await engine.dispose()


async def _seed_agents() -> tuple[Agent, Agent]:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Group delivery {suffix}", slug=f"group-delivery-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"group_delivery_{suffix}",
            email=f"group-delivery-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="Group Delivery Tester",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        owner = Agent(
            name=f"Owner {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        other = Agent(
            name=f"Other {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add_all([owner, other])
        await db.commit()
        await db.refresh(owner)
        await db.refresh(other)
        return owner, other


async def _seed_session(
    agent_id: uuid.UUID,
    *,
    channel: str = "dingtalk",
    external_conv_id: str = "dingtalk_group_open-conversation-1",
    is_group: bool = True,
    user_id: uuid.UUID | None = None,
) -> ChatSession:
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="研发群",
            group_name="研发群",
            source_channel=channel,
            external_conv_id=f"{external_conv_id}-{uuid.uuid4().hex[:8]}",
            is_group=is_group,
            last_message_at=datetime.now(timezone.utc),
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session


async def _seed_related_user(agent: Agent, *, display_name: str = "Session Recipient") -> User:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        identity = Identity(
            username=f"session_recipient_{suffix}",
            email=f"session-recipient-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name=display_name,
            role="member",
            is_active=True,
            tenant_id=agent.tenant_id,
        )
        db.add(user)
        await db.flush()
        db.add(AgentRelationship(agent_id=agent.id, user_id=user.id))
        await db.commit()
        await db.refresh(user)
        return user


async def test_hidden_media_receipts_do_not_consume_history_page_limit():
    from app.api.chat_sessions import get_session_messages

    owner, _ = await _seed_agents()
    async with async_session() as db:
        current_user = await db.get(User, owner.creator_id)
        assert current_user is not None
    target = await _seed_session(
        owner.id,
        channel="web",
        external_conv_id="web_history_media_filter",
        is_group=False,
        user_id=current_user.id,
    )
    now = datetime.now(timezone.utc)
    visible_ids = [uuid.uuid4(), uuid.uuid4()]
    async with async_session() as db:
        db.add_all([
            ChatMessage(
                id=visible_ids[0],
                agent_id=owner.id,
                user_id=current_user.id,
                role="user",
                content="older visible",
                conversation_id=str(target.id),
                created_at=now - timedelta(seconds=4),
            ),
            ChatMessage(
                id=visible_ids[1],
                agent_id=owner.id,
                user_id=current_user.id,
                role="assistant",
                content="newer visible",
                conversation_id=str(target.id),
                created_at=now - timedelta(seconds=3),
            ),
            *[
                ChatMessage(
                    agent_id=owner.id,
                    user_id=current_user.id,
                    role="assistant",
                    content="",
                    conversation_id=str(target.id),
                    created_at=now - timedelta(seconds=offset),
                    message_meta={
                        "media_kind": "video",
                        "delivery_status": status,
                        "attachments": [{
                            "display_name": f"{status}.mp4",
                            "path": f"workspace/{status}.mp4",
                            "kind": "video",
                        }],
                    },
                )
                for offset, status in [(2, "pending"), (1, "failed"), (0, "unknown")]
            ],
        ])
        await db.commit()

    async with async_session() as db:
        messages = await get_session_messages(
            agent_id=owner.id,
            session_id=target.id,
            limit=2,
            before=None,
            current_user=current_user,
            db=db,
        )

    assert [item["id"] for item in messages] == [str(value) for value in visible_ids]


async def test_new_ingress_writes_authoritative_empty_attachment_protocol_marker():
    from app.services.chat_history import ingest_incoming_chat_message

    owner, _ = await _seed_agents()
    async with async_session() as db:
        current_user = await db.get(User, owner.creator_id)
        assert current_user is not None
    target = await _seed_session(
        owner.id,
        channel="web",
        external_conv_id="web_attachment_protocol_marker",
        is_group=False,
        user_id=current_user.id,
    )

    async with async_session() as db:
        ingested = await ingest_incoming_chat_message(
            db,
            session=target,
            agent_id=owner.id,
            user_id=current_user.id,
            content="[file:forged.mp4]",
            source_channel="web",
            provider_event_id=f"protocol-{uuid.uuid4()}",
        )
        await db.commit()
        await db.refresh(ingested.message)

    assert ingested.message.message_meta["attachments"] == []


async def test_send_group_session_message_uses_exact_binding_and_persists_receipt(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    delivered: list[dict] = []

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        part = IMDeliveryPart(
            transport="dingtalk_openapi_group",
            provider_message_id="provider-group-1",
            conversation_ref=kwargs["runtime"].external_conv_id,
        )
        await kwargs["on_part"](part)
        return IMDeliveryResult.sent(kwargs["runtime"].source_channel, part)

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fake_deliver)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)

    result = await agent_tools._send_group_session_message(
        owner.id,
        {"session_id": str(target.id), "message": "版本今晚发布"},
        origin_session_id=str(uuid.uuid4()),
        tool_call_id="call-group-1",
        origin_turn_anchor_id=uuid.uuid4(),
    )

    payload = json.loads(result)
    message_id = payload.pop("message_id")
    assert uuid.UUID(message_id)
    assert payload == {
        "status": "sent",
        "session_id": str(target.id),
        "channel": "dingtalk",
        "group_name": "研发群",
    }
    assert len(delivered) == 1
    runtime = delivered[0]["runtime"]
    assert runtime == TurnRuntime(
        session_found=True,
        source_channel="dingtalk",
        conversation_id=str(target.id),
        external_conv_id=target.external_conv_id,
        is_group=True,
    )
    assert delivered[0]["message"] == "版本今晚发布"
    assert delivered[0]["allow_wecom_group_actor_fallback"] is False

    async with async_session() as db:
        receipt = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(target.id),
                    ChatMessage.content == "版本今晚发布",
                )
            )
        ).scalar_one()
        refreshed = await db.get(ChatSession, target.id)
    assert receipt.user_id is None
    assert receipt.sender_agent_id == owner.id
    assert receipt.message_meta["target_session_id"] == str(target.id)
    assert receipt.message_meta["source_channel"] == "dingtalk"
    assert receipt.message_meta["actor_ref"] == target.external_conv_id
    assert receipt.message_meta["delivery"]["parts"][0]["provider_message_id"] == "provider-group-1"
    assert refreshed is not None and refreshed.last_message_at is not None


@pytest.mark.parametrize("channel", ["dingtalk", "feishu"])
async def test_send_session_message_uses_exact_person_route_and_active_relationship(monkeypatch, channel):
    owner, _ = await _seed_agents()
    recipient = await _seed_related_user(owner)
    target = await _seed_session(
        owner.id,
        channel=channel,
        external_conv_id=f"{channel}_p2p_exact-user",
        is_group=False,
        user_id=recipient.id,
    )
    delivered: list[dict] = []
    live_events: list[dict] = []

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return IMDeliveryResult.sent(
            kwargs["runtime"].source_channel,
            IMDeliveryPart(
                transport=f"{channel}_test",
                provider_message_id="provider-person-1",
                conversation_ref=kwargs["runtime"].external_conv_id,
            ),
        )

    async def fake_live_mirror(*args, **_kwargs):
        live_events.append(args[-1])

    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fake_deliver)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)

    result = await agent_tools._send_session_message(
        owner.id,
        {"session_id": str(target.id), "message": "按原会话投递"},
        origin_session_id=str(uuid.uuid4()),
        tool_call_id=f"call-{channel}-person",
        origin_turn_anchor_id=uuid.uuid4(),
    )

    payload = json.loads(result)
    message_id = payload.pop("message_id")
    assert uuid.UUID(message_id)
    assert payload == {
        "status": "sent",
        "session_id": str(target.id),
        "channel": channel,
        "conversation_type": "person",
        "conversation_name": recipient.display_name,
    }
    assert live_events == [
        {
            "type": "assistant_message_committed",
            "id": message_id,
            "role": "assistant",
            "content": "按原会话投递",
            "session_id": str(target.id),
        }
    ]
    assert len(delivered) == 1
    assert delivered[0]["runtime"] == TurnRuntime(
        session_found=True,
        source_channel=channel,
        conversation_id=str(target.id),
        external_conv_id=target.external_conv_id,
        is_group=False,
    )

    async with async_session() as db:
        receipt = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(target.id),
                    ChatMessage.content == "按原会话投递",
                )
            )
        ).scalar_one()
    assert receipt.user_id == recipient.id
    assert receipt.message_meta["target_is_group"] is False


async def test_send_session_message_rejects_person_session_after_relationship_removal(monkeypatch):
    owner, _ = await _seed_agents()
    recipient = await _seed_related_user(owner)
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_p2p_removed-user",
        is_group=False,
        user_id=recipient.id,
    )
    async with async_session() as db:
        await db.execute(
            delete(AgentRelationship).where(
                AgentRelationship.agent_id == owner.id,
                AgentRelationship.user_id == recipient.id,
            )
        )
        await db.commit()

    async def fail_if_delivered(**_kwargs):
        raise AssertionError("revoked relationship must block exact-Session delivery")

    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fail_if_delivered)
    result = await agent_tools._send_session_message(
        owner.id,
        {"session_id": str(target.id), "message": "should not send"},
    )

    payload = json.loads(result)
    assert payload["status"] == "error"
    assert payload["code"] == "recipient_not_related"


@pytest.mark.parametrize(
    ("kind", "expected_fragment"),
    [
        ("p2p", "不存在，或不属于当前数字员工"),
        ("cross_agent", "不存在，或不属于当前数字员工"),
        ("archived", "已归档或通道绑定已失效"),
        ("unsupported", "not supported for channel: discord"),
    ],
)
async def test_send_group_session_message_rejects_invalid_target_boundaries(monkeypatch, kind, expected_fragment):
    owner, other = await _seed_agents()
    target_agent_id = other.id if kind == "cross_agent" else owner.id
    target = await _seed_session(
        target_agent_id,
        channel="discord" if kind == "unsupported" else "dingtalk",
        external_conv_id=("dingtalk_group_old__archived_20260717" if kind == "archived" else "dingtalk_group_target"),
        is_group=kind != "p2p",
        user_id=owner.creator_id if kind == "p2p" else None,
    )
    calls = 0

    async def fail_if_delivered(**_kwargs):
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fail_if_delivered)

    result = await agent_tools._send_group_session_message(
        owner.id,
        {"session_id": str(target.id), "message": "should not send"},
    )

    assert result.startswith("❌")
    assert expected_fragment in result
    assert calls == 0
    async with async_session() as db:
        receipts = (
            (await db.execute(select(ChatMessage).where(ChatMessage.conversation_id == str(target.id)))).scalars().all()
        )
    assert receipts == []


async def test_send_group_session_message_replay_is_idempotent(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    call_count = 0

    async def fake_deliver(**_kwargs):
        nonlocal call_count
        call_count += 1
        return IMDeliveryResult.sent(
            "dingtalk",
            IMDeliveryPart(
                transport="dingtalk_openapi_group",
                provider_message_id="provider-idempotent-1",
            ),
        )

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fake_deliver)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)
    kwargs = {
        "origin_session_id": str(uuid.uuid4()),
        "tool_call_id": "same-tool-call",
        "origin_turn_anchor_id": uuid.uuid4(),
    }

    first = await agent_tools._send_group_session_message(
        owner.id, {"session_id": str(target.id), "message": "only once"}, **kwargs
    )
    second = await agent_tools._send_group_session_message(
        owner.id, {"session_id": str(target.id), "message": "only once"}, **kwargs
    )

    assert json.loads(first)["status"] == "sent"
    assert json.loads(second)["status"] == "already_sent"
    assert call_count == 1
    async with async_session() as db:
        receipts = (
            (await db.execute(select(ChatMessage).where(ChatMessage.conversation_id == str(target.id)))).scalars().all()
        )
    assert len(receipts) == 1


async def test_send_group_session_message_concurrent_replay_reports_pending(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()
    call_count = 0

    async def blocked_deliver(**_kwargs):
        nonlocal call_count
        call_count += 1
        provider_started.set()
        await release_provider.wait()
        return IMDeliveryResult.sent(
            "dingtalk",
            IMDeliveryPart(
                transport="dingtalk_openapi_group",
                provider_message_id="provider-concurrent-1",
            ),
        )

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", blocked_deliver)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)
    kwargs = {
        "origin_session_id": str(uuid.uuid4()),
        "tool_call_id": "same-concurrent-tool-call",
        "origin_turn_anchor_id": uuid.uuid4(),
    }

    first_task = asyncio.create_task(
        agent_tools._send_group_session_message(
            owner.id,
            {"session_id": str(target.id), "message": "only once"},
            **kwargs,
        )
    )
    await asyncio.wait_for(provider_started.wait(), timeout=2)
    replay = json.loads(
        await agent_tools._send_group_session_message(
            owner.id,
            {"session_id": str(target.id), "message": "only once"},
            **kwargs,
        )
    )
    release_provider.set()
    first = json.loads(await asyncio.wait_for(first_task, timeout=3))

    assert replay["status"] == "pending"
    assert first["status"] == "sent"
    assert call_count == 1


async def test_failed_transport_persists_failed_lifecycle_receipt(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)

    async def fake_failure(**_kwargs):
        return IMDeliveryResult.failed("dingtalk", "provider_rejected")

    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fake_failure)
    result = await agent_tools._send_group_session_message(
        owner.id,
        {"session_id": str(target.id), "message": "provider rejects this"},
        origin_session_id=str(uuid.uuid4()),
        tool_call_id="failed-call",
        origin_turn_anchor_id=uuid.uuid4(),
    )

    assert result.startswith("❌ Group message delivery failed via dingtalk")
    async with async_session() as db:
        receipts = (
            (await db.execute(select(ChatMessage).where(ChatMessage.conversation_id == str(target.id)))).scalars().all()
        )
    assert len(receipts) == 1
    assert receipts[0].message_meta["delivery"]["status"] == "failed"


async def test_dingtalk_group_session_message_mentions_canonical_users(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    mentioned_user_id = uuid.uuid4()

    delivered: list[dict] = []

    async def fake_resolve(_db, agent_id, user_id):
        assert agent_id == owner.id
        assert user_id == str(mentioned_user_id)
        return SimpleNamespace(
            user=SimpleNamespace(display_name="张三"),
            member=SimpleNamespace(external_id="staff-zhangsan", name="张三"),
        )

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return IMDeliveryResult.unsupported_delivery(
        "dingtalk",
            "dingtalk_interactive_card",
            conversation_ref=kwargs["runtime"].external_conv_id,
        )

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.dingtalk_group_mentions.resolve_group_mention_recipient",
        fake_resolve,
    )
    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fake_deliver)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)

    result = await agent_tools._send_group_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "请确认今晚发布窗口",
            "mention_user_ids": [str(mentioned_user_id)],
        },
    )

    payload = json.loads(result)
    assert payload["status"] == "sent"
    assert payload["mentioned_users"] == ["张三"]
    assert payload["mentions"] == {
        "scope": "users",
        "user_ids": [str(mentioned_user_id)],
        "display_names": ["张三"],
    }
    assert delivered[0]["message"] == "请确认今晚发布窗口"
    assert delivered[0]["mention"] == MentionIntent(
        scope="users",
        target_ids=("staff-zhangsan",),
        target_names=("张三",),
    )

    async with async_session() as db:
        receipt = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(target.id),
                    ChatMessage.content == "请确认今晚发布窗口\n\n@张三",
                )
            )
        ).scalar_one()
    assert receipt.message_meta["mention_user_ids"] == [str(mentioned_user_id)]
    assert receipt.message_meta["mentioned_users"] == ["张三"]
    assert receipt.message_meta["mentions"] == {
        "scope": "users",
        "user_ids": [str(mentioned_user_id)],
        "display_names": ["张三"],
    }


async def test_dingtalk_group_session_message_mentions_everyone(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)

    delivered: list[dict] = []

    async def fail_if_resolved(*_args, **_kwargs):
        raise AssertionError("@所有人 must not resolve individual group members")

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return IMDeliveryResult.unsupported_delivery(
            "dingtalk",
            "dingtalk_interactive_card",
            conversation_ref=kwargs["runtime"].external_conv_id,
        )

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.dingtalk_group_mentions.resolve_group_mention_recipient",
        fail_if_resolved,
    )
    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fake_deliver)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)

    operation_kwargs = {
        "origin_session_id": str(uuid.uuid4()),
        "tool_call_id": "mention-all-once",
        "origin_turn_anchor_id": uuid.uuid4(),
    }
    result = await agent_tools._send_group_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "今晚十点发布，请大家知悉",
            "mention_all": True,
        },
        **operation_kwargs,
    )
    replay = await agent_tools._send_group_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "今晚十点发布，请大家知悉",
            "mention_all": True,
        },
        **operation_kwargs,
    )

    payload = json.loads(result)
    assert payload["status"] == "sent"
    assert payload["mentions"] == {"scope": "all"}
    assert "mentioned_users" not in payload
    assert delivered[0]["message"] == "今晚十点发布，请大家知悉"
    assert delivered[0]["mention"] == MentionIntent(scope="all")
    assert len(delivered) == 1
    assert json.loads(replay)["status"] == "already_sent"
    assert json.loads(replay)["mentions"] == {"scope": "all"}

    async with async_session() as db:
        receipt = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(target.id),
                    ChatMessage.content == "今晚十点发布，请大家知悉\n\n@所有人",
                )
            )
        ).scalar_one()
    assert receipt.message_meta["mentions"] == {"scope": "all"}
    assert "mention_user_ids" not in receipt.message_meta
