"""Exact-Session group message delivery contract."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.mcp_server import MCPServer  # noqa: F401 - register Tool FK target
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
) -> ChatSession:
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=None,
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


async def test_dingtalk_runtime_delivers_to_exact_group_conversation(monkeypatch):
    owner, _ = await _seed_agents()
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id="ding-app",
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
        "app_id": "ding-app",
        "app_secret": "ding-secret",
        "open_conversation_id": "open-conversation-exact",
        "message": "exact target",
    }


async def test_unconfigured_channel_is_not_used(monkeypatch):
    owner, _ = await _seed_agents()
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id="ding-app",
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
                app_id="ding-app",
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
        },
        "required": ["session_id", "message"],
        "additionalProperties": False,
    }
