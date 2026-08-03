"""Exact-Session group message delivery contract."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

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


async def test_send_group_session_message_uses_exact_binding_and_persists_receipt(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    delivered: list[dict] = []

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return True

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_tools, "deliver_message_to_runtime", fake_deliver)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)

    result = await agent_tools._send_group_session_message(
        owner.id,
        {"session_id": str(target.id), "message": "版本今晚发布"},
        origin_session_id=str(uuid.uuid4()),
        tool_call_id="call-group-1",
        origin_turn_anchor_id=uuid.uuid4(),
    )

    payload = json.loads(result)
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
    assert delivered[0]["require_transport"] is True
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
        return True

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_tools, "deliver_message_to_runtime", fake_deliver)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)

    result = await agent_tools._send_session_message(
        owner.id,
        {"session_id": str(target.id), "message": "按原会话投递"},
        origin_session_id=str(uuid.uuid4()),
        tool_call_id=f"call-{channel}-person",
        origin_turn_anchor_id=uuid.uuid4(),
    )

    assert json.loads(result) == {
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

    monkeypatch.setattr(agent_tools, "deliver_message_to_runtime", fail_if_delivered)
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

    monkeypatch.setattr(agent_tools, "deliver_message_to_runtime", fail_if_delivered)

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
        return True

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_tools, "deliver_message_to_runtime", fake_deliver)
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


async def test_failed_transport_does_not_persist_target_receipt(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)

    async def fake_failure(**_kwargs):
        return False

    monkeypatch.setattr(agent_tools, "deliver_message_to_runtime", fake_failure)
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
    assert receipts == []


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
        return True

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_tools, "resolve_human_channel_recipient", fake_resolve)
    monkeypatch.setattr(agent_tools, "deliver_message_to_runtime", fake_deliver)
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
    assert delivered[0]["message"] == "@张三\n请确认今晚发布窗口"
    assert delivered[0]["dingtalk_at_user_ids"] == ["staff-zhangsan"]
    assert delivered[0]["dingtalk_session_webhook"] == webhook

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

    monkeypatch.setattr(agent_tools, "deliver_message_to_runtime", fail_if_called)
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


async def test_dingtalk_runtime_uses_temporary_webhook_for_native_mentions(monkeypatch):
    owner, _ = await _seed_agents()
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
            conversation_id=str(uuid.uuid4()),
            external_conv_id="dingtalk_group_open-conversation-exact",
            is_group=True,
        ),
        message="@张三\n请确认",
        dingtalk_at_user_ids=["staff-zhangsan"],
        dingtalk_session_webhook="https://oapi.dingtalk.com/robot/sendBySession?secret",
    )

    assert sent is True
    assert captured == {
        "session_webhook": "https://oapi.dingtalk.com/robot/sendBySession?secret",
        "message": "@张三\n请确认",
        "at_user_ids": ["staff-zhangsan"],
    }


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
        message="@张三\n请确认",
        at_user_ids=["staff-zhangsan"],
    )

    assert result == {"errcode": 0}
    assert captured["url"].endswith("sendBySession?secret")
    assert captured["json"] == {
        "msgtype": "text",
        "text": {"content": "@张三\n请确认"},
        "at": {"atUserIds": ["staff-zhangsan"], "isAtAll": False},
    }


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
        existing_tool_id = (
            await db.execute(select(Tool.id).where(Tool.name == "send_group_session_message"))
        ).scalar_one_or_none()
        if existing_tool_id is not None:
            await db.execute(delete(AgentTool).where(AgentTool.tool_id == existing_tool_id))
            await db.execute(delete(Tool).where(Tool.id == existing_tool_id))
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
        assignment = (
            await db.execute(
                select(AgentTool)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(
                    AgentTool.agent_id == owner.id,
                    AgentTool.enabled.is_(True),
                    Tool.name == "send_group_session_message",
                )
            )
        ).scalar_one_or_none()
    assert assignment is not None

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
                "description": "Text content to send to the bound group.",
            },
            "mention_user_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
                "description": (
                    "Optional canonical platform user_ids to @ in a DingTalk group. "
                    "Each person must have an active DingTalk route; DingTalk only renders "
                    "the @ for people who are members of the target group."
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
                "description": "Text content to send to the bound conversation.",
            },
            "mention_user_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
                "description": (
                    "Optional canonical platform user_ids to @ in a DingTalk group. "
                    "Each person must have an active DingTalk route; DingTalk only renders "
                    "the @ for people who are members of the target group."
                ),
            },
        },
        "required": ["session_id", "message"],
        "additionalProperties": False,
    }
