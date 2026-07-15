"""Confirmation-as-tool_call backend tests.

The confirmation card is a normal tool_call that suspends the turn until the user
clicks. There is NO parallel table/event/role — the card IS a `request_confirmation`
tool_call ChatMessage row, and the user's click fills its tool result and resumes the
loop. These tests pin:

- persist_pending_confirmation: suspending writes a pending tool_call row + returns its id
- (resolve + reenter tests are added as the resolve path is built)
"""

import datetime
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from app.database import async_session, engine
from app.models.audit import ChatMessage
from app.models.agent import Agent  # noqa: F401 — FK target registered in metadata
from app.models.user import User, Identity  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.models.participant import Participant  # noqa: F401 — ChatMessage.participant_id FK

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
    from app.services.chat_history import persist_pending_confirmation
    from sqlalchemy import select

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


async def _make_turn_anchor(agent_id, user_id, conv: str) -> uuid.UUID:
    from app.services.chat_history import persist_incoming_user_message

    async with async_session() as db:
        row = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="请执行危险操作",
        )
        await db.commit()
        return row.id


async def _row_payload(row_id) -> dict:
    async with async_session() as db:
        row = (await db.execute(select(ChatMessage).where(ChatMessage.id == row_id))).scalar_one()
    return json.loads(row.content)


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

    agent_id, user_id = await _make_agent()
    conv = str(uuid.uuid4())
    anchor_id = await _make_turn_anchor(agent_id, user_id, conv)

    with patch.object(cs, "_broadcast", new=AsyncMock()):
        row_id = await cs.suspend_for_confirmation(
            agent_id=agent_id,
            conversation_id=conv,
            chat_session_id=None,
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


async def test_suspend_confirmation_persists_intro_before_pending_card():
    """Intro and pending card are persisted in order as ordinary append-only messages."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    conv = str(uuid.uuid4())
    anchor_id = await _make_turn_anchor(agent_id, user_id, conv)

    with patch.object(cs, "_broadcast", new=AsyncMock()):
        row_id = await cs.suspend_for_confirmation(
            agent_id=agent_id,
            conversation_id=conv,
            chat_session_id=None,
            source_channel="web",
            user_id=user_id,
            intro_text="需要你确认",
            title="删库确认",
            summary="清理历史订单",
            action=None,
            risk_level="high",
            buttons=[{"text": "确认", "value": "confirm"}],
            turn_anchor_id=anchor_id,
        )

    async with async_session() as db:
        rows = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv,
                    ChatMessage.role.in_(["assistant", "tool_call"]),
                )
                .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            )
        ).scalars().all()
    assert [row.role for row in rows] == ["assistant", "tool_call"]
    assert rows[0].content == "需要你确认"
    assert rows[1].id == row_id
    assert json.loads(rows[1].content)["status"] == "pending"


async def test_reenter_loop_marks_turn_completed_after_final_reply(monkeypatch):
    """After a confirmation click completes normally, the original turn is completed."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    conv = str(uuid.uuid4())
    anchor_id = await _make_turn_anchor(agent_id, user_id, conv)
    captured: dict = {}

    async def fake_call_agent_llm(*_args, **kwargs):
        captured.update(kwargs)
        return "最终已完成"

    async def fake_run_channel_message(_conversation_id, *, work, **_kwargs):
        return await work()

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr("app.services.channel_dispatch.run_channel_message", fake_run_channel_message)
    monkeypatch.setattr(cs, "_deliver_reply_to_channel", AsyncMock())

    await cs._reenter_loop(agent_id, conv, user_id, turn_anchor_id=anchor_id)

    assert captured["continue_turn"] is True
    assert captured["turn_anchor_id"] == anchor_id
    async with async_session() as db:
        replies = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv,
                    ChatMessage.role == "assistant",
                    ChatMessage.content == "最终已完成",
                )
            )
        ).scalars().all()
    assert len(replies) == 1


async def test_reenter_loop_does_not_complete_turn_when_final_persist_fails(monkeypatch):
    """If the durable final reply write fails, startup recovery must be able to retry."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    conv = str(uuid.uuid4())
    anchor_id = await _make_turn_anchor(agent_id, user_id, conv)

    async def fake_call_agent_llm(*_args, **_kwargs):
        return "最终已完成"

    async def fake_run_channel_message(_conversation_id, *, work, **_kwargs):
        return await work()

    async def fail_finalizer(*_args, **_kwargs):
        raise RuntimeError("persist failed")

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr("app.services.channel_dispatch.run_channel_message", fake_run_channel_message)
    monkeypatch.setattr("app.services.chat_history.persist_assistant_reply_and_complete_turn", fail_finalizer)

    with pytest.raises(RuntimeError, match="persist failed"):
        await cs._reenter_loop(agent_id, conv, user_id, turn_anchor_id=anchor_id)

    async with async_session() as db:
        replies = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv,
                    ChatMessage.role == "assistant",
                    ChatMessage.content == "最终已完成",
                )
            )
        ).scalars().all()
    assert replies == []


async def test_call_llm_confirmation_tool_suspends_turn_anchor(monkeypatch):
    """The unified LLM caller must attach the active turn anchor to request_confirmation."""
    from app.services import confirmation_service as cs
    from app.services.llm.caller import call_llm
    from app.services.llm.client import LLMResponse

    class FakeClient:
        async def stream(self, **_kwargs):
            return LLMResponse(
                content="需要你确认",
                tool_calls=[
                    {
                        "id": "confirm-1",
                        "type": "function",
                        "function": {
                            "name": "request_confirmation",
                            "arguments": json.dumps(
                                {
                                    "title": "删库确认",
                                    "summary": "清理历史订单",
                                    "risk_level": "high",
                                    "buttons": [{"text": "确认", "value": "confirm"}],
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
                finish_reason="tool_calls",
            )

        async def close(self):
            pass

    class FakeModel:
        provider = "qwen"
        model = "qwen-turbo"
        base_url = "https://example.invalid"
        api_key_encrypted = ""
        temperature = 0.7
        max_output_tokens = None
        request_timeout = 30.0
        id = "model-x"
        supports_vision = False

    agent_id, user_id = await _make_agent()
    conv = str(uuid.uuid4())
    anchor_id = await _make_turn_anchor(agent_id, user_id, conv)

    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **_kwargs: FakeClient())
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *_args, **_kwargs: 1024)
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "fake-key")
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(50, None)))
    monkeypatch.setattr("app.services.llm.caller._get_user_name", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("STATIC", "DYN")),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(
            return_value=[
                {
                    "type": "function",
                    "function": {"name": "request_confirmation", "description": "ask for confirmation"},
                }
            ]
        ),
    )
    monkeypatch.setattr("app.services.llm.caller.record_token_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(cs, "_broadcast", AsyncMock())

    result = await call_llm(
        model=FakeModel(),
        messages=[{"role": "user", "content": "请执行危险操作"}],
        agent_name="Agent",
        role_description="",
        agent_id=agent_id,
        user_id=user_id,
        session_id=conv,
        turn_anchor_id=anchor_id,
    )

    assert result == ""
    async with async_session() as db:
        pending = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv,
                    ChatMessage.role == "tool_call",
                )
            )
        ).scalar_one()

    payload = json.loads(pending.content)
    assert payload["name"] == "request_confirmation"
    assert payload["status"] == "pending"
    assert "turn_anchor_id" not in payload


async def test_resolve_idempotent_on_already_done():
    """A second resolve (double-click) of the same card is a no-op: no re-fill, no re-enter."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    _conv, row_id = await _make_pending(agent_id, user_id)

    with (
        patch.object(cs, "_reenter_loop", new=AsyncMock()) as reenter,
        patch.object(cs, "_broadcast", new=AsyncMock()),
    ):
        first = await cs.resolve_confirmation(
            agent_id=agent_id, call_id=row_id, button_value="confirm",
            button_label="确认", resolving_user_id=user_id,
        )
        second = await cs.resolve_confirmation(
            agent_id=agent_id, call_id=row_id, button_value="confirm",
            button_label="确认", resolving_user_id=user_id,
        )

    assert first is not None
    assert second is None  # idempotent — already resolved
    assert reenter.await_count == 1


async def test_resolve_expired_marks_unreliable():
    """A card older than the 24h window resolves with a result that flags the response as
    unreliable and warns against acting on dangerous operations."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    _conv, row_id = await _make_pending(agent_id, user_id)
    # Backdate the row beyond the expiry window.
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)
    async with async_session() as db:
        await db.execute(update(ChatMessage).where(ChatMessage.id == row_id).values(created_at=old))
        await db.commit()

    with (
        patch.object(cs, "_reenter_loop", new=AsyncMock()),
        patch.object(cs, "_broadcast", new=AsyncMock()),
    ):
        result = await cs.resolve_confirmation(
            agent_id=agent_id, call_id=row_id, button_value="confirm",
            button_label="确认", resolving_user_id=user_id,
        )

    assert result is not None
    assert "失效" not in result or "不可靠" in result  # message conveys staleness
    assert "不可靠" in result
    payload = await _row_payload(row_id)
    assert payload["status"] == "done"


async def test_dingtalk_stale_click_only_disables_card_no_reresolve_no_message():
    """A DingTalk click on a card already resolved elsewhere (e.g. on web) is a framework-level
    no-op beyond the visual: it must NOT re-resolve, NOT deliver any message, NOT wake the
    agent — it just replaces the card's buttons with a single disabled 已过期 button."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    _conv, row_id = await _make_pending(agent_id, user_id)
    # Mark the row already resolved (as if web resolved it first).
    async with async_session() as db:
        row = (await db.execute(select(ChatMessage).where(ChatMessage.id == row_id))).scalar_one()
        payload = json.loads(row.content)
        payload["status"] = "done"
        payload["result"] = "用户点了「确认」(value=confirm)、在有效期内。"
        await db.execute(update(ChatMessage).where(ChatMessage.id == row_id).values(content=json.dumps(payload)))
        await db.commit()

    with (
        patch.object(cs, "_push_card_state", new=AsyncMock()) as push,
        patch.object(cs, "_deliver_reply_to_channel", new=AsyncMock()) as reply,
        patch.object(cs, "resolve_confirmation", new=AsyncMock()) as resolve,
    ):
        await cs.resolve_confirmation_via_dingtalk(str(row_id), "staff-x", "confirm", "确认")

    resolve.assert_not_awaited()  # no re-resolve of an already-resolved card
    reply.assert_not_awaited()    # no message delivery, no agent wake-up
    push.assert_awaited()         # only the card buttons get disabled → 已过期
    assert push.await_args.kwargs.get("buttons_disabled") is True
    fields = push.await_args.args[2]
    assert fields["buttons"] == [{"text": "已过期", "value": "expired", "color": "gray"}]


async def test_suspend_persists_intro_before_toolcall_and_broadcasts_web():
    """Suspending persists the agent's intro text BEFORE the pending tool_call row (so reload
    renders text-before-card) and broadcasts the card as a running tool_call event (web)."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    conv = str(uuid.uuid4())

    with patch.object(cs, "_broadcast", new=AsyncMock()) as broadcast:
        row_id = await cs.suspend_for_confirmation(
            agent_id=agent_id, conversation_id=conv, chat_session_id=None,
            source_channel="web", user_id=user_id, intro_text="我需要你确认删库操作:",
            title="删库确认", summary="清理历史订单", action=None, risk_level="high",
            buttons=[{"text": "确认", "value": "confirm", "color": "red"}],
        )

    async with async_session() as db:
        rows = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.conversation_id == conv).order_by(ChatMessage.created_at)
            )
        ).scalars().all()
    # Intro assistant text precedes the tool_call card row.
    assert [r.role for r in rows] == ["assistant", "tool_call"]
    assert rows[0].content == "我需要你确认删库操作:"
    assert rows[1].id == row_id
    # Card broadcast as a running tool_call (the frontend renders it as the card).
    broadcast.assert_awaited()
    payload = broadcast.await_args.args[2]
    assert payload["type"] == "tool_call"
    assert payload["name"] == "request_confirmation"
    assert payload["call_id"] == str(row_id)
    assert payload["status"] == "running"
    assert payload["args"]["buttons"][0]["value"] == "confirm"


async def test_confirmation_rejects_a_different_resolving_user():
    """A shareable/forwarded card cannot be resolved by another agent user."""
    from app.services import confirmation_service as cs

    agent_id, intended_user_id = await _make_agent()
    _conv, row_id = await _make_pending(agent_id, intended_user_id)

    with pytest.raises(cs.ConfirmationActorMismatch):
        await cs.resolve_confirmation(
            agent_id=agent_id,
            call_id=row_id,
            button_value="confirm",
            button_label="确认",
            resolving_user_id=uuid.uuid4(),
        )

    payload = await _row_payload(row_id)
    assert payload["status"] == "pending"
