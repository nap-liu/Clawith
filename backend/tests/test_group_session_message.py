"""Exact-Session group message delivery contract."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
from app.services.im_delivery import IMDeliveryPart, IMDeliveryResult, MentionIntent
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
        return IMDeliveryResult.sent(
            kwargs["runtime"].source_channel,
            IMDeliveryPart(
                transport="dingtalk_openapi_group",
                provider_message_id="provider-group-1",
                conversation_ref=kwargs["runtime"].external_conv_id,
            ),
        )

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

    async def fake_live_mirror(*_args, **_kwargs):
        return None

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
    from app.services.dingtalk_group_mentions import cache_group_session_webhook

    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    mentioned_user_id = uuid.uuid4()
    webhook = "https://oapi.dingtalk.com/robot/sendBySession?session=secret"
    async with async_session() as db:
        stored = await cache_group_session_webhook(
            db,
            agent_id=owner.id,
            external_conv_id=target.external_conv_id,
            webhook=webhook,
            expires_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000) + 600_000,
        )
        await db.commit()
        assert stored is not None
        encrypted_config = dict(stored.im_config or {})
    assert webhook not in json.dumps(encrypted_config)

    delivered: list[dict] = []

    async def fake_resolve(_db, agent_id, user_id, *, channel):
        assert agent_id == owner.id
        assert user_id == str(mentioned_user_id)
        assert channel == "dingtalk"
        return SimpleNamespace(
            user=SimpleNamespace(display_name="张三"),
            member=SimpleNamespace(external_id="staff-zhangsan", name="张三"),
        )

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return IMDeliveryResult.unsupported_delivery(
            "dingtalk",
            "dingtalk_session_webhook",
            conversation_ref=kwargs["runtime"].external_conv_id,
        )

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.dingtalk_group_mentions.resolve_human_channel_recipient",
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
    )

    async with async_session() as db:
        receipt = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(target.id),
                    ChatMessage.content == "@张三\n请确认今晚发布窗口",
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
    from app.services.dingtalk_group_mentions import cache_group_session_webhook

    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    webhook = "https://oapi.dingtalk.com/robot/sendBySession?session=all-secret"
    async with async_session() as db:
        await cache_group_session_webhook(
            db,
            agent_id=owner.id,
            external_conv_id=target.external_conv_id,
            webhook=webhook,
            expires_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000) + 600_000,
        )
        await db.commit()

    delivered: list[dict] = []

    async def fail_if_resolved(*_args, **_kwargs):
        raise AssertionError("@所有人 must not resolve individual group members")

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return IMDeliveryResult.unsupported_delivery(
            "dingtalk",
            "dingtalk_session_webhook",
            conversation_ref=kwargs["runtime"].external_conv_id,
        )

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.dingtalk_group_mentions.resolve_human_channel_recipient",
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
                    ChatMessage.content == "@所有人\n今晚十点发布，请大家知悉",
                )
            )
        ).scalar_one()
    assert receipt.message_meta["mentions"] == {"scope": "all"}
    assert "mention_user_ids" not in receipt.message_meta


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}])
async def test_session_message_rejects_non_boolean_mention_all(value):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)

    result = await agent_tools._send_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "不应发送",
            "mention_all": value,
        },
    )

    assert result == "❌ mention_all must be a boolean"


async def test_session_message_rejects_conflicting_native_mentions():
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)

    result = await agent_tools._send_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "不应发送",
            "mention_all": True,
            "mention_user_ids": [str(uuid.uuid4())],
        },
    )

    assert result == "❌ mention_all=true cannot be combined with mention_user_ids"


@pytest.mark.parametrize(
    ("channel", "is_group"),
    [("dingtalk", False), ("feishu", True)],
)
async def test_session_message_rejects_unsupported_mention_all_before_persist(
    monkeypatch,
    channel,
    is_group,
):
    owner, _ = await _seed_agents()
    user_id = owner.creator_id if not is_group else None
    target = await _seed_session(
        owner.id,
        channel=channel,
        external_conv_id=f"{channel}_mention-all-boundary",
        is_group=is_group,
        user_id=user_id,
    )

    async def fail_if_delivered(**_kwargs):
        raise AssertionError("unsupported mention must fail before delivery")

    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fail_if_delivered)
    result = await agent_tools._send_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "不应发送",
            "mention_all": True,
        },
    )

    assert result == "❌ 原生 @ 当前仅支持钉钉群 Session。"
    async with async_session() as db:
        receipts = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.conversation_id == str(target.id))
            )
        ).scalars().all()
    assert receipts == []


async def test_dingtalk_webhook_merge_preserves_concurrent_scene_and_model_switches():
    from app.services.dingtalk_group_mentions import (
        cache_group_session_webhook,
        load_group_session_webhook,
    )

    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    selected_model_id = str(uuid.uuid4())
    webhook = "https://oapi.dingtalk.com/robot/sendBySession?session=concurrent"

    async with async_session() as stale_db:
        stale = await stale_db.get(ChatSession, target.id)
        assert stale is not None and stale.im_config == {}
        stale.im_config = {"stale_write": "must-not-survive"}

        async with async_session() as command_db:
            locked = (
                await command_db.execute(select(ChatSession).where(ChatSession.id == target.id).with_for_update())
            ).scalar_one()
            locked.im_config = {
                "scene_key": "warranty",
                "model_id": selected_model_id,
            }
            await command_db.commit()

        merged = await cache_group_session_webhook(
            stale_db,
            agent_id=owner.id,
            external_conv_id=target.external_conv_id,
            webhook=webhook,
            expires_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000) + 600_000,
        )
        assert merged is not None
        await stale_db.commit()

    async with async_session() as db:
        refreshed = await db.get(ChatSession, target.id)
        assert refreshed is not None
        assert refreshed.im_config["scene_key"] == "warranty"
        assert refreshed.im_config["model_id"] == selected_model_id
        assert "stale_write" not in refreshed.im_config
        assert load_group_session_webhook(refreshed) == webhook


async def test_dingtalk_webhook_cache_never_regresses_to_older_callback():
    from app.services.dingtalk_group_mentions import (
        cache_group_session_webhook,
        load_group_session_webhook,
    )

    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    newer_webhook = "https://oapi.dingtalk.com/robot/sendBySession?session=newer"
    older_webhook = "https://oapi.dingtalk.com/robot/sendBySession?session=older"

    async with async_session() as db:
        await cache_group_session_webhook(
            db,
            agent_id=owner.id,
            external_conv_id=target.external_conv_id,
            webhook=newer_webhook,
            expires_at_ms=now_ms + 900_000,
        )
        await db.commit()

    async with async_session() as db:
        await cache_group_session_webhook(
            db,
            agent_id=owner.id,
            external_conv_id=target.external_conv_id,
            webhook=older_webhook,
            expires_at_ms=now_ms + 600_000,
        )
        await db.commit()

    async with async_session() as db:
        refreshed = await db.get(ChatSession, target.id)
        assert refreshed is not None
        assert load_group_session_webhook(refreshed) == newer_webhook
        assert refreshed.im_config["dingtalk_session_webhook_expires_at_ms"] == (now_ms + 900_000)


async def test_dingtalk_group_mention_requires_recent_group_webhook(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)

    async def fail_if_called(**_kwargs):
        raise AssertionError("delivery must not run without a temporary group webhook")

    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fail_if_called)
    result = await agent_tools._send_group_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "请确认",
            "mention_user_ids": [str(uuid.uuid4())],
        },
    )

    assert result.startswith("❌")
    assert "先在群内 @数字员工" in result


async def test_dingtalk_runtime_delivers_to_exact_group_conversation(monkeypatch):
    owner, _ = await _seed_agents()
    app_id = f"ding-app-{uuid.uuid4().hex}"
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=app_id,
                app_secret="ding-secret",
                is_configured=True,
            )
        )
        await db.commit()
    captured = {}

    async def fake_group_send(**kwargs):
        captured.update(kwargs)
        return {"errcode": 0}

    monkeypatch.setattr(turn_runtime, "_send_dingtalk_group_markdown", fake_group_send)
    runtime = TurnRuntime(
        session_found=True,
        source_channel="dingtalk",
        conversation_id=str(uuid.uuid4()),
        external_conv_id="dingtalk_group_open-conversation-exact",
        is_group=True,
    )

    sent = await turn_runtime.deliver_message_to_runtime(
        agent_id=owner.id,
        runtime=runtime,
        message="exact target",
    )

    assert sent is True
    assert captured == {
        "app_id": app_id,
        "app_secret": "ding-secret",
        "open_conversation_id": "open-conversation-exact",
        "message": "exact target",
    }


async def test_send_video_targets_exact_group_session_with_custom_cover(
    tmp_path, monkeypatch
):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_group_media-room",
        is_group=True,
    )
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=f"ding-media-{uuid.uuid4().hex}",
                app_secret="ding-secret",
                is_configured=True,
            )
        )
        await db.commit()
    video = tmp_path / "demo.mp4"
    cover = tmp_path / "cover.png"
    video.write_bytes(b"video")
    cover.write_bytes(b"cover")
    calls = []

    async def fake_video(app_id, app_secret, target_id, file_path, conversation_type, **kwargs):
        calls.append({
            "target_id": target_id,
            "file_path": file_path,
            "conversation_type": conversation_type,
            "cover": kwargs.get("cover_image_path"),
        })
        await kwargs["on_result"]({"processQueryKey": "group-video-process-key"})
        return True, "MEDIA_SENT"

    async def fake_caption(**_kwargs):
        return IMDeliveryResult.sent("dingtalk")

    live_events = []

    async def fake_live_mirror(*args, **_kwargs):
        live_events.append(args[-1])

    monkeypatch.setattr(
        "app.services.dingtalk_stream._send_dingtalk_native_video",
        fake_video,
    )
    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fake_caption)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)
    kwargs = {
        "agent_id": owner.id,
        "session_id": str(target.id),
        "file_path": video,
        "workspace_path": "workspace/demo.mp4",
        "media_kind": "video",
        "caption": "群视频",
        "cover_path": cover,
        "intent_id": "media-group-call",
        "origin_session_id": str(uuid.uuid4()),
        "origin_turn_anchor_id": uuid.uuid4(),
        "tool_args": {
            "media_type": "video",
            "file_path": "workspace/demo.mp4",
            "title": "示例媒体标题",
        },
    }

    first = json.loads(await agent_tools._send_media_to_session(**kwargs))
    second = json.loads(await agent_tools._send_media_to_session(**kwargs))

    assert first["status"] == "sent"
    assert first["conversation_type"] == "group"
    assert first["session_id"] == str(target.id)
    assert first["title"] == "示例媒体标题"
    assert second["status"] == "already_sent"
    assert second["title"] == "示例媒体标题"
    assert calls == [{
        "target_id": target.external_conv_id.removeprefix("dingtalk_group_"),
        "file_path": video,
        "conversation_type": "2",
        "cover": cover,
    }]
    assert len(live_events) == 2
    assert live_events[0]["type"] == "tool_call"
    assert live_events[0]["name"] == "send_media"
    assert live_events[0]["call_id"] == "media-group-call"
    render_result = json.loads(live_events[0]["result"])
    assert render_result["type"] == "platform_media_delivery"
    assert render_result["path"] == "workspace/demo.mp4"
    assert render_result["title"] == "示例媒体标题"
    assert render_result["allow_download"] is False
    assert live_events[1]["type"] == "assistant_message_committed"
    assert live_events[1]["content"] == "群视频"
    assert live_events[1]["attachments"] == []
    async with async_session() as db:
        rows = list((
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(target.id),
                    ChatMessage.external_event_key.is_not(None),
                ).order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            )
        ).scalars().all())
    assert len(rows) == 2
    receipt = next(row for row in rows if row.message_meta.get("media_kind") == "video")
    caption_row = next(row for row in rows if row.message_meta.get("media_caption_for"))
    assert receipt.role == "tool_call"
    assert caption_row.content == "群视频"
    assert caption_row.message_meta["attachments"] == []
    assert caption_row.message_meta["media_caption_for"] == str(receipt.id)
    assert receipt.message_meta["attachments"] == [{
        "display_name": "demo.mp4",
        "path": "workspace/demo.mp4",
        "kind": "video",
        "mime_type": "video/mp4",
        "size_bytes": 5,
    }]
    assert receipt.message_meta["target_is_group"] is True
    assert receipt.message_meta["display_title"] == "示例媒体标题"
    assert receipt.message_meta["delivery_claim"] is True
    assert receipt.message_meta["delivery"]["status"] == "sent"
    assert receipt.message_meta["delivery"]["parts"][0]["provider_message_id"] == (
        "group-video-process-key"
    )
    assert receipt.message_meta["delivery"]["parts"][0]["recall_status"] == "available"
    assert "delivery_claim" not in caption_row.message_meta
    stored_render = json.loads(json.loads(receipt.content)["result"])
    assert stored_render["message_id"] == str(receipt.id)
    assert stored_render["allow_download"] is False
    assert stored_render["title"] == "示例媒体标题"


async def test_send_media_caption_failure_is_preserved_on_replay(tmp_path, monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_group_caption-failure",
        is_group=True,
    )
    async with async_session() as db:
        db.add(ChannelConfig(
            agent_id=owner.id,
            channel_type="dingtalk",
            app_id=f"ding-caption-{uuid.uuid4().hex}",
            app_secret="ding-secret",
            is_configured=True,
        ))
        await db.commit()

    video = tmp_path / "caption.mp4"
    video.write_bytes(b"video")
    provider_calls = []

    async def fake_video(*_args, **_kwargs):
        provider_calls.append(True)
        return True, "MEDIA_SENT"

    async def fail_caption(**_kwargs):
        return IMDeliveryResult.failed("dingtalk", "caption_failed")

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.dingtalk_stream._send_dingtalk_native_video",
        fake_video,
    )
    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fail_caption)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)
    kwargs = {
        "agent_id": owner.id,
        "session_id": str(target.id),
        "file_path": video,
        "workspace_path": "workspace/caption.mp4",
        "media_kind": "video",
        "caption": "应当失败的说明",
        "cover_path": None,
        "intent_id": "caption-failure-call",
        "origin_session_id": str(uuid.uuid4()),
        "origin_turn_anchor_id": uuid.uuid4(),
    }

    first = json.loads(await agent_tools._send_media_to_session(**kwargs))
    replay = json.loads(await agent_tools._send_media_to_session(**kwargs))

    assert first["status"] == "sent"
    assert first["code"] == "MEDIA_SENT_CAPTION_FAILED"
    assert "说明文字发送失败" in first["message"]
    assert "do not resend the media" in first["agent_action"]
    assert replay["status"] == "already_sent"
    assert replay["code"] == "MEDIA_SENT_CAPTION_FAILED"
    assert replay["caption_status"] == "failed"
    assert replay["message"] == first["message"]
    assert provider_calls == [True]


async def test_managed_url_pending_receipt_converges_unknown_before_network_or_preflight(
    tmp_path,
    monkeypatch,
):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_p2p_pending-recovery",
        is_group=False,
        user_id=owner.creator_id,
    )
    origin_session_id = str(uuid.uuid4())
    intent_id = "managed-pending-recovery"
    operation_key = agent_tools._build_outbound_operation_key(
        agent_id=owner.id,
        origin_session_id=origin_session_id,
        tool_call_id=intent_id,
        origin_turn_anchor_id=None,
    )
    args = {
        "media_type": "audio",
        "url": "https://expired.example/signed.mp3?token=gone",
        "url_mode": "managed",
        "session_id": str(target.id),
    }
    async with async_session() as db:
        receipt = ChatMessage(
            agent_id=owner.id,
            user_id=target.user_id,
            role="tool_call",
            content=json.dumps({
                "name": "send_media",
                "call_id": intent_id,
                "args": args,
                "status": "running",
                "result": "",
            }),
            conversation_id=str(target.id),
            external_event_key=operation_key,
            message_meta={
                "delivery_status": "pending",
                "delivery_code": "MEDIA_DELIVERY_PENDING",
                "source_channel": "dingtalk",
                "media_kind": "audio",
                "source_mode": "managed_url",
            },
        )
        db.add(receipt)
        await db.commit()
        receipt_id = receipt.id

    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("pending recovery must not preflight, fetch, or call provider")

    live_events = []

    async def fake_live_mirror(*args, **_kwargs):
        live_events.append(args[-1])

    monkeypatch.setattr(agent_tools, "_preflight_managed_media_target", should_not_run)
    monkeypatch.setattr(agent_tools, "import_managed_media_url", should_not_run)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)

    result = json.loads(await agent_tools._send_channel_media(
        owner.id,
        tmp_path,
        args,
        media_kind="audio",
        tool_call_id=intent_id,
        origin_session_id=origin_session_id,
    ))

    assert result["status"] == "unknown"
    assert result["code"] == "MEDIA_DELIVERY_STATE_UNKNOWN"
    assert result["retryable"] is False
    assert "do not retry" in result["agent_action"]
    async with async_session() as db:
        stored = await db.get(ChatMessage, receipt_id)
    assert stored.message_meta["delivery_status"] == "unknown"
    stored_call = json.loads(stored.content)
    assert stored_call["status"] == "done"
    assert json.loads(stored_call["result"])["status"] == "unknown"
    assert live_events[-1]["status"] == "done"
    assert json.loads(live_events[-1]["result"])["status"] == "unknown"


async def test_live_media_delivery_holds_replay_until_one_sent_terminal(
    tmp_path,
    monkeypatch,
):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_group_live-lock",
        is_group=True,
    )
    async with async_session() as db:
        db.add(ChannelConfig(
            agent_id=owner.id,
            channel_type="dingtalk",
            app_id=f"ding-live-lock-{uuid.uuid4().hex}",
            app_secret="ding-secret",
            is_configured=True,
        ))
        await db.commit()

    video = tmp_path / "live-lock.mp4"
    video.write_bytes(b"video")
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()
    provider_calls = []
    live_events = []

    async def blocked_provider(*_args, **kwargs):
        lifecycle_connection = agent_tools._outbound_media_connection.get()
        assert lifecycle_connection is not None
        assert lifecycle_connection.in_transaction() is False
        provider_calls.append(True)
        provider_started.set()
        await release_provider.wait()
        await kwargs["on_result"]({"processQueryKey": "live-lock-process-key"})
        return True, "MEDIA_SENT"

    async def fake_live_mirror(*args, **_kwargs):
        live_events.append(args[-1])

    monkeypatch.setattr(
        "app.services.dingtalk_stream._send_dingtalk_native_video",
        blocked_provider,
    )
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)

    origin_session_id = str(uuid.uuid4())
    intent_id = "live-provider-lock"
    first_task = asyncio.create_task(agent_tools._send_media_to_session(
        agent_id=owner.id,
        session_id=str(target.id),
        file_path=video,
        workspace_path="workspace/live-lock.mp4",
        media_kind="video",
        caption="",
        cover_path=None,
        intent_id=intent_id,
        origin_session_id=origin_session_id,
        origin_turn_anchor_id=None,
    ))
    await asyncio.wait_for(provider_started.wait(), timeout=2)
    replay_tasks = [
        asyncio.create_task(agent_tools._replay_terminal_media_delivery(
            agent_id=owner.id,
            origin_session_id=origin_session_id,
            intent_id=intent_id,
            origin_turn_anchor_id=None,
        ))
        for _ in range(35)
    ]
    await asyncio.sleep(0.1)
    assert all(task.done() is False for task in replay_tasks)
    async with async_session() as db:
        ordinary_query = await asyncio.wait_for(db.execute(select(1)), timeout=0.5)
        assert ordinary_query.scalar_one() == 1

    release_provider.set()
    first = json.loads(await asyncio.wait_for(first_task, timeout=3))
    replays = await asyncio.wait_for(asyncio.gather(*replay_tasks), timeout=5)

    assert first["status"] == "sent"
    assert all(replay["status"] == "already_sent" for replay in replays)
    assert provider_calls == [True]
    assert len(live_events) == 1
    assert json.loads(live_events[0]["result"])["status"] == "sent"
    async with async_session() as db:
        receipt = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.agent_id == owner.id,
                    ChatMessage.external_event_key == agent_tools._build_outbound_operation_key(
                        agent_id=owner.id,
                        origin_session_id=origin_session_id,
                        tool_call_id=intent_id,
                        origin_turn_anchor_id=None,
                    ),
                )
            )
        ).scalar_one()
    assert receipt.message_meta["delivery_status"] == "sent"


async def test_media_lifecycle_reuses_one_connection_and_leaves_pool_capacity(
    tmp_path,
    monkeypatch,
):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_group_pool-two",
        is_group=True,
    )
    async with async_session() as db:
        db.add(ChannelConfig(
            agent_id=owner.id,
            channel_type="dingtalk",
            app_id=f"ding-pool-two-{uuid.uuid4().hex}",
            app_secret="ding-secret",
            is_configured=True,
        ))
        await db.commit()

    video = tmp_path / "pool-two.mp4"
    video.write_bytes(b"video")
    both_in_provider = asyncio.Event()
    release_provider = asyncio.Event()
    provider_count = 0

    async def blocked_provider(*_args, **kwargs):
        nonlocal provider_count
        lifecycle_connection = agent_tools._outbound_media_connection.get()
        assert lifecycle_connection is not None
        assert lifecycle_connection.in_transaction() is False
        provider_count += 1
        if provider_count == 2:
            both_in_provider.set()
        await release_provider.wait()
        await kwargs["on_result"](
            {"processQueryKey": f"pool-process-key-{provider_count}"}
        )
        return True, "MEDIA_SENT"

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    small_engine = create_async_engine(
        engine.url.render_as_string(hide_password=False),
        pool_size=3,
        max_overflow=0,
        pool_timeout=1,
    )
    monkeypatch.setattr(agent_tools, "engine", small_engine)
    monkeypatch.setattr(
        "app.services.dingtalk_stream._send_dingtalk_native_video",
        blocked_provider,
    )
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)

    async def deliver(intent_id):
        return json.loads(await agent_tools._send_media_to_session(
            agent_id=owner.id,
            session_id=str(target.id),
            file_path=video,
            workspace_path="workspace/pool-two.mp4",
            media_kind="video",
            caption="",
            cover_path=None,
            intent_id=intent_id,
            origin_session_id=str(target.id),
            origin_turn_anchor_id=None,
        ))

    tasks = [
        asyncio.create_task(deliver("pool-two-a")),
        asyncio.create_task(deliver("pool-two-b")),
    ]
    try:
        try:
            await asyncio.wait_for(both_in_provider.wait(), timeout=2)
        except TimeoutError:
            for task in tasks:
                if task.done() and not task.cancelled() and task.exception() is not None:
                    raise task.exception()
            raise
        async with small_engine.connect() as ordinary_connection:
            ordinary_result = await asyncio.wait_for(
                ordinary_connection.execute(text("SELECT 1")),
                timeout=0.5,
            )
            assert ordinary_result.scalar_one() == 1
        release_provider.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
    finally:
        release_provider.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await small_engine.dispose()

    assert [result["status"] for result in results] == ["sent", "sent"]
    assert provider_count == 2


async def test_media_lifecycle_releases_lock_when_setup_fails_after_acquire(monkeypatch):
    operation_key = f"lock-cleanup-{uuid.uuid4()}"
    lock_id = agent_tools._outbound_operation_lock_id(operation_key)

    class FailingContext:
        def set(self, _connection):
            raise RuntimeError("injected failure after lock commit")

        def reset(self, _token):
            raise AssertionError("no token was created")

        def get(self):
            return None

    monkeypatch.setattr(agent_tools, "_outbound_media_connection", FailingContext())

    with pytest.raises(RuntimeError, match="injected failure"):
        async with agent_tools._outbound_operation_lifecycle_lock(operation_key):
            raise AssertionError("setup failure must happen before yield")

    async with engine.connect() as connection:
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": lock_id},
            )
        ).scalar_one()
        assert acquired is True
        await connection.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": lock_id},
        )
        await connection.commit()


async def test_send_media_reuses_current_running_tool_row_and_orders_caption_after_it(tmp_path, monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id, channel="web", is_group=False, user_id=owner.creator_id
    )
    video = tmp_path / "current.mp4"
    video.write_bytes(b"video")
    anchor_id = uuid.uuid4()
    call_id = "current-media-call"

    async with async_session() as db:
        running = ChatMessage(
            agent_id=owner.id,
            user_id=target.user_id,
            role="tool_call",
            content=json.dumps({
                "name": "send_media",
                "call_id": call_id,
                "args": {"media_type": "video", "file_path": "workspace/current.mp4"},
                "status": "running",
                "result": "",
            }),
            conversation_id=str(target.id),
            message_meta={"turn_anchor_id": str(anchor_id)},
        )
        db.add(running)
        await db.commit()
        running_id = running.id

    live_events = []

    async def fake_live(*args, **_kwargs):
        live_events.append(args[-1])

    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live)
    payload = json.loads(await agent_tools._send_media_to_session(
        agent_id=owner.id,
        session_id=str(target.id),
        file_path=video,
        workspace_path="workspace/current.mp4",
        media_kind="video",
        caption="卡片后的说明",
        cover_path=None,
        intent_id=call_id,
        origin_session_id=str(target.id),
        origin_turn_anchor_id=anchor_id,
    ))

    assert payload["status"] == "sent"
    assert payload["message_id"] == str(running_id)
    assert [event["type"] for event in live_events] == [
        "tool_call", "assistant_message_committed",
    ]
    async with async_session() as db:
        rows = list((await db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(target.id),
                ChatMessage.external_event_key.is_not(None),
            ).order_by(ChatMessage.created_at, ChatMessage.id)
        )).scalars().all())
    assert [row.role for row in rows] == ["tool_call", "assistant"]
    assert rows[0].id == running_id
    assert json.loads(rows[0].content)["status"] == "done"
    assert rows[0].message_meta["delivery_claim"] is False
    assert rows[1].message_meta["media_caption_for"] == str(running_id)
    assert "delivery_claim" not in rows[1].message_meta
    assert rows[0].created_at <= rows[1].created_at


async def test_failed_media_claim_finishes_with_explicit_error_instead_of_running(tmp_path, monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_group_failed-media",
        is_group=True,
    )
    async with async_session() as db:
        db.add(ChannelConfig(
            agent_id=owner.id,
            channel_type="dingtalk",
            app_id=f"ding-failed-{uuid.uuid4().hex}",
            app_secret="ding-secret",
            is_configured=True,
        ))
        await db.commit()
    video = tmp_path / "failed.mp4"
    video.write_bytes(b"video")

    async def fail_video(*_args, **_kwargs):
        return False, "MEDIA_SEND_FAILED"

    live_events = []

    async def fake_live(*args, **_kwargs):
        live_events.append(args[-1])

    monkeypatch.setattr("app.services.dingtalk_stream._send_dingtalk_native_video", fail_video)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live)
    payload = json.loads(await agent_tools._send_media_to_session(
        agent_id=owner.id,
        session_id=str(target.id),
        file_path=video,
        workspace_path="workspace/failed.mp4",
        media_kind="video",
        caption="",
        cover_path=None,
        intent_id="failed-media-call",
        origin_session_id=str(uuid.uuid4()),
        origin_turn_anchor_id=uuid.uuid4(),
    ))

    assert payload["status"] == "failed"
    assert payload["code"] == "MEDIA_SEND_FAILED"
    assert "发送" in payload["message"]
    assert len(live_events) == 1
    assert live_events[0]["type"] == "tool_call"
    live_result = json.loads(live_events[0]["result"])
    assert live_result == payload
    async with async_session() as db:
        row = (await db.execute(select(ChatMessage).where(
            ChatMessage.message_meta["tool_call_id"].as_string() == "failed-media-call"
        ))).scalar_one()
    stored = json.loads(row.content)
    assert stored["status"] == "done"
    stored_result = json.loads(stored["result"])
    assert stored_result["status"] == "failed"
    assert stored_result["message"] == payload["message"]


async def test_send_media_pending_claim_replay_never_calls_provider(tmp_path, monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_group_pending-media",
        is_group=True,
    )
    origin_session_id = str(uuid.uuid4())
    turn_anchor_id = uuid.uuid4()
    intent_id = "media-crash-window-call"
    operation_key = agent_tools._build_outbound_operation_key(
        agent_id=owner.id,
        origin_session_id=origin_session_id,
        tool_call_id=intent_id,
        origin_turn_anchor_id=turn_anchor_id,
    )
    async with async_session() as db:
        db.add(ChatMessage(
            agent_id=owner.id,
            role="assistant",
            content="",
            conversation_id=str(target.id),
            external_event_key=operation_key,
            message_meta={
                "source_channel": "dingtalk",
                "delivery_status": "pending",
                "delivery_code": "MEDIA_DELIVERY_PENDING",
                "attachments": [{
                    "display_name": "demo.mp4",
                    "path": "workspace/demo.mp4",
                    "kind": "video",
                    "mime_type": "video/mp4",
                }],
            },
        ))
        await db.commit()

    video = tmp_path / "demo.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42hdlr\x00\x00\x00\x00\x00\x00\x00\x00vide")
    provider_calls = []

    async def should_not_send(*_args, **_kwargs):
        provider_calls.append(True)
        return True, "MEDIA_SENT"

    monkeypatch.setattr(
        "app.services.dingtalk_stream._send_dingtalk_native_video",
        should_not_send,
    )
    payload = json.loads(await agent_tools._send_media_to_session(
        agent_id=owner.id,
        session_id=str(target.id),
        file_path=video,
        workspace_path="workspace/demo.mp4",
        media_kind="video",
        caption="",
        cover_path=None,
        intent_id=intent_id,
        origin_session_id=origin_session_id,
        origin_turn_anchor_id=turn_anchor_id,
    ))

    assert payload["status"] == "unknown"
    assert payload["code"] == "MEDIA_DELIVERY_STATE_UNKNOWN"
    assert provider_calls == []
    async with async_session() as db:
        receipt = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.external_event_key == operation_key)
            )
        ).scalar_one()
    assert receipt.message_meta["delivery_status"] == "unknown"


async def test_send_channel_media_sanitizes_native_visible_fields(tmp_path, monkeypatch):
    forbidden = "cla" + "with"
    media = tmp_path / "demo.mp4"
    media.write_bytes(b"video")
    captured = {}

    async def no_tool_config(*_args, **_kwargs):
        return {}

    async def capture_native(**kwargs):
        captured.update(kwargs)
        return json.dumps({"status": "sent"})

    monkeypatch.setattr(agent_tools, "_get_tool_config", no_tool_config)
    monkeypatch.setattr(agent_tools, "_sniff_media_file_mime", lambda _path: "video/mp4")
    monkeypatch.setattr(agent_tools, "_send_media_to_session", capture_native)

    await agent_tools._send_channel_media(
        uuid.uuid4(),
        tmp_path,
        {
            "media_type": "video",
            "file_path": "demo.mp4",
            "session_id": str(uuid.uuid4()),
            "message": f"caption {forbidden.upper()}",
            "title": f"title {forbidden}",
        },
        media_kind="video",
        tool_call_id="sanitized-native-media",
    )

    assert forbidden not in captured["caption"].lower()
    assert forbidden not in str(captured["tool_args"]).lower()


async def test_send_channel_media_sanitizes_platform_visible_fields(monkeypatch):
    forbidden = "cla" + "with"
    captured = {}

    async def allow_url(url, **_kwargs):
        return url

    async def no_tool_config(*_args, **_kwargs):
        return {}

    async def capture_platform(**kwargs):
        captured.update(kwargs)
        return json.dumps({"status": "sent"})

    monkeypatch.setattr(agent_tools, "validate_media_url", allow_url)
    monkeypatch.setattr(agent_tools, "_get_tool_config", no_tool_config)
    monkeypatch.setattr(agent_tools, "_publish_external_media_to_session", capture_platform)

    await agent_tools._send_channel_media(
        uuid.uuid4(),
        ws=agent_tools.WORKSPACE_ROOT,
        arguments={
            "media_type": "video",
            "url": "https://media.example/demo.mp4",
            "url_mode": "external",
            "session_id": str(uuid.uuid4()),
            "message": f"caption {forbidden}",
            "title": f"title {forbidden.upper()}",
        },
        media_kind="video",
        tool_call_id="sanitized-platform-media",
    )

    assert forbidden not in captured["caption"].lower()
    assert forbidden not in str(captured["tool_args"]).lower()


async def test_send_channel_file_terminal_receipt_replay_never_calls_provider(
    tmp_path, monkeypatch
):
    owner, _ = await _seed_agents()
    origin = await _seed_session(owner.id, channel="web", is_group=True)
    call_id = "file-terminal-replay"
    turn_anchor_id = uuid.uuid4()
    async with async_session() as db:
        row = ChatMessage(
            agent_id=owner.id,
            user_id=origin.user_id,
            role="tool_call",
            content=json.dumps({
                "name": "send_channel_file",
                "call_id": call_id,
                "args": {"file_path": "workspace/report.pdf"},
                "status": "running",
                "result": "",
            }),
            conversation_id=str(origin.id),
            message_meta={"turn_anchor_id": str(turn_anchor_id)},
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    claimed = await agent_tools._claim_channel_file_receipt(
        agent_id=owner.id,
        tool_call_id=call_id,
        origin_session_id=str(origin.id),
        origin_turn_anchor_id=turn_anchor_id,
    )
    assert claimed == row_id
    assert await agent_tools.register_delivery(
        row_id,
        IMDeliveryResult.unsupported_delivery("slack", "slack_file"),
    )

    workspace = tmp_path / str(owner.id)
    report = workspace / "workspace" / "report.pdf"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    provider_calls = []

    async def should_not_send(*_args, **_kwargs):
        provider_calls.append(True)
        return IMDeliveryResult.unsupported_delivery("slack", "slack_file")

    token = agent_tools.channel_file_sender.set(should_not_send)
    try:
        result = await agent_tools._send_channel_file(
            owner.id,
            workspace,
            {"file_path": "workspace/report.pdf"},
            tool_call_id=call_id,
            origin_session_id=str(origin.id),
            origin_turn_anchor_id=turn_anchor_id,
        )
    finally:
        agent_tools.channel_file_sender.reset(token)

    assert "receipt unavailable" in result
    assert provider_calls == []
    async with async_session() as db:
        stored = await db.get(ChatMessage, row_id)
    assert stored.message_meta["delivery"]["status"] == "sent"


async def test_send_channel_file_persists_first_part_before_later_timeout(
    tmp_path, monkeypatch
):
    owner, _ = await _seed_agents()
    origin = await _seed_session(owner.id, channel="web", is_group=True)
    call_id = "file-partial-timeout"
    turn_anchor_id = uuid.uuid4()
    async with async_session() as db:
        row = ChatMessage(
            agent_id=owner.id,
            user_id=origin.user_id,
            role="tool_call",
            content=json.dumps({
                "name": "send_channel_file",
                "call_id": call_id,
                "args": {"file_path": "workspace/report.pdf"},
                "status": "running",
                "result": "",
            }),
            conversation_id=str(origin.id),
            message_meta={"turn_anchor_id": str(turn_anchor_id)},
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    workspace = tmp_path / str(owner.id)
    report = workspace / "workspace" / "report.pdf"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    async def partial_sender(*_args, **_kwargs):
        await agent_tools.record_channel_file_part(IMDeliveryPart(
            transport="dingtalk_openapi_oto",
            provider_message_id="first-provider-part",
            conversation_ref="staff-1",
            artifact_role="channel_file",
        ))
        raise TimeoutError("second part timed out")

    token = agent_tools.channel_file_sender.set(partial_sender)
    try:
        result = await agent_tools._send_channel_file(
            owner.id,
            workspace,
            {"file_path": "workspace/report.pdf"},
            tool_call_id=call_id,
            origin_session_id=str(origin.id),
            origin_turn_anchor_id=turn_anchor_id,
        )
    finally:
        agent_tools.channel_file_sender.reset(token)

    assert "Failed to send file" in result
    async with async_session() as db:
        stored = await db.get(ChatMessage, row_id)
    delivery = stored.message_meta["delivery"]
    assert delivery["status"] == "partial"
    assert delivery["uncertain"] is True
    assert [part["provider_message_id"] for part in delivery["parts"]] == [
        "first-provider-part"
    ]


async def test_send_media_pending_standard_tool_call_replay_becomes_visible_unknown_error(
    tmp_path, monkeypatch
):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_group_pending-standard-media",
        is_group=True,
    )
    origin_session_id = str(uuid.uuid4())
    turn_anchor_id = uuid.uuid4()
    intent_id = "media-standard-crash-window-call"
    operation_key = agent_tools._build_outbound_operation_key(
        agent_id=owner.id,
        origin_session_id=origin_session_id,
        tool_call_id=intent_id,
        origin_turn_anchor_id=turn_anchor_id,
    )
    async with async_session() as db:
        pending = ChatMessage(
            agent_id=owner.id,
            role="tool_call",
            content=json.dumps({
                "name": "send_media",
                "call_id": intent_id,
                "args": {"media_type": "video", "file_path": "workspace/demo.mp4"},
                "status": "running",
                "result": "",
            }),
            conversation_id=str(target.id),
            external_event_key=operation_key,
            message_meta={
                "source_channel": "dingtalk",
                "delivery_claim": True,
                "delivery_status": "pending",
                "delivery_code": "MEDIA_DELIVERY_PENDING",
            },
        )
        db.add(pending)
        await db.commit()
        pending_id = pending.id

    video = tmp_path / "demo.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42hdlr\x00\x00\x00\x00\x00\x00\x00\x00vide")
    provider_calls = []

    async def should_not_send(*_args, **_kwargs):
        provider_calls.append(True)
        return True, "MEDIA_SENT"

    monkeypatch.setattr(
        "app.services.dingtalk_stream._send_dingtalk_native_video",
        should_not_send,
    )
    payload = json.loads(await agent_tools._send_media_to_session(
        agent_id=owner.id,
        session_id=str(target.id),
        file_path=video,
        workspace_path="workspace/demo.mp4",
        media_kind="video",
        caption="",
        cover_path=None,
        intent_id=intent_id,
        origin_session_id=origin_session_id,
        origin_turn_anchor_id=turn_anchor_id,
    ))

    assert payload["status"] == "unknown"
    assert payload["message_id"] == str(pending_id)
    assert payload["retryable"] is False
    assert "不要自动重试" in payload["message"]
    assert provider_calls == []
    async with async_session() as db:
        receipt = await db.get(ChatMessage, pending_id)
    stored = json.loads(receipt.content)
    assert stored["status"] == "done"
    stored_result = json.loads(stored["result"])
    assert stored_result == payload


async def test_send_media_rejects_group_flag_and_route_prefix_mismatch(tmp_path):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_p2p_wrong-for-group",
        is_group=True,
    )
    async with async_session() as db:
        db.add(ChannelConfig(
            agent_id=owner.id,
            channel_type="dingtalk",
            app_id=f"ding-route-{uuid.uuid4().hex}",
            app_secret="ding-secret",
            is_configured=True,
        ))
        await db.commit()
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42hdlr\x00\x00\x00\x00\x00\x00\x00\x00vide")

    payload = json.loads(await agent_tools._send_media_to_session(
        agent_id=owner.id,
        session_id=str(target.id),
        file_path=video,
        workspace_path="workspace/demo.mp4",
        media_kind="video",
        caption="",
        cover_path=None,
        intent_id="route-mismatch-call",
        origin_session_id=None,
        origin_turn_anchor_id=None,
    ))

    assert payload["status"] == "failed"
    assert payload["code"] == "SESSION_ROUTE_MISMATCH"


async def test_external_media_url_reuses_standard_current_tool_call(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        channel="web",
        external_conv_id="platform-media-url",
        is_group=False,
        user_id=owner.creator_id,
    )
    anchor_id = uuid.uuid4()
    call_id = "external-media-call"
    tool_args = {
        "media_type": "video",
        "url": "https://media.example/demo.mp4?token=short-lived",
        "url_mode": "external",
        "title": "  外部媒体\n标题  ",
    }
    async with async_session() as db:
        running = ChatMessage(
            agent_id=owner.id,
            user_id=target.user_id,
            role="tool_call",
            content=json.dumps({
                "name": "send_media",
                "call_id": call_id,
                "args": tool_args,
                "status": "running",
                "result": "",
            }),
            conversation_id=str(target.id),
            message_meta={"turn_anchor_id": str(anchor_id)},
        )
        db.add(running)
        await db.commit()
        running_id = running.id

    live_events = []

    async def fake_live(*args, **_kwargs):
        live_events.append(args[-1])

    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live)
    kwargs = {
        "agent_id": owner.id,
        "session_id": str(target.id),
        "media_url": tool_args["url"],
        "media_kind": "video",
        "caption": "",
        "intent_id": call_id,
        "origin_session_id": str(target.id),
        "origin_turn_anchor_id": anchor_id,
        "allow_download": False,
        "tool_args": tool_args,
    }
    first = json.loads(await agent_tools._publish_external_media_to_session(**kwargs))
    replay = json.loads(await agent_tools._publish_external_media_to_session(**kwargs))

    assert first["status"] == "sent"
    assert first["source_mode"] == "external_url"
    assert first["url"] == tool_args["url"]
    assert first["title"] == "外部媒体 标题"
    assert "path" not in first
    assert replay["status"] == "already_sent"
    assert replay["title"] == "外部媒体 标题"
    assert len(live_events) == 1
    async with async_session() as db:
        stored = await db.get(ChatMessage, running_id)
    assert stored is not None
    assert stored.message_meta["attachments"] == []
    assert stored.message_meta["delivery_status"] == "sent"
    assert stored.message_meta["display_title"] == "外部媒体 标题"
    stored_call = json.loads(stored.content)
    assert stored_call["status"] == "done"
    assert stored_call["args"] == tool_args


async def test_outbound_operation_key_supports_sessionless_agent_turns():
    key = agent_tools._build_outbound_operation_key(
        agent_id=uuid.uuid4(),
        origin_session_id=None,
        tool_call_id="heartbeat-tool-call",
    )

    assert key is not None
    assert ":no-session:unanchored:heartbeat-tool-call" in key


async def test_dingtalk_runtime_uses_temporary_webhook_for_native_mentions(monkeypatch):
    from app.services.dingtalk_group_mentions import cache_group_session_webhook

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
        await cache_group_session_webhook(
            db,
            agent_id=owner.id,
            external_conv_id=target.external_conv_id,
            webhook="https://oapi.dingtalk.com/robot/sendBySession?secret",
            expires_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000) + 600_000,
        )
        await db.commit()
    captured = {}

    async def fake_mention(**kwargs):
        captured.update(kwargs)
        return {"errcode": 0}

    async def fail_proactive(**_kwargs):
        raise AssertionError("native mention must use the temporary session webhook")

    monkeypatch.setattr(turn_runtime, "_send_dingtalk_group_mention", fake_mention)
    monkeypatch.setattr(turn_runtime, "_send_dingtalk_group_markdown", fail_proactive)
    sent = await turn_runtime.deliver_message_to_runtime(
        agent_id=owner.id,
        runtime=TurnRuntime(
            session_found=True,
            source_channel="dingtalk",
            conversation_id=str(target.id),
            external_conv_id=target.external_conv_id,
            is_group=True,
        ),
        message="请确认",
        mention=MentionIntent(scope="users", target_ids=("staff-zhangsan",)),
    )

    assert sent is True
    assert captured == {
        "session_webhook": "https://oapi.dingtalk.com/robot/sendBySession?secret",
        "message": "请确认",
        "mention": MentionIntent(scope="users", target_ids=("staff-zhangsan",)),
    }


async def test_dingtalk_runtime_rejects_mention_webhook_owned_by_another_agent(
    monkeypatch,
):
    from app.services.dingtalk_group_mentions import cache_group_session_webhook

    owner, other = await _seed_agents()
    target = await _seed_session(owner.id)
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=other.id,
                channel_type="dingtalk",
                app_id=f"ding-app-{uuid.uuid4().hex}",
                app_secret="ding-secret",
                is_configured=True,
            )
        )
        await cache_group_session_webhook(
            db,
            agent_id=owner.id,
            external_conv_id=target.external_conv_id,
            webhook="https://oapi.dingtalk.com/robot/sendBySession?owned-by-owner",
            expires_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000) + 600_000,
        )
        await db.commit()

    provider_calls: list[str] = []

    async def fail_native(**_kwargs):
        provider_calls.append("native")
        return {"errcode": 0}

    async def fail_proactive(**_kwargs):
        provider_calls.append("proactive")
        return {"errcode": 0}

    monkeypatch.setattr(turn_runtime, "_send_dingtalk_group_mention", fail_native)
    monkeypatch.setattr(turn_runtime, "_send_dingtalk_group_markdown", fail_proactive)
    result = await turn_runtime.deliver_message_with_receipt(
        agent_id=other.id,
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
    assert result.error == "dingtalk_session_webhook_unavailable"
    assert provider_calls == []


async def test_dingtalk_runtime_rejects_changed_group_conversation_generation(
    monkeypatch,
):
    from app.services.dingtalk_group_mentions import cache_group_session_webhook

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
        await cache_group_session_webhook(
            db,
            agent_id=owner.id,
            external_conv_id=target.external_conv_id,
            webhook="https://oapi.dingtalk.com/robot/sendBySession?old-generation",
            expires_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000) + 600_000,
        )
        await db.commit()

    provider_calls: list[str] = []

    async def fail_native(**_kwargs):
        provider_calls.append("native")
        return {"errcode": 0}

    async def fail_proactive(**_kwargs):
        provider_calls.append("proactive")
        return {"errcode": 0}

    monkeypatch.setattr(turn_runtime, "_send_dingtalk_group_mention", fail_native)
    monkeypatch.setattr(turn_runtime, "_send_dingtalk_group_markdown", fail_proactive)
    result = await turn_runtime.deliver_message_with_receipt(
        agent_id=owner.id,
        runtime=TurnRuntime(
            session_found=True,
            source_channel="dingtalk",
            conversation_id=str(target.id),
            external_conv_id="dingtalk_group_new-generation",
            is_group=True,
        ),
        message="不应发送",
        mention=MentionIntent(scope="all"),
    )

    assert result.ok is False
    assert result.error == "dingtalk_session_webhook_unavailable"
    assert provider_calls == []


async def test_dingtalk_mention_webhook_safety_window_finishes_receipt_failed(
    monkeypatch,
):
    from app.services.dingtalk_group_mentions import cache_group_session_webhook

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
        await cache_group_session_webhook(
            db,
            agent_id=owner.id,
            external_conv_id=target.external_conv_id,
            webhook="https://oapi.dingtalk.com/robot/sendBySession?near-expiry",
            expires_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000) + 20_000,
        )
        await db.commit()

    provider_calls: list[str] = []

    async def fail_native(**_kwargs):
        provider_calls.append("native")
        return {"errcode": 0}

    async def fail_proactive(**_kwargs):
        provider_calls.append("proactive")
        return {"errcode": 0}

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        agent_tools,
        "load_group_session_webhook",
        lambda _session: "valid-during-preflight",
    )
    monkeypatch.setattr(turn_runtime, "_send_dingtalk_group_mention", fail_native)
    monkeypatch.setattr(turn_runtime, "_send_dingtalk_group_markdown", fail_proactive)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)
    result = await agent_tools._send_group_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "不应发送",
            "mention_all": True,
        },
        origin_session_id=str(uuid.uuid4()),
        tool_call_id="mention-expired-after-preflight",
        origin_turn_anchor_id=uuid.uuid4(),
    )

    assert result.startswith("❌ Group message delivery failed via dingtalk")
    assert provider_calls == []
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
        == "dingtalk_session_webhook_unavailable"
    )


async def test_dingtalk_group_mention_payload_contains_native_at_metadata(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            return {"errcode": 0}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(
        turn_runtime.httpx,
        "AsyncClient",
        lambda **_kwargs: FakeClient(),
    )

    result = await turn_runtime._send_dingtalk_group_mention(
        session_webhook="https://oapi.dingtalk.com/robot/sendBySession?secret",
        message="请确认",
        mention=MentionIntent(scope="users", target_ids=("staff-zhangsan",)),
    )

    assert result == {"errcode": 0}
    assert captured["url"].endswith("sendBySession?secret")
    assert captured["json"] == {
        "msgtype": "text",
        "text": {"content": "请确认"},
        "at": {"atUserIds": ["staff-zhangsan"], "isAtAll": False},
    }


async def test_dingtalk_group_mention_all_payload_contains_native_at_metadata(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            return {"errcode": 0}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(
        turn_runtime.httpx,
        "AsyncClient",
        lambda **_kwargs: FakeClient(),
    )

    result = await turn_runtime._send_dingtalk_group_mention(
        session_webhook="https://oapi.dingtalk.com/robot/sendBySession?secret",
        message="今晚十点发布",
        mention=MentionIntent(scope="all"),
    )

    assert result == {"errcode": 0}
    assert captured["json"] == {
        "msgtype": "text",
        "text": {"content": "今晚十点发布"},
        "at": {"isAtAll": True},
    }


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
                    Tool.name.in_({"send_group_session_message", "recall_message"})
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
    assert assigned_names == {"send_group_session_message", "recall_message"}

    tools = await agent_tools.get_agent_tools_for_llm(owner.id)
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
