"""Confirmation-as-tool_call backend tests.

The confirmation card is a normal tool_call that suspends the turn until the user
clicks. There is NO parallel table/event/role — the card IS a `request_confirmation`
tool_call ChatMessage row, and the user's click fills its tool result and resumes the
loop. These tests pin:

- persist_pending_confirmation: suspending writes a pending tool_call row + returns its id
- (resolve + reenter tests are added as the resolve path is built)
"""

import asyncio
import datetime
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.participant import Participant  # noqa: F401 — ChatMessage.participant_id FK
from app.models.tenant import Tenant
from app.models.user import Identity, User
from tests.confirmation_toolcall_support import _make_turn_anchor, _row_payload

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _setup_tables():
    # The isolated clawith_test DB is a full schema clone, so every table already
    # exists — no create_all needed (and ChatMessage's FKs to participants/agents
    # would need those table objects in metadata to DDL-generate anyway).
    yield
    await engine.dispose()


async def _make_agent() -> tuple[uuid.UUID, uuid.UUID]:
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        tenant = Tenant(name=f"t_{suffix}", slug=f"t-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(username=f"u_{suffix}", email=f"{suffix}@t.local", password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id, display_name="Tester", role="member",
            is_active=True, tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(name=f"Agent_{suffix}", creator_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id, user.id


async def test_persist_pending_confirmation_writes_pending_toolcall_row():
    """Suspending a confirmation persists a `request_confirmation` tool_call row with
    status='pending' and an empty result, and returns the row id so the card delivery
    (web event / DingTalk outTrackId) can reference it for later resolution."""
    from sqlalchemy import select

    from app.services.chat_history import persist_pending_confirmation

    agent_id, user_id = await _make_agent()
    conv = str(uuid.uuid4())
    args = {"title": "删库确认", "summary": "清理历史订单", "buttons": [{"text": "确认", "value": "confirm"}]}

    row_id = await persist_pending_confirmation(
        async_session,
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=conv,
        name="request_confirmation",
        args=args,
    )

    assert isinstance(row_id, uuid.UUID)
    async with async_session() as db:
        row = (await db.execute(select(ChatMessage).where(ChatMessage.id == row_id))).scalar_one()
    assert row.role == "tool_call"
    assert row.conversation_id == conv
    payload = json.loads(row.content)
    assert payload["name"] == "request_confirmation"
    assert payload["status"] == "pending"
    assert payload["result"] == ""
    assert payload["args"] == args


def test_mark_selected_buttons_adds_check_to_chosen_only():
    """The resolved card marks ONLY the chosen button with a ✅ so a row of disabled buttons
    still shows what was selected."""
    from app.services.confirmation_service import _mark_selected_buttons

    buttons = [
        {"text": "确认删除", "value": "confirm", "color": "red"},
        {"text": "取消", "value": "cancel", "color": "gray"},
    ]
    out = _mark_selected_buttons(buttons, "confirm")
    assert out[0]["text"] == "✅ 确认删除"
    assert out[1]["text"] == "取消"
    assert out[0]["color"] == "red"  # other fields preserved
    # Idempotent — re-marking doesn't double the ✅.
    assert _mark_selected_buttons(out, "confirm")[0]["text"] == "✅ 确认删除"
    # None buttons fall back to default confirm/cancel and still mark the choice.
    assert _mark_selected_buttons(None, "cancel")[0]["text"] == "✅ 取消"


async def _make_pending(agent_id, user_id, *, conv=None, args=None) -> tuple[str, uuid.UUID]:
    from app.services.chat_history import persist_pending_confirmation

    conv = conv or str(uuid.uuid4())
    row_id = await persist_pending_confirmation(
        async_session,
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=conv,
        name="request_confirmation",
        args=args or {"title": "删库确认", "summary": "...", "buttons": [{"text": "确认", "value": "confirm"}]},
    )
    return conv, row_id


async def _make_session(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    source_channel: str = "web",
    external_conv_id: str | None = None,
    is_group: bool = False,
) -> ChatSession:
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=None if is_group else user_id,
            title="confirmation gate",
            source_channel=source_channel,
            external_conv_id=external_conv_id or f"{source_channel}_p2p_{uuid.uuid4().hex}",
            is_group=is_group,
            group_name="confirmation gate" if is_group else None,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        db.expunge(session)
        return session


async def test_pending_confirmation_blocks_canonical_message_ingest():
    """No text/file event is persisted or routed while the normalized message
    stream contains an unresolved request_confirmation tool call."""
    from app.services.chat_history import ingest_incoming_chat_message

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="dingtalk")
    _conv, row_id = await _make_pending(
        agent_id,
        user_id,
        conv=str(session.id),
        args={"title": "确认", "summary": "继续操作"},
    )

    async with async_session() as db:
        live_session = await db.get(ChatSession, session.id)
        result = await ingest_incoming_chat_message(
            db,
            session=live_session,
            agent_id=agent_id,
            user_id=user_id,
            content="不要点了，直接继续",
            source_channel="dingtalk",
            provider_event_id="blocked-event-1",
            channel_config_id="bot-1",
            actor_ref="staff-1",
        )
        await db.commit()

    assert result.created is False
    assert result.consumed_by_onmessage is True
    assert result.blocked_by_confirmation is True
    assert result.message.id == row_id
    assert result.pending_confirmation.row_id == row_id
    async with async_session() as db:
        blocked_rows = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == str(session.id),
                    ChatMessage.role == "user",
                    ChatMessage.content == "不要点了，直接继续",
                )
            )
        ).scalars().all()
    assert blocked_rows == []


async def test_confirmation_gate_is_limited_to_supported_product_channels():
    """Other IM transports keep their existing input behavior until they
    implement the same clickable confirmation-card lifecycle."""
    from app.services.chat_history import ingest_incoming_chat_message

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="feishu")
    await _make_pending(agent_id, user_id, conv=str(session.id))

    async with async_session() as db:
        live_session = await db.get(ChatSession, session.id)
        result = await ingest_incoming_chat_message(
            db,
            session=live_session,
            agent_id=agent_id,
            user_id=user_id,
            content="飞书沿用原有输入链路",
            source_channel="feishu",
            provider_event_id=f"feishu-{uuid.uuid4()}",
            channel_config_id=session.id,
            actor_ref=str(user_id),
        )
        await db.commit()

    assert result.created is True
    assert result.blocked_by_confirmation is False
    assert result.ignored_confirmation is None


@pytest.mark.parametrize(
    "source_channel",
    ["web", "miniprogram", "wechat_miniprogram"],
)
async def test_first_party_h5_channels_share_the_confirmation_hard_gate(
    source_channel: str,
):
    from app.services.chat_history import ingest_incoming_chat_message

    agent_id, user_id = await _make_agent()
    session = await _make_session(
        agent_id,
        user_id,
        source_channel=source_channel,
    )
    _conversation_id, row_id = await _make_pending(
        agent_id,
        user_id,
        conv=str(session.id),
    )

    async with async_session() as db:
        live_session = await db.get(ChatSession, session.id)
        result = await ingest_incoming_chat_message(
            db,
            session=live_session,
            agent_id=agent_id,
            user_id=user_id,
            content="必须先处理确认卡",
            source_channel=source_channel,
            provider_event_id=f"{source_channel}-{uuid.uuid4()}",
            channel_config_id=session.id,
            actor_ref=str(user_id),
        )
        await db.commit()

    assert result.created is False
    assert result.blocked_by_confirmation is True
    assert result.pending_confirmation is not None
    assert result.pending_confirmation.row_id == row_id


async def test_non_blocking_confirmation_is_closed_before_new_user_message():
    """A new message ignores a non-blocking card with a real negative tool result."""
    from app.services.chat_history import ingest_incoming_chat_message
    from app.services.confirmation_service import IGNORED_CONFIRMATION_RESULT

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="web")
    _conv, row_id = await _make_pending(
        agent_id,
        user_id,
        conv=str(session.id),
        args={
            "title": "是否继续",
            "summary": "可以忽略",
            "force_confirmation": False,
        },
    )

    async with async_session() as db:
        live_session = await db.get(ChatSession, session.id)
        result = await ingest_incoming_chat_message(
            db,
            session=live_session,
            agent_id=agent_id,
            user_id=user_id,
            content="先处理另一件事",
            source_channel="web",
            provider_event_id="ignore-confirmation-1",
            channel_config_id=session.id,
            actor_ref=str(user_id),
        )
        await db.commit()

    assert result.created is True
    assert result.blocked_by_confirmation is False
    assert result.ignored_confirmation is not None
    assert result.ignored_confirmation.row_id == row_id
    assert result.ignored_confirmation_result == IGNORED_CONFIRMATION_RESULT
    payload = await _row_payload(row_id)
    assert payload["status"] == "done"
    assert payload["result"] == IGNORED_CONFIRMATION_RESULT


async def test_latest_tool_call_is_the_normalized_confirmation_state():
    """A later tool call means the standard message tail is no longer suspended."""
    from app.services.chat_history import ingest_incoming_chat_message

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="web")
    await _make_pending(agent_id, user_id, conv=str(session.id))
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "read_file",
                        "args": {"path": "a.txt"},
                        "status": "done",
                        "result": "ok",
                    }
                ),
                conversation_id=str(session.id),
                created_at=datetime.datetime.now(datetime.UTC)
                + datetime.timedelta(seconds=1),
            )
        )
        await db.commit()

    async with async_session() as db:
        live_session = await db.get(ChatSession, session.id)
        result = await ingest_incoming_chat_message(
            db,
            session=live_session,
            agent_id=agent_id,
            user_id=user_id,
            content="正常继续",
            source_channel="web",
            provider_event_id="last-tool-wins",
            channel_config_id=session.id,
            actor_ref=str(user_id),
        )
        await db.commit()

    assert result.created is True
    assert result.blocked_by_confirmation is False


async def test_provider_retry_is_deduplicated_before_confirmation_gate():
    """A retry of an event accepted before suspension is not a new blocked message."""
    from app.services.chat_history import ingest_incoming_chat_message

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="dingtalk")
    async with async_session() as db:
        live_session = await db.get(ChatSession, session.id)
        first = await ingest_incoming_chat_message(
            db,
            session=live_session,
            agent_id=agent_id,
            user_id=user_id,
            content="执行操作",
            source_channel="dingtalk",
            provider_event_id="original-event-1",
            channel_config_id="bot-1",
            actor_ref="staff-1",
        )
        await db.commit()

    await _make_pending(agent_id, user_id, conv=str(session.id))

    async with async_session() as db:
        live_session = await db.get(ChatSession, session.id)
        retry = await ingest_incoming_chat_message(
            db,
            session=live_session,
            agent_id=agent_id,
            user_id=user_id,
            content="执行操作",
            source_channel="dingtalk",
            provider_event_id="original-event-1",
            channel_config_id="bot-1",
            actor_ref="staff-1",
        )
        await db.commit()

    assert first.created is True
    assert retry.created is False
    assert retry.consumed_by_onmessage is True
    assert retry.blocked_by_confirmation is False
    assert retry.message.id == first.message.id


async def test_finish_blocked_ingest_redelivers_same_confirmation(monkeypatch):
    """IM recovery re-delivers the original card handle instead of creating a row."""
    from app.services import confirmation_service as cs
    from app.services.chat_history import (
        finish_blocked_confirmation_ingest,
        ingest_incoming_chat_message,
    )

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="dingtalk")
    _conv, row_id = await _make_pending(agent_id, user_id, conv=str(session.id))
    redeliver = AsyncMock(return_value=True)
    monkeypatch.setattr(cs, "redeliver_pending_confirmation", redeliver)

    async with async_session() as db:
        live_session = await db.get(ChatSession, session.id)
        result = await ingest_incoming_chat_message(
            db,
            session=live_session,
            agent_id=agent_id,
            user_id=user_id,
            content="[file:test.pdf]",
            source_channel="dingtalk",
            provider_event_id="blocked-file-1",
            channel_config_id="bot-1",
            actor_ref="staff-1",
        )
        handled = await finish_blocked_confirmation_ingest(db, result)

    assert handled is True
    redeliver.assert_awaited_once()
    assert redeliver.await_args.args[0].row_id == row_id


async def test_dingtalk_forced_redelivery_creates_fresh_card_instance(monkeypatch):
    """A rejected DingTalk input creates a new visible card instance, not a repeated
    deliver call for the original outTrackId."""
    from app.models.channel_config import ChannelConfig
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="dingtalk")
    _conv, row_id = await _make_pending(
        agent_id,
        user_id,
        conv=str(session.id),
        args={
            "title": "确认",
            "summary": "必须点击",
            "force_confirmation": True,
        },
    )
    async with async_session() as db:
        app_id = f"ding-{uuid.uuid4().hex}"
        db.add(
            ChannelConfig(
                agent_id=agent_id,
                channel_type="dingtalk",
                app_id=app_id,
                app_secret="ding-secret",
                is_configured=True,
            )
        )
        await db.commit()
        pending = await cs.find_pending_confirmation(
            db,
            agent_id=agent_id,
            conversation_id=str(session.id),
        )
    assert pending is not None

    sent_ids: list[str] = []

    async def fake_send_confirmation_card(**kwargs):
        sent_ids.append(kwargs["out_track_id"])
        return kwargs["out_track_id"]

    monkeypatch.setattr(
        cs,
        "_resolve_session_channel",
        AsyncMock(
            return_value=("dingtalk", session.external_conv_id, False)
        ),
    )
    monkeypatch.setattr(
        "app.services.agent_tools._get_tool_config",
        AsyncMock(return_value={"card_template_id": "template-1"}),
    )
    monkeypatch.setattr(
        "app.services.dingtalk_card.send_confirmation_card",
        fake_send_confirmation_card,
    )
    monkeypatch.setattr(cs, "_mark_card_expired", AsyncMock())

    assert await cs.redeliver_pending_confirmation(pending) is True
    assert len(sent_ids) == 1
    assert sent_ids[0] != str(row_id)
    assert sent_ids[0].startswith(f"{row_id.hex}.")
    async with async_session() as db:
        row = await db.get(ChatMessage, row_id)
    assert row.message_meta["confirmation_delivery_id"] == sent_ids[0]


async def test_channel_command_keeps_priority_over_pending_confirmation():
    """Control-plane commands are not dialogue and must never be blocked by a card."""
    from app.services.channel_commands import handle_channel_command

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="dingtalk")
    await _make_pending(agent_id, user_id, conv=str(session.id))

    async with async_session() as db:
        result = await handle_channel_command(
            db=db,
            command="/reset",
            agent_id=agent_id,
            user_id=user_id,
            external_conv_id=session.external_conv_id,
            source_channel="dingtalk",
        )
        await db.commit()
        updated = await db.get(ChatSession, session.id)

    assert result["action"] == "new_session"
    assert "__archived_" in updated.external_conv_id


async def test_resolve_fills_tool_result_and_reenters():
    """Resolving a pending card fills the SAME tool_call row's result with a faithful relay
    of the click + flips status to done, then re-enters the loop exactly once."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    _conv, row_id = await _make_pending(agent_id, user_id)

    with (
        patch.object(cs, "_reenter_loop", new=AsyncMock()) as reenter,
        patch.object(cs, "_broadcast", new=AsyncMock()) as broadcast,
        patch.object(cs, "_update_origin_card", new=AsyncMock()) as origin_card,
    ):
        result = await cs.resolve_confirmation(
            agent_id=agent_id, call_id=row_id, button_value="confirm",
            button_label="确认", resolving_user_id=user_id,
        )

    assert result is not None
    assert "确认" in result and "value=confirm" in result and "在有效期内" in result
    payload = await _row_payload(row_id)
    assert payload["status"] == "done"
    assert payload["result"] == result
    reenter.assert_awaited_once()
    broadcast.assert_awaited()  # live web card flip
    origin_card.assert_awaited_once()  # origin IM (DingTalk) card kept in sync


async def test_confirmation_pending_tool_call_is_the_suspended_state():
    """A confirmation turn is suspended by the pending tool_call row itself."""
    from app.services import confirmation_service as cs
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="web")
    conv = str(session.id)
    anchor_id = await _make_turn_anchor(agent_id, user_id, conv)

    with patch.object(cs, "_broadcast", new=AsyncMock()):
        row_id = await cs.suspend_for_confirmation(
            agent_id=agent_id,
            conversation_id=conv,
            chat_session_id=session.id,
            source_channel="web",
            user_id=user_id,
            intro_text=None,
            title="删库确认",
            summary="清理历史订单",
            action=None,
            risk_level="high",
            buttons=[{"text": "确认", "value": "confirm"}],
            turn_anchor_id=anchor_id,
        )

    payload = await _row_payload(row_id)
    assert payload["status"] == "pending"
    # The anchor remains platform-private metadata, not model-visible tool
    # content, and is restored when the confirmation resumes the turn.
    assert "turn_anchor_id" not in payload
    async with async_session() as db:
        pending_row = await db.get(ChatMessage, row_id)
        assert pending_row.message_meta["turn_anchor_id"] == str(anchor_id)
        assert pending_row.message_meta["turn_status"] == "suspended"
        suspended_current = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=conv,
        )
        suspended_exact = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=conv,
            turn_anchor_id=anchor_id,
        )
        assert suspended_current == suspended_exact
        assert suspended_current.status == "suspended"

    with (
        patch.object(cs, "_reenter_loop", new=AsyncMock()) as reenter,
        patch.object(cs, "_broadcast", new=AsyncMock()),
        patch.object(cs, "_update_origin_card", new=AsyncMock()),
    ):
        result = await cs.resolve_confirmation(
            agent_id=agent_id,
            call_id=row_id,
            button_value="confirm",
            button_label="确认",
            resolving_user_id=user_id,
        )

    assert result is not None
    reenter.assert_awaited_once()
    assert reenter.await_args.kwargs["turn_anchor_id"] == anchor_id
    async with async_session() as db:
        resumed_current = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=conv,
        )
        resumed_exact = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=conv,
            turn_anchor_id=anchor_id,
        )
    assert resumed_current == resumed_exact
    assert resumed_current.status == "running"


async def test_session_lock_reuses_existing_pending_confirmation():
    """A second suspension attempt in the same real session cannot create another card."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="web")
    with patch.object(cs, "_broadcast", new=AsyncMock()):
        first = await cs.suspend_for_confirmation(
            agent_id=agent_id,
            conversation_id=str(session.id),
            chat_session_id=session.id,
            source_channel="web",
            user_id=user_id,
            intro_text=None,
            title="第一次确认",
            summary="只能存在一张",
            action=None,
            risk_level="medium",
        )
        second = await cs.suspend_for_confirmation(
            agent_id=agent_id,
            conversation_id=str(session.id),
            chat_session_id=session.id,
            source_channel="web",
            user_id=user_id,
            intro_text=None,
            title="第二次确认",
            summary="不能覆盖第一张",
            action=None,
            risk_level="medium",
        )

    assert second == first
    async with async_session() as db:
        rows = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(session.id),
                    ChatMessage.role == "tool_call",
                )
            )
        ).scalars().all()
    assert [row.id for row in rows] == [first]


async def test_pending_lookup_reads_latest_tool_call_without_casting_history():
    """Escaped NULs in older tool results cannot break the normalized tail lookup."""
    from app.services import confirmation_service as cs
    from app.services.chat_history import persist_pending_confirmation_row

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="web")
    now = datetime.datetime.now(datetime.UTC)

    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=(
                    '{"name":"read_file","args":{},"status":"done",'
                    '"result":"historic\\\\u0000payload"}'
                ),
                conversation_id=str(session.id),
                created_at=now - datetime.timedelta(seconds=1),
            )
        )
        pending_id = await persist_pending_confirmation_row(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=str(session.id),
            name="request_confirmation",
            args={
                "title": "确认",
                "summary": "读取最新工具消息",
                "force_confirmation": True,
            },
            created_at=now,
        )
        await db.commit()

    async with async_session() as db:
        pending = await cs.find_pending_confirmation(
            db,
            agent_id=agent_id,
            conversation_id=str(session.id),
        )

    assert pending is not None
    assert pending.row_id == pending_id


async def test_pending_lookup_does_not_reopen_an_older_confirmation():
    """A later completed tool call closes the normalized tail's pending state."""
    from app.services import confirmation_service as cs
    from app.services.chat_history import persist_pending_confirmation_row

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="web")
    now = datetime.datetime.now(datetime.UTC)

    async with async_session() as db:
        await persist_pending_confirmation_row(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=str(session.id),
            name="request_confirmation",
            args={"title": "旧确认", "summary": "不应重新生效"},
            created_at=now,
        )
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "request_confirmation",
                        "args": {"title": "旧确认"},
                        "status": "done",
                        "result": "用户已处理",
                    },
                    ensure_ascii=False,
                ),
                conversation_id=str(session.id),
                created_at=now + datetime.timedelta(microseconds=1),
            )
        )
        await db.commit()

    async with async_session() as db:
        pending = await cs.find_pending_confirmation(
            db,
            agent_id=agent_id,
            conversation_id=str(session.id),
        )

    assert pending is None
