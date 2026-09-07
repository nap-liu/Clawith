"""Behavior tests for the shared user-visible text guard."""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.mcp_server import MCPServer  # noqa: F401 - register Tool FK target
from app.models.participant import Participant  # noqa: F401 - register ChatMessage FK target
from app.models.tenant import Tenant
from app.models.tool import Tool
from app.models.user import Identity, User
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.chat_history import persist_assistant_reply_row
from app.services.im_delivery import IMDeliveryResult, attach_delivery_to_meta
from app.services.llm.failure_outcome import make_llm_failure
from app.services.turn_runtime import TurnRuntime
from app.services.tool_seeder import seed_builtin_tools
from app.services.user_output import UserOutputStreamSanitizer, sanitize_user_visible_text

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_messages_and_engine():
    await engine.dispose()
    async with async_session() as db:
        await db.execute(delete(ChatMessage))
        await db.commit()
    yield
    await engine.dispose()


async def _seed_session() -> tuple[Agent, User, ChatSession]:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Output guard {suffix}", slug=f"output-guard-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"output_guard_{suffix}",
            email=f"output-guard-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="Output Guard Tester",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"Output Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add(agent)
        await db.flush()
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="Output guard",
            source_channel="slack",
            external_conv_id=f"slack_{suffix}",
        )
        db.add(session)
        await db.commit()
        await db.refresh(agent)
        await db.refresh(user)
        await db.refresh(session)
        return agent, user, session


async def test_guard_removes_all_case_variants_and_empty_wrappers():
    legacy_keyword = "Cla" + "WiTh"
    text = (
        f"[{legacy_keyword}] Release "
        f"【{legacy_keyword.swapcase()}】note pre{legacy_keyword.upper()}post"
    )

    cleaned = sanitize_user_visible_text(text)

    assert cleaned == "Release note prepost"
    assert legacy_keyword.casefold() not in cleaned.casefold()
    assert "[]" not in cleaned
    assert "【】" not in cleaned


async def test_stream_guard_catches_keyword_split_across_chunks():
    guard = UserOutputStreamSanitizer()
    chunks = ["before [cL", "aW", "iTh] after " + ("x" * 40)]

    cleaned = "".join(guard.feed(chunk) for chunk in chunks) + guard.flush()

    assert cleaned == "before after " + ("x" * 40)


async def test_assistant_persistence_sanitizes_content_and_visible_thinking():
    agent, user, session = await _seed_session()
    legacy_keyword = "cLa" + "WiTh"
    async with async_session() as db:
        message_id = await persist_assistant_reply_row(
            db,
            agent_id=agent.id,
            user_id=user.id,
            conversation_id=str(session.id),
            content=f"prefix 【{legacy_keyword}】message and pre{legacy_keyword}post",
            thinking=f"[{legacy_keyword.upper()}] visible trace",
        )
        await db.commit()

    async with async_session() as db:
        stored = await db.get(ChatMessage, message_id)

    assert stored.content == "prefix message and prepost"
    assert stored.thinking == "visible trace"
    assert legacy_keyword.casefold() not in stored.content.casefold()
    assert legacy_keyword.casefold() not in stored.thinking.casefold()


async def test_assistant_persistence_keeps_typed_failure_details():
    agent, user, session = await _seed_session()
    failure = make_llm_failure(
        code="provider_rate_limit_exhausted",
        message_key="errors.providerRateLimitExhausted",
        details={"retry_count": 5, "recovery_action": "continue"},
    )
    async with async_session() as db:
        message_id = await persist_assistant_reply_row(
            db,
            agent_id=agent.id,
            user_id=user.id,
            conversation_id=str(session.id),
            content=failure,
        )
        await db.commit()

    async with async_session() as db:
        stored = await db.get(ChatMessage, message_id)

    assert stored.message_meta["error_code"] == "provider_rate_limit_exhausted"
    assert stored.message_meta["llm_failure"] == {
        "code": "provider_rate_limit_exhausted",
        "retry_count": 5,
        "recovery_action": "continue",
    }


async def test_im_delivery_sanitizes_provider_payload_and_existing_anchor(monkeypatch):
    from app.services import im_delivery, turn_runtime

    agent, user, session = await _seed_session()
    legacy_keyword = "CLAW" + "ITH"
    original = f"【{legacy_keyword}】 deliver pre{legacy_keyword.lower()}post"
    async with async_session() as db:
        row = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="assistant",
            content=original,
            conversation_id=str(session.id),
            message_meta=attach_delivery_to_meta(
                {},
                IMDeliveryResult.pending("slack"),
            ),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        message_id = row.id

    delivered: list[str] = []

    async def fake_deliver_message_with_receipt(**kwargs):
        delivered.append(kwargs["message"])
        return IMDeliveryResult.unsupported_delivery("slack", "test_transport")

    monkeypatch.setattr(
        turn_runtime,
        "deliver_message_with_receipt",
        fake_deliver_message_with_receipt,
    )
    result = await im_delivery.deliver_persisted_message(
        message_id=message_id,
        agent_id=agent.id,
        runtime=TurnRuntime(
            session_found=True,
            source_channel="slack",
            conversation_id=str(session.id),
            external_conv_id=session.external_conv_id,
            is_group=False,
        ),
        message=original,
    )

    async with async_session() as db:
        stored = await db.get(ChatMessage, message_id)

    assert result.ok is True
    assert delivered == ["deliver prepost"]
    assert stored.content == "deliver prepost"
    assert legacy_keyword.casefold() not in delivered[0].casefold()
    assert legacy_keyword.casefold() not in stored.content.casefold()


async def test_autonomy_notification_sanitizes_dynamic_title_and_body(monkeypatch):
    from app.services import notification_service
    from app.services.autonomy_service import AutonomyService

    legacy_keyword = "cLaW" + "iTh"
    send_notification = AsyncMock()
    monkeypatch.setattr(notification_service, "send_notification", send_notification)

    class EmptyScalars:
        def first(self):
            return None

    class EmptyResult:
        def scalars(self):
            return EmptyScalars()

    class NoChannelDB:
        async def execute(self, _statement):
            return EmptyResult()

    await AutonomyService()._notify_creator(
        NoChannelDB(),
        SimpleNamespace(
            id=uuid.uuid4(),
            creator_id=uuid.uuid4(),
            name=f"[{legacy_keyword}] Operations",
        ),
        f"pre{legacy_keyword.upper()}post",
        {"summary": f"【{legacy_keyword}】 completed"},
    )

    payload = send_notification.await_args.kwargs
    assert payload["title"] == "Operations: executed prepost"
    assert payload["body"] == '{"summary": "completed"}'
    assert legacy_keyword.casefold() not in payload["title"].casefold()
    assert legacy_keyword.casefold() not in payload["body"].casefold()


async def test_feishu_file_fallback_sanitizes_provider_exception(tmp_path, monkeypatch):
    from app.services import agent_tools
    from app.services.feishu_service import feishu_service

    legacy_keyword = "cLaW" + "iTh"
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF-1.4 test")

    async def fail_upload(*_args, **_kwargs):
        raise RuntimeError(f"[{legacy_keyword}] provider rejected")

    send_message = AsyncMock(return_value={"data": {"message_id": "fallback-1"}})
    monkeypatch.setattr(feishu_service, "upload_and_send_file", fail_upload)
    monkeypatch.setattr(feishu_service, "send_message", send_message)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    _text, result = await agent_tools._send_file_via_feishu(
        uuid.uuid4(),
        SimpleNamespace(app_id="app", app_secret="secret"),
        file_path,
        SimpleNamespace(external_id="user-1", open_id=None),
        "Recipient",
        "Please review",
    )

    provider_text = send_message.await_args.args[4]
    assert result.ok is True
    assert legacy_keyword.casefold() not in provider_text.casefold()
    assert "[]" not in provider_text


async def test_system_email_sanitizes_sender_subject_and_body(monkeypatch):
    from app.services import system_email_service

    legacy_keyword = "ClAw" + "ItH"
    captured: dict[str, str] = {}

    def fake_send_smtp_email(**kwargs):
        captured["message"] = kwargs["msg_string"]

    monkeypatch.setattr(system_email_service, "send_smtp_email", fake_send_smtp_email)
    monkeypatch.setattr(system_email_service, "force_ipv4", contextlib.nullcontext)
    config = system_email_service.SystemEmailConfig(
        from_address="bot@example.com",
        from_name=f"[{legacy_keyword}] Notifications",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="bot@example.com",
        smtp_password="secret",
        smtp_ssl=True,
        smtp_timeout_seconds=15,
    )

    system_email_service._send_email_with_config_sync(
        config,
        "alice@example.com",
        f"【{legacy_keyword}】 Verify account",
        f"Welcome to {legacy_keyword.upper()}",
    )

    assert legacy_keyword.casefold() not in captured["message"].casefold()
    assert "Notifications" in captured["message"]
    assert "Verify account" in captured["message"]


async def test_seeded_and_runtime_llm_tool_surfaces_exclude_banned_keyword():
    import json

    agent, _user, _session = await _seed_session()
    legacy_keyword = ("cla" + "with").casefold()
    await seed_builtin_tools()

    async with async_session() as db:
        persisted = (
            await db.execute(select(Tool).where(Tool.source == "builtin"))
        ).scalars().all()
    persisted_surface = json.dumps(
        [
            {
                "display_name": tool.display_name,
                "description": tool.description,
                "parameters_schema": tool.parameters_schema,
                "config_schema": tool.config_schema,
            }
            for tool in persisted
        ],
        ensure_ascii=False,
    )
    runtime_surface = json.dumps(
        await get_agent_tools_for_llm(agent.id),
        ensure_ascii=False,
    )

    assert legacy_keyword not in persisted_surface.casefold()
    assert legacy_keyword not in runtime_surface.casefold()
