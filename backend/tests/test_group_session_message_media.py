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


async def test_send_channel_file_exact_session_timeout_persists_unknown_without_retry(
    tmp_path,
    monkeypatch,
):
    owner, _ = await _seed_agents()
    origin = await _seed_session(owner.id, channel="web", is_group=True)
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_group_timeout-file",
        is_group=True,
    )
    call_id = "file-exact-timeout"
    turn_anchor_id = uuid.uuid4()
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=f"ding-file-app-{uuid.uuid4().hex}",
                app_secret="ding-file-secret",
                is_configured=True,
            )
        )
        row = ChatMessage(
            agent_id=owner.id,
            user_id=origin.user_id,
            role="tool_call",
            content=json.dumps({
                "name": "send_channel_file",
                "call_id": call_id,
                "args": {
                    "file_path": "workspace/report.pdf",
                    "session_id": str(target.id),
                },
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
    report.write_bytes(b"%PDF-1.4 timeout")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    provider_calls = []

    async def fake_upload(*_args, **_kwargs):
        return "media-id"

    async def timeout_send(*_args, **_kwargs):
        provider_calls.append(True)
        raise httpx.ReadTimeout("provider response timed out")

    monkeypatch.setattr(
        "app.services.dingtalk_stream._upload_dingtalk_media",
        fake_upload,
    )
    monkeypatch.setattr(
        "app.services.dingtalk_stream._send_dingtalk_media_message",
        timeout_send,
    )

    result = await agent_tools._send_channel_file(
        owner.id,
        workspace,
        {
            "file_path": "workspace/report.pdf",
            "session_id": str(target.id),
        },
        tool_call_id=call_id,
        origin_session_id=str(origin.id),
        origin_turn_anchor_id=turn_anchor_id,
    )

    assert "Failed to send file" in result
    assert provider_calls == [True]
    async with async_session() as db:
        stored = await db.get(ChatMessage, row_id)
    assert stored.message_meta["delivery"]["status"] == "unknown"
    assert stored.message_meta["delivery"]["error"] == "ReadTimeout"


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
