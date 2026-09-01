"""Mechanical continuation of exact-Session delivery contract tests."""

import pytest

from tests.test_group_session_message import (
    Agent,
    AgentRelationship,
    AgentTool,
    ChannelConfig,
    ChatMessage,
    ChatSession,
    DeliveryReceiptPersistenceError,
    IMDeliveryPart,
    IMDeliveryResult,
    Identity,
    MentionIntent,
    SimpleNamespace,
    Tenant,
    Tool,
    TurnRuntime,
    User,
    _isolate_messages_and_engine,
    _seed_agents,
    _seed_related_user,
    _seed_session,
    agent_tools,
    asyncio,
    async_session,
    create_async_engine,
    datetime,
    delete,
    engine,
    httpx,
    json,
    seed_builtin_tools,
    select,
    text,
    timedelta,
    timezone,
    turn_runtime,
    uuid,
)

pytestmark = pytest.mark.asyncio

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
                "reasoning_content": "typed reasoning",
                "assistant_content": "I will send the requested video.",
                "recovery_prefix_messages": [
                    {"role": "assistant", "content": "partial media plan"},
                    {"role": "user", "content": "continue exactly"},
                ],
                "round_id": "round-media-1",
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
                "reasoning_content": "typed reasoning",
                "assistant_content": "I will send the requested video.",
                "recovery_prefix_messages": [
                    {"role": "assistant", "content": "partial media plan"},
                    {"role": "user", "content": "Please continue."},
                ],
                "round_id": "round-media-1",
                "round_tool_index": 0,
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
    stored_tool = json.loads(rows[0].content)
    assert stored_tool["status"] == "done"
    assert stored_tool["reasoning_content"] == "typed reasoning"
    assert stored_tool["assistant_content"] == "I will send the requested video."
    assert stored_tool["recovery_prefix_messages"][0]["content"] == "partial media plan"
    assert stored_tool["round_id"] == "round-media-1"
    assert rows[0].message_meta["delivery_claim"] is False
    assert rows[1].message_meta["media_caption_for"] == str(running_id)
    assert "delivery_claim" not in rows[1].message_meta
    assert rows[0].created_at <= rows[1].created_at
