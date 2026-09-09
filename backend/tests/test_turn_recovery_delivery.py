"""Mechanical continuation of startup turn-recovery tests."""

import pytest

from tests.test_turn_recovery import (
    UTC as UTC,
    Agent as Agent,
    ChannelConfig,
    ChatMessage,
    ChatSession,
    IdentityProvider as IdentityProvider,
    OrgMember as OrgMember,
    SimpleNamespace as SimpleNamespace,
    User as User,
    _dispose_engine_between_tests,  # noqa: F401 - pytest autouse fixture
    _make_agent_with_model,
    _make_user_anchor as _make_user_anchor,
    asyncio,
    async_session,
    datetime,
    delete as delete,
    engine as engine,
    json,
    select,
    text as text,
    timedelta,
    timezone,
    uuid,
)

pytestmark = pytest.mark.asyncio
async def test_new_can_archive_during_delivery_and_prevents_redelivery(monkeypatch):
    """Network waits release the DB; an archived route cannot be delivered again."""
    from app.services import channel_commands, turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=1)
    external_conv_id = f"dingtalk_group_locked_{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk locked delivery",
            source_channel="dingtalk",
            external_conv_id=external_conv_id,
        )
        db.add(session)
        await db.flush()
        conv = str(session.id)
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="lock delivery generation",
        )
        anchor_id = anchor.id
        await db.commit()

    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
    expected_origin = await turn_recovery._load_fresh_recovery_origin(anchor)
    assert expected_origin is not None

    delivery_entered = asyncio.Event()
    allow_delivery = asyncio.Event()
    delivery_finished = asyncio.Event()
    delivery_calls = []

    async def fake_deliver(**_kwargs):
        delivery_calls.append(_kwargs)
        delivery_entered.set()
        await allow_delivery.wait()
        delivery_finished.set()
        return True

    async def no_local_task(_lock_key: str) -> bool:
        return False

    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver)
    monkeypatch.setattr(channel_commands, "cancel_running_turn", no_local_task)

    delivery_task = asyncio.create_task(
        turn_recovery._deliver_recovered_reply(
            anchor,
            expected_origin=expected_origin,
            reply="serialized reply",
            execution_agent_id=agent_id,
        )
    )
    await asyncio.wait_for(delivery_entered.wait(), timeout=2)

    async def archive_session():
        async with async_session() as db:
            result = await channel_commands.handle_channel_command(
                db=db,
                command="/new",
                agent_id=agent_id,
                user_id=user_id,
                external_conv_id=external_conv_id,
                source_channel="dingtalk",
            )
            await db.commit()
            return result

    archive_task = asyncio.create_task(archive_session())
    result = await asyncio.wait_for(archive_task, timeout=2)
    assert result["action"] == "new_session"
    assert delivery_finished.is_set() is False

    allow_delivery.set()
    assert await asyncio.wait_for(delivery_task, timeout=2) is True
    assert delivery_finished.is_set()

    async with async_session() as db:
        session = await db.get(ChatSession, uuid.UUID(conv))
    assert "__archived_" in session.external_conv_id
    assert await turn_recovery._deliver_recovered_reply(
        anchor,
        expected_origin=expected_origin,
        reply="serialized reply",
        execution_agent_id=agent_id,
    ) is False
    assert len(delivery_calls) == 1


async def test_deliver_recovered_reply_routes_dingtalk_from_chat_session(monkeypatch):
    """Restart delivery should derive DingTalk runtime from ChatSession, not turn metadata."""
    from app.services.turn_runtime import deliver_recovered_reply_to_origin

    agent_id, user_id = await _make_agent_with_model(context_window_size=1)
    app_id = f"fake-app-id-{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk",
            source_channel="dingtalk",
            external_conv_id="dingtalk_p2p_staff-42",
        )
        cfg = ChannelConfig(
            agent_id=agent_id,
            channel_type="dingtalk",
            app_id=app_id,
            app_secret="fake-secret",
            is_configured=True,
        )
        db.add_all([session, cfg])
        await db.commit()
        conv = str(session.id)

    captured: dict = {}

    async def fake_send(
        app_id,
        app_secret,
        user_ids,
        message,
        msg_type="text",
        robot_code=None,
        *,
        raise_on_transport_error=False,
    ):
        captured.update(
            {
                "app_id": app_id,
                "app_secret": app_secret,
                "user_ids": user_ids,
                "message": message,
                "msg_type": msg_type,
                "robot_code": robot_code,
                "raise_on_transport_error": raise_on_transport_error,
            }
        )
        return {"errcode": 0}

    monkeypatch.setattr(
        "app.services.dingtalk_service.send_dingtalk_v1_robot_oto_message",
        fake_send,
    )

    ok = await deliver_recovered_reply_to_origin(
        agent_id=agent_id,
        conversation_id=conv,
        reply="恢复完成",
    )

    assert ok is True
    assert captured == {
        "app_id": app_id,
        "app_secret": "fake-secret",
        "user_ids": ["staff-42"],
        "message": "恢复完成",
        "msg_type": "markdown",
        "robot_code": app_id,
        "raise_on_transport_error": True,
    }


async def test_deliver_reply_to_origin_contains_transport_exceptions(monkeypatch):
    """A channel outage must not unwind a turn whose assistant reply is already durable."""
    from app.services import turn_runtime

    agent_id = uuid.uuid4()
    conversation_id = str(uuid.uuid4())

    async def fake_load_turn_runtime(**_kwargs):
        return turn_runtime.TurnRuntime(
            session_found=True,
            source_channel="dingtalk",
            conversation_id=conversation_id,
            external_conv_id="dingtalk_group_test",
            is_group=True,
        )

    async def failing_delivery(**_kwargs):
        raise RuntimeError("temporary channel outage")

    monkeypatch.setattr(turn_runtime, "load_turn_runtime", fake_load_turn_runtime)
    monkeypatch.setattr(turn_runtime, "deliver_message_to_runtime", failing_delivery)

    delivered = await turn_runtime.deliver_reply_to_origin(
        agent_id=agent_id,
        conversation_id=conversation_id,
        reply="already persisted",
        require_transport=True,
    )

    assert delivered is False


async def test_resume_turn_continues_after_completed_tool_call_tail(monkeypatch):
    """Crash after a completed tool row should continue the same tool loop."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    conv = f"tool_tail_{uuid.uuid4().hex}"
    async with async_session() as db:
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="status please",
        )
        anchor_id = anchor.id
        anchor.created_at = datetime.now(timezone.utc) - timedelta(seconds=2)
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "list_sessions",
                        "args": {"query": "刘喜", "limit": 5},
                        "status": "done",
                        "result": "session-1",
                    },
                    ensure_ascii=False,
                ),
                conversation_id=conv,
                created_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        await db.commit()

    captured = {}

    async def fake_call_agent_llm(*_args, **kwargs):
        captured.update(kwargs)
        return "final reply after tool"

    delivered = []

    async def fake_deliver(*, agent_id, conversation_id, reply, message_id):
        assert message_id is not None
        delivered.append((agent_id, conversation_id, reply))
        return True

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert captured["continue_turn"] is True
    assert captured["recovery_mode"] is True
    assert captured["turn_anchor_id"] == anchor_id
    assert [msg["role"] for msg in captured["history"]] == ["user", "assistant", "tool"]
    assert captured["history"][1]["tool_calls"][0]["function"]["name"] == "list_sessions"
    assert captured["history"][2]["content"] == "session-1"
    assert len(delivered) == 1
    async with async_session() as db:
        replies = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == conv,
                        ChatMessage.role == "assistant",
                        ChatMessage.content == "final reply after tool",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(replies) == 1


async def test_resume_turn_does_not_reexecute_unfinished_code(monkeypatch):
    """Recovery never repeats arbitrary code after an ambiguous crash window."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    conv = f"running_tool_{uuid.uuid4().hex}"
    call_id = "call_resume_running"
    async with async_session() as db:
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="run the code",
        )
        anchor_id = anchor.id
        anchor.created_at = datetime.now(timezone.utc) - timedelta(seconds=2)
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "execute_code_aio",
                        "call_id": call_id,
                        "args": {
                            "language": "bash",
                            "code": "echo recovered",
                            "execution_mode": "foreground",
                        },
                        "status": "running",
                        "result": "",
                        "assistant_content": "I am running the requested code.",
                        "recovery_prefix_messages": [
                            {"role": "assistant", "content": "partial before limit"},
                            {"role": "user", "content": "continue exactly"},
                        ],
                        "turn_anchor_id": str(anchor_id),
                    },
                    ensure_ascii=False,
                ),
                conversation_id=conv,
                created_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        await db.commit()

    executed = []

    async def fake_execute_tool(name, args, **kwargs):
        executed.append((name, args, kwargs))
        return "recovered\n"

    captured = {}

    async def fake_call_agent_llm(*_args, **kwargs):
        captured.update(kwargs)
        return "reply after recovered tool"

    async def fake_deliver(*_args, **_kwargs):
        return True

    monkeypatch.setattr(turn_recovery, "execute_tool", fake_execute_tool, raising=False)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert executed == []
    assert [msg["role"] for msg in captured["history"]] == [
        "user", "assistant", "user", "assistant", "tool"
    ]
    assert captured["history"][1]["content"] == "partial before limit"
    assert captured["history"][2]["content"] == "continue exactly"
    assert captured["history"][3]["content"] == "I am running the requested code."
    assert captured["history"][3]["tool_calls"][0]["function"]["name"] == "execute_code_aio"
    assert captured["history"][3]["tool_calls"][0]["id"] == call_id
    assert "Recovery blocked automatic replay" in captured["history"][4]["content"]

    async with async_session() as db:
        payloads = [
            json.loads(row.content)
            for row in (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conv, ChatMessage.role == "tool_call")
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            )
            .scalars()
            .all()
        ]
    assert [payload["status"] for payload in payloads] == ["running", "done"]
    assert payloads[1]["call_id"] == call_id
    assert "Recovery blocked automatic replay" in payloads[1]["result"]
    assert payloads[1]["assistant_content"] == "I am running the requested code."
    assert payloads[1]["recovery_prefix_messages"][0]["content"] == "partial before limit"
    async with async_session() as db:
        replies = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == conv,
                        ChatMessage.role == "assistant",
                        ChatMessage.content == "reply after recovered tool",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(replies) == 1


async def test_resume_turn_replays_only_tools_after_current_anchor(monkeypatch):
    """Restart must not replay unfinished tools belonging to an older turn."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    conv = f"current_anchor_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        old_anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="old interrupted turn",
        )
        old_anchor.created_at = now - timedelta(seconds=4)
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "read_file",
                        "call_id": "old_call",
                        "args": {"path": "old.txt"},
                        "status": "running",
                        "result": "",
                    }
                ),
                conversation_id=conv,
                message_meta={"turn_anchor_id": str(old_anchor.id)},
                created_at=now - timedelta(seconds=3),
            )
        )
        current_anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="current interrupted turn",
        )
        current_anchor_id = current_anchor.id
        current_anchor.created_at = now - timedelta(seconds=2)
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "read_file",
                        "call_id": "current_call",
                        "args": {"path": "current.txt"},
                        "status": "running",
                        "result": "",
                    }
                ),
                conversation_id=conv,
                message_meta={"turn_anchor_id": str(current_anchor_id)},
                created_at=now - timedelta(seconds=1),
            )
        )
        await db.commit()

    executed: list[str] = []

    async def fake_execute_tool(_name, args, **_kwargs):
        executed.append(args["path"])
        return f"read {args['path']}"

    async def fake_call_agent_llm(*_args, **_kwargs):
        return "current turn recovered"

    async def fake_deliver(**_kwargs):
        return True

    monkeypatch.setattr(turn_recovery, "execute_tool", fake_execute_tool, raising=False)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver)

    async with async_session() as db:
        anchor = await db.get(ChatMessage, current_anchor_id)

    assert await turn_recovery.resume_turn(anchor) is True
    assert executed == []

    async with async_session() as db:
        tool_rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conv, ChatMessage.role == "tool_call")
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            )
            .scalars()
            .all()
        )
    done_rows = [row for row in tool_rows if json.loads(row.content)["status"] == "done"]
    assert len(done_rows) == 1
    assert json.loads(done_rows[0].content)["call_id"] == "current_call"
    assert done_rows[0].message_meta["turn_anchor_id"] == str(current_anchor_id)


async def test_resume_turn_never_reexecutes_ambiguous_external_send(monkeypatch):
    """Crash recovery fails closed instead of duplicating an external send."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    conv = f"unsafe_running_tool_{uuid.uuid4().hex}"
    call_id = "call_unsafe_running"
    async with async_session() as db:
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="send this externally",
        )
        anchor_id = anchor.id
        anchor.created_at = datetime.now(timezone.utc) - timedelta(seconds=2)
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "send_feishu_message",
                        "call_id": call_id,
                        "args": {"open_id": "ou_x", "text": "hello"},
                        "status": "running",
                        "result": "",
                    },
                    ensure_ascii=False,
                ),
                conversation_id=conv,
                created_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        await db.commit()

    executed = []

    async def fake_execute_tool(name, args, **kwargs):
        executed.append((name, args, kwargs))
        return "sent ok"

    captured = {}

    async def fake_call_agent_llm(*_args, **kwargs):
        captured.update(kwargs)
        return "continued after recovered send"

    async def fake_deliver(*_args, **_kwargs):
        return True

    monkeypatch.setattr(turn_recovery, "execute_tool", fake_execute_tool, raising=False)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert executed == []
    assert [msg["role"] for msg in captured["history"]] == ["user", "assistant", "tool"]
    assert captured["history"][1]["tool_calls"][0]["function"]["name"] == "send_feishu_message"
    assert captured["history"][1]["tool_calls"][0]["id"] == call_id
    assert "Recovery blocked automatic replay" in captured["history"][2]["content"]

    async with async_session() as db:
        payloads = [
            json.loads(row.content)
            for row in (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conv, ChatMessage.role == "tool_call")
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            )
            .scalars()
            .all()
        ]
    assert [payload["status"] for payload in payloads] == ["running", "done"]
    assert payloads[1]["call_id"] == call_id
    assert "Recovery blocked automatic replay" in payloads[1]["result"]


@pytest.mark.parametrize("channel", ["dingtalk", "web"])
async def test_pending_terminal_delivery_survives_next_turn_without_model_replay(monkeypatch, channel):
    from app.services.chat_history import ingest_incoming_chat_message, persist_assistant_reply_row
    from app.services.im_delivery import IMDeliveryResult, IMDeliveryPart, attach_delivery_to_meta
    from app.services.turn_recovery_scanner import _load_recoverable_anchors
    from app.services.turn_recovery_startup import resume_startup_anchor
    from app.services import turn_runtime, turn_recovery

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(agent_id=agent_id, user_id=user_id, title="Delivery recovery",
                              source_channel=channel, external_conv_id="dingtalk_group_original" if channel == "dingtalk" else None)
        db.add(session)
        await db.flush()
        original = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id,
            content="original input", source_channel=channel,
        )
        await db.commit()
        final_id = await persist_assistant_reply_row(
            db, agent_id=agent_id, user_id=user_id, conversation_id=str(session.id),
            content="committed reply", turn_anchor_id=original.message.id,
            message_meta=attach_delivery_to_meta({}, IMDeliveryResult.pending(channel)),
        )
        await db.commit()
        following = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id,
            content="next input", source_channel=channel,
        )
        await db.commit()
        candidates = await _load_recoverable_anchors(db, include_legacy=False)
        final = next(row for row in candidates if row.id == final_id)
        following_id = following.message.id
        session_id = session.id

    sends = []
    async def provider_send(*, runtime, message, **_kwargs):
        sends.append((runtime.external_conv_id, message))
        return IMDeliveryResult.sent("dingtalk", IMDeliveryPart("dingtalk_openapi", "remote-1"))

    async def forbidden_model(*_args, **_kwargs):
        raise AssertionError("a committed reply must never re-enter the model")

    async def publish_web(_agent, _session, payload):
        sends.append(("web", payload["content"]))

    from app.api.websocket import manager
    monkeypatch.setattr(manager, "send_to_session", publish_web)
    monkeypatch.setattr(turn_runtime, "deliver_message_with_receipt", provider_send)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", forbidden_model)
    assert await resume_startup_anchor(final)
    assert not await resume_startup_anchor(final)
    assert sends == [("dingtalk_group_original" if channel == "dingtalk" else "web", "committed reply")]
    async with async_session() as db:
        stored = await db.get(ChatMessage, final_id)
        assert stored.message_meta["delivery"]["status"] == "sent"
        session = await db.get(ChatSession, session_id)
        assert session.im_config["conversation_turn"]["turn_anchor_id"] == str(following_id)
        assert session.im_config["conversation_turn"]["status"] == "running"


async def test_background_web_terminal_receipt_remains_owned_by_trigger(monkeypatch):
    from app.api.websocket import manager
    from app.services import background_turns, turn_recovery
    from app.services.chat_history import ingest_incoming_chat_message, persist_assistant_reply_row
    from app.services.turn_recovery_scanner import _load_recoverable_anchors
    from app.services.turn_recovery_startup import resume_startup_anchor

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(agent_id=agent_id, user_id=user_id, title="Background Web",
                              source_channel="web")
        db.add(session)
        await db.flush()
        admitted = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id,
            content="on_message event", source_channel="web",
        )
        anchor = admitted.message
        await background_turns.initialize_background_turn(
            db, session=session, anchor=anchor, kind="trigger", reference_id=uuid.uuid4(),
            completion={"origin": True, "triggers": [{"type": "on_message", "config": {}}]},
        )
        await db.commit()
        reply_id = await persist_assistant_reply_row(
            db, agent_id=agent_id, user_id=user_id, conversation_id=str(session.id),
            content="background reply", turn_anchor_id=anchor.id,
        )
        reply = await db.get(ChatMessage, reply_id)
        await background_turns._finalize(db, anchor, reply)
        await db.commit()
        assert reply.message_meta["delivery"]["status"] == "pending"
        assert not anchor.message_meta["background_execution"]["delivered"]
        anchor_id = anchor.id

    events = []
    async def publish(_agent, _session, payload):
        events.append(payload)

    async def forbidden_model(*_args, **_kwargs):
        raise AssertionError("terminal background delivery must not rerun the model")

    monkeypatch.setattr(manager, "send_to_session", publish)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", forbidden_model)
    async with async_session() as db:
        candidates = await _load_recoverable_anchors(db, include_legacy=False)
        candidate = next(row for row in candidates if row.id == anchor_id)
    assert await resume_startup_anchor(candidate)
    assert len(events) == 1
    assert events[0]["message_id"] == str(reply_id)
    async with async_session() as db:
        current = await db.get(ChatMessage, anchor_id)
        reply = await db.get(ChatMessage, reply_id)
        assert current.message_meta["background_execution"]["delivered"] is True
        assert reply.message_meta["delivery"]["status"] == "sent"
        candidates = await _load_recoverable_anchors(db, include_legacy=False)
        assert not any(row.id in {anchor_id, reply_id} for row in candidates)
    assert await resume_startup_anchor(current)
    assert len(events) == 1
