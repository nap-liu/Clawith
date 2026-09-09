"""Confirmation suspension, resumption, delivery, and resolution tests."""

import asyncio
import datetime
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from tests.confirmation_toolcall_support import _make_turn_anchor, _row_payload
from tests.test_confirmation_toolcall import (
    ChannelConfig,
    ChatMessage,
    _make_agent,
    _make_pending,
    _make_session,
    _setup_tables as _setup_tables,
    async_session,
)

pytestmark = pytest.mark.asyncio


async def test_concurrent_suspensions_create_exactly_one_pending_confirmation():
    """Independent turns racing across database sessions converge on one durable card."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="web")

    async def suspend(title: str) -> uuid.UUID:
        return await cs.suspend_for_confirmation(
            agent_id=agent_id,
            conversation_id=str(session.id),
            chat_session_id=session.id,
            source_channel="web",
            user_id=user_id,
            intro_text=None,
            title=title,
            summary="并发请求只能保留一张",
            action=None,
            risk_level="medium",
        )

    with patch.object(cs, "_broadcast", new=AsyncMock()):
        first, second = await asyncio.gather(
            suspend("并发确认 A"),
            suspend("并发确认 B"),
        )

    assert first == second
    async with async_session() as db:
        rows = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(session.id),
                    ChatMessage.role == "tool_call",
                )
            )
        ).scalars().all()
    pending_rows = [
        row
        for row in rows
        if json.loads(row.content).get("status") == "pending"
    ]
    assert [row.id for row in pending_rows] == [first]


async def test_suspend_confirmation_rejects_non_session_conversation():
    """Confirmation state must belong to the normalized ChatSession message stream."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    conversation_id = f"legacy-{uuid.uuid4()}"

    with (
        patch.object(
            cs,
            "_resolve_session_channel",
            new=AsyncMock(return_value=("web", None, False)),
        ),
        patch.object(cs, "_broadcast", new=AsyncMock()),
        pytest.raises(
            RuntimeError,
            match="confirmation requires a normalized chat session",
        ),
    ):
        await cs.suspend_for_confirmation(
            agent_id=agent_id,
            conversation_id=conversation_id,
            chat_session_id=None,
            source_channel="web",
            user_id=user_id,
            intro_text=None,
            title="无效确认",
            summary="不应创建独立确认状态",
            action=None,
            risk_level="medium",
        )

    async with async_session() as db:
        rows = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.role == "tool_call",
                )
            )
        ).scalars().all()
    assert rows == []


async def test_confirmation_result_without_intro_replays_as_standard_tool_pair():
    """An assistant may call the card tool without preceding prose; after resolution,
    the original user anchor and real tool result must still reach continuation."""
    from app.services.chat_history import load_history_for_llm

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="web")
    conv = str(session.id)
    await _make_turn_anchor(agent_id, user_id, conv)
    _conv, row_id = await _make_pending(agent_id, user_id, conv=conv)
    async with async_session() as db:
        row = await db.get(ChatMessage, row_id)
        payload = json.loads(row.content)
        payload["status"] = "done"
        payload["result"] = "用户点了「确认」(value=confirm)、在有效期内。"
        row.content = json.dumps(payload, ensure_ascii=False)
        await db.commit()

    async with async_session() as db:
        history = await load_history_for_llm(
            db,
            agent_id=agent_id,
            conversation_id=conv,
            ctx_size=100,
        )

    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "tool",
    ]
    assert history[-1]["content"].startswith("用户点了")


async def test_confirmation_resume_replays_exact_typed_prefix_without_intro_duplicate():
    """A confirmation suspend keeps length-recovery provenance byte-for-byte.

    The intro remains visible in the transcript, while provider history uses
    only the typed prefix and raw assistant tool-call content.
    """
    from app.services import confirmation_service as cs
    from app.services.chat_history import load_history_for_llm

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="web")
    conv = str(session.id)
    anchor_id = await _make_turn_anchor(agent_id, user_id, conv)
    prefix = [
        {"role": "assistant", "content": "partial before output limit"},
        {"role": "user", "content": "Continue exactly where you left off."},
    ]

    with patch.object(cs, "_broadcast", new=AsyncMock()):
        row_id = await cs.suspend_for_confirmation(
            agent_id=agent_id,
            conversation_id=conv,
            chat_session_id=session.id,
            source_channel="web",
            user_id=user_id,
            intro_text="此前可见片段\n\n现在需要确认",
            title="确认执行",
            summary="验证恢复前缀",
            action=None,
            risk_level="high",
            buttons=[{"text": "确认", "value": "confirm"}],
            turn_anchor_id=anchor_id,
            assistant_content="现在需要确认",
            recovery_prefix_messages=prefix,
            reasoning_content="typed reasoning",
            round_id="typed-confirmation-round",
        )

    async with async_session() as db:
        row = await db.get(ChatMessage, row_id)
        payload = json.loads(row.content)
        payload["status"] = "done"
        payload["result"] = "用户点了确认"
        row.content = json.dumps(payload, ensure_ascii=False)
        await db.commit()

    async with async_session() as db:
        history = await load_history_for_llm(
            db,
            agent_id=agent_id,
            conversation_id=conv,
            ctx_size=100,
        )

    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
    ]
    assert history[1:3] == prefix
    assert history[3]["content"] == "现在需要确认"
    assert history[3]["reasoning_content"] == "typed reasoning"
    assert history[3]["tool_calls"][0]["function"]["name"] == "request_confirmation"
    assert all("此前可见片段" not in str(message.get("content")) for message in history)


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
    monkeypatch.setattr(cs, "deliver_reply_to_origin", AsyncMock())

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


async def test_local_confirmation_completion_acknowledges_reply_once(monkeypatch, caplog):
    """The terminal event's sent receipt completes H5 confirmation delivery."""
    from app.api.websocket import manager
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="miniprogram")
    conv = str(session.id)
    anchor_id = await _make_turn_anchor(agent_id, user_id, conv)
    events = []

    async def capture_event(_agent_id, _conversation_id, payload):
        events.append(payload)

    monkeypatch.setattr(manager, "send_to_session", capture_event)
    monkeypatch.setattr(
        "app.services.channel_llm._call_agent_llm", AsyncMock(return_value="Confirmed action completed"),
    )
    await cs._reenter_loop(agent_id, conv, user_id, turn_anchor_id=anchor_id)

    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
        replies = list(await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == conv, ChatMessage.role == "assistant",
        )))
    assert anchor.message_meta["turn_status"] == "completed"
    assert len(replies) == 1
    assert replies[0].message_meta["turn_status"] == "completed"
    assert replies[0].message_meta["delivery"]["status"] == "sent"
    terminals = [event for event in events if event.get("event_kind") == "turn_terminal"]
    assert len(terminals) == 1
    assert terminals[0]["message_id"] == str(replies[0].id)
    assert not any("origin delivery failed" in record.message for record in caplog.records)


async def test_reenter_loop_skips_cancelled_confirmation_turn(monkeypatch):
    """A /stop in the pending-to-reenter window prevents continuation."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    conv = str(uuid.uuid4())
    anchor_id = await _make_turn_anchor(agent_id, user_id, conv)
    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
        anchor.message_meta = {
            **(anchor.message_meta or {}),
            "turn_status": "cancelled",
            "cancel_reason": "stop",
        }
        await db.commit()

    async def fail_if_llm_called(*_args, **_kwargs):
        raise AssertionError("cancelled confirmation continuation must not call the LLM")

    async def fake_run_channel_message(_lock_key, *, work, **_kwargs):
        return await work()

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fail_if_llm_called)
    monkeypatch.setattr(
        "app.services.channel_dispatch.run_channel_message",
        fake_run_channel_message,
    )
    delivery = AsyncMock()
    monkeypatch.setattr(cs, "deliver_reply_to_origin", delivery)

    await cs._reenter_loop(agent_id, conv, user_id, turn_anchor_id=anchor_id)

    delivery.assert_not_awaited()
    async with async_session() as db:
        replies = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv,
                    ChatMessage.role == "assistant",
                )
            )
        ).scalars().all()
    assert replies == []


async def test_dingtalk_group_confirmation_followup_uses_unified_origin_delivery(monkeypatch):
    """A DingTalk group card continuation reaches the persisted group target."""
    from app.services import channel_dispatch, turn_runtime
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    external_conv_id = f"dingtalk_group_open-conversation-{uuid.uuid4().hex}"
    app_id = f"ding-app-{uuid.uuid4().hex}"
    session = await _make_session(
        agent_id,
        user_id,
        source_channel="dingtalk",
        external_conv_id=external_conv_id,
        is_group=True,
    )
    async with async_session() as db:
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

    captured = {}
    lock_keys = []

    async def fake_group_send(**kwargs):
        captured.update(kwargs)
        return {"errcode": 0}

    async def fake_call_agent_llm(*_args, **_kwargs):
        return "卡片处理完成"

    async def fake_run_channel_message(lock_key, *, work, **_kwargs):
        lock_keys.append(lock_key)
        return await work()

    monkeypatch.setattr(turn_runtime, "_send_dingtalk_group_markdown", fake_group_send)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr("app.services.channel_dispatch.run_channel_message", fake_run_channel_message)

    await cs._reenter_loop(agent_id, str(session.id), user_id)

    assert captured == {
        "app_id": app_id,
        "app_secret": "ding-secret",
        "open_conversation_id": external_conv_id.removeprefix("dingtalk_group_"),
        "message": "卡片处理完成",
        "raise_on_transport_error": True,
    }
    assert lock_keys == [
        channel_dispatch.channel_session_lock_key(
            agent_id,
            "dingtalk",
            external_conv_id,
        )
    ]


@pytest.mark.parametrize("source_channel", ["web", "miniprogram", "wechat_miniprogram"])
async def test_first_party_confirmation_followup_uses_unified_origin_delivery(monkeypatch, source_channel):
    """Web and both H5 Chat channels receive the resumed final reply live."""
    from app.api.websocket import manager
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel=source_channel)
    delivered = []

    async def fake_call_agent_llm(*_args, **_kwargs):
        return "卡片处理完成"

    async def fake_run_channel_message(_conversation_id, *, work, **_kwargs):
        return await work()

    async def fake_send_to_session(target_agent_id, conversation_id, payload):
        delivered.append((target_agent_id, conversation_id, payload))

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr("app.services.channel_dispatch.run_channel_message", fake_run_channel_message)
    monkeypatch.setattr(manager, "send_to_session", fake_send_to_session)

    await cs._reenter_loop(agent_id, str(session.id), user_id)

    assert delivered == [
        (
            str(agent_id),
            str(session.id),
            {"type": "done", "role": "assistant", "content": "卡片处理完成"},
        )
    ]


async def test_dingtalk_p2p_confirmation_followup_uses_unified_origin_delivery(monkeypatch):
    """A DingTalk P2P card continuation still uses the exact stored recipient."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    staff_id = f"staff-{uuid.uuid4().hex}"
    app_id = f"ding-app-{uuid.uuid4().hex}"
    session = await _make_session(
        agent_id,
        user_id,
        source_channel="dingtalk",
        external_conv_id=f"dingtalk_p2p_{staff_id}",
    )
    async with async_session() as db:
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

    captured = {}

    async def fake_call_agent_llm(*_args, **_kwargs):
        return "卡片处理完成"

    async def fake_run_channel_message(_conversation_id, *, work, **_kwargs):
        return await work()

    async def fake_p2p_send(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"errcode": 0}

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr("app.services.channel_dispatch.run_channel_message", fake_run_channel_message)
    monkeypatch.setattr("app.services.dingtalk_service.send_dingtalk_v1_robot_oto_message", fake_p2p_send)

    await cs._reenter_loop(agent_id, str(session.id), user_id)

    assert captured == {
        "args": (app_id, "ding-secret", [staff_id], "卡片处理完成"),
        "kwargs": {
            "msg_type": "markdown",
            "robot_code": app_id,
            "raise_on_transport_error": True,
        },
    }


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
    session = await _make_session(agent_id, user_id, source_channel="web")
    conv = str(session.id)
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
        patch.object(cs, "deliver_reply_to_origin", new=AsyncMock()) as reply,
        patch.object(cs, "resolve_confirmation", new=AsyncMock()) as resolve,
    ):
        delivery_id = f"{row_id.hex}.retrycard"
        await cs.resolve_confirmation_via_dingtalk(
            delivery_id,
            "staff-x",
            "confirm",
            "确认",
        )

    resolve.assert_not_awaited()  # no re-resolve of an already-resolved card
    reply.assert_not_awaited()    # no message delivery, no agent wake-up
    push.assert_awaited()         # only the card buttons get disabled → 已过期
    assert push.await_args.kwargs.get("buttons_disabled") is True
    assert push.await_args.args[1] == delivery_id
    fields = push.await_args.args[2]
    assert fields["buttons"] == [{"text": "已过期", "value": "expired", "color": "gray"}]


async def test_suspend_persists_intro_before_toolcall_and_broadcasts_web():
    """Suspending persists the agent's intro text BEFORE the pending tool_call row (so reload
    renders text-before-card) and broadcasts the card as a running tool_call event (web)."""
    from app.services import confirmation_service as cs

    agent_id, user_id = await _make_agent()
    session = await _make_session(agent_id, user_id, source_channel="web")
    conv = str(session.id)

    with patch.object(cs, "_broadcast", new=AsyncMock()) as broadcast:
        row_id = await cs.suspend_for_confirmation(
            agent_id=agent_id, conversation_id=conv, chat_session_id=session.id,
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
    assert json.loads(rows[1].content)["status"] == "pending"
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
