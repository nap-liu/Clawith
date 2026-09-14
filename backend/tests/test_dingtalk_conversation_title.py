"""DingTalk titles must survive the platform ingress and persisted session."""

import asyncio
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import dingtalk_stream
import pytest
from sqlalchemy import select

from app.api import dingtalk as dingtalk_api
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services import dingtalk_stream as stream_service

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.mark.parametrize(
    ("conversation_type", "conversation_title", "expected_group_name"),
    [
        ("2", "  产品讨论组  ", "产品讨论组"),
        ("2", "   ", "DingTalk Group cidnBH1dM4ab"),
        ("2", "", "DingTalk Group cidnBH1dM4ab"),
        ("1", "Ignored P2P title", None),
    ],
)
async def test_conversation_title_persisted_by_message_entry(
    monkeypatch, conversation_type, conversation_title, expected_group_name,
):
    suffix = uuid.uuid4().hex
    staff_id = f"staff_{suffix}"
    async with async_session() as db:
        tenant = Tenant(name="Title test", slug=f"title_{suffix}")
        identity = Identity(username=f"title_{suffix}", password_hash="unused")
        db.add_all([tenant, identity])
        await db.flush()
        user = User(
            tenant_id=tenant.id, identity_id=identity.id, display_name="Title Tester",
            role="member", is_active=True,
        )
        provider = IdentityProvider(
            tenant_id=tenant.id, name="DingTalk", provider_type="dingtalk",
            is_active=True, config={"app_key": f"title-{suffix}", "app_secret": "test-secret"},
        )
        db.add_all([user, provider])
        await db.flush()
        agent = Agent(name="Title Agent", creator_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.flush()
        db.add(ChannelConfig(agent_id=agent.id, channel_type="dingtalk", app_id=f"title-{suffix}", is_configured=True))
        db.add(OrgMember(
            tenant_id=tenant.id, provider_id=provider.id, external_id=staff_id,
            name=user.display_name, status="active", user_id=user.id,
        ))
        await db.commit()
        agent_id = agent.id

    monkeypatch.setattr(
        dingtalk_api, "_get_dingtalk_user_detail_with_fallback",
        AsyncMock(return_value={"name": "Title Tester"}),
    )
    llm = AsyncMock(return_value="")
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", llm)
    await dingtalk_api.process_dingtalk_message(
        agent_id=agent_id, sender_staff_id=staff_id, user_text="Hello from DingTalk",
        conversation_id="cidnBH1dM4abcdef", conversation_type=conversation_type,
        conversation_title=conversation_title, message_id=f"message_{suffix}",
        sender_nick="Title Tester",
    )

    async with async_session() as db:
        session = (await db.execute(
            select(ChatSession).where(ChatSession.agent_id == agent_id)
        )).scalar_one()
        assert session.is_group is (conversation_type == "2")
        assert session.group_name == expected_group_name
        assert session.title == (expected_group_name or "Hello from DingTalk")
    llm.assert_awaited_once()


@pytest.mark.parametrize("allowed", [True, False])
async def test_stream_handler_passes_conversation_title_to_platform_entry(monkeypatch, allowed):
    """Exercise the registered platform callback, including its scheduled work."""
    manager = stream_service.DingTalkStreamManager()
    manager._main_loop = asyncio.get_running_loop()
    entry = AsyncMock()
    scheduled = []
    callbacks = []
    admission = AsyncMock(return_value=allowed)
    monkeypatch.setattr("app.services.dingtalk_stream_runner.group_ingress_allowed", admission)
    monkeypatch.setattr(dingtalk_api, "process_dingtalk_message", entry)
    monkeypatch.setattr(dingtalk_api, "_check_message_dedup", AsyncMock(return_value=False))
    monkeypatch.setattr(stream_service, "_parse_dingtalk_quoted_message", AsyncMock(return_value=None))
    monkeypatch.setattr(stream_service, "_make_dingtalk_reactions", lambda *_: None)

    async def run_work(_key, *, work, **_kwargs):
        return await work()

    monkeypatch.setattr(stream_service, "run_channel_message", run_work)
    monkeypatch.setattr(stream_service, "_fire_and_forget", lambda _loop, work: scheduled.append(work))
    monkeypatch.setattr(manager, "_handle_runner_exit", AsyncMock())

    async def receive_callback(**kwargs):
        client = kwargs["client"]
        handler = client.callback_handler_map[dingtalk_stream.ChatbotMessage.TOPIC]
        callbacks.append(await handler.process(SimpleNamespace(data={
            "senderStaffId": "staff_title", "conversationId": "cid_title",
            "conversationType": "2", "conversationTitle": "  Real Group Name  ",
            "senderNick": "Alice", "msgId": "title_message", "msgtype": "text",
            "text": {"content": "hi"},
        })))

    monkeypatch.setattr(manager, "_run_managed_client", receive_callback)
    await asyncio.to_thread(
        manager._run_client_thread, uuid.uuid4(), "test-key", "test-secret",
        threading.Event(), 1, "test-fingerprint",
    )
    assert callbacks == [(dingtalk_stream.AckMessage.STATUS_OK, "ok")]
    assert len(scheduled) == 1
    await scheduled[0]
    assert admission.await_args.args[1:] == ("dingtalk", "cid_title")
    assert admission.await_args.kwargs == {"is_group": True, "name": "Real Group Name",
                                          "sender_id": "staff_title", "sender_name": "Alice", "sender_type": "staff_id",
                                          "sender_info": {"staff_id": "staff_title"}}
    if not allowed:
        entry.assert_not_awaited()
        return
    entry.assert_awaited_once()
    assert entry.await_args.kwargs["conversation_title"] == "Real Group Name"
    assert entry.await_args.kwargs["conversation_id"] == "cid_title"
    assert entry.await_args.kwargs["conversation_type"] == "2"
