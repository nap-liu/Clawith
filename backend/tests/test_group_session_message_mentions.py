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

@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}])
async def test_session_message_rejects_non_boolean_mention_all(value):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)

    result = await agent_tools._send_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "不应发送",
            "mention_all": value,
        },
    )

    assert result == "❌ mention_all must be a boolean"


async def test_session_message_rejects_conflicting_native_mentions():
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)

    result = await agent_tools._send_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "不应发送",
            "mention_all": True,
            "mention_user_ids": [str(uuid.uuid4())],
        },
    )

    assert result == "❌ mention_all=true cannot be combined with mention_user_ids"


@pytest.mark.parametrize(
    ("channel", "is_group"),
    [("dingtalk", False), ("feishu", True)],
)
async def test_session_message_rejects_unsupported_mention_all_before_persist(
    monkeypatch,
    channel,
    is_group,
):
    owner, _ = await _seed_agents()
    user_id = owner.creator_id if not is_group else None
    target = await _seed_session(
        owner.id,
        channel=channel,
        external_conv_id=f"{channel}_mention-all-boundary",
        is_group=is_group,
        user_id=user_id,
    )

    async def fail_if_delivered(**_kwargs):
        raise AssertionError("unsupported mention must fail before delivery")

    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fail_if_delivered)
    result = await agent_tools._send_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "不应发送",
            "mention_all": True,
        },
    )

    assert result == "❌ 原生 @ 当前仅支持钉钉群 Session。"
    async with async_session() as db:
        receipts = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.conversation_id == str(target.id))
            )
        ).scalars().all()
    assert receipts == []


async def test_dingtalk_group_mention_does_not_require_recent_group_webhook(monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(owner.id)
    canonical_user_id = str(uuid.uuid4())
    delivered: list[dict] = []

    async def fake_prepare(*_args, **_kwargs):
        return ["staff-zhangsan"], ["张三"]

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return IMDeliveryResult.unsupported_delivery(
            "dingtalk",
            "dingtalk_interactive_card",
        )

    monkeypatch.setattr(agent_tools, "prepare_group_user_mentions", fake_prepare)
    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fake_deliver)
    result = await agent_tools._send_group_session_message(
        owner.id,
        {
            "session_id": str(target.id),
            "message": "请确认",
            "mention_user_ids": [canonical_user_id],
        },
    )

    assert json.loads(result)["status"] == "sent"
    assert delivered[0]["mention"] == MentionIntent(
        scope="users",
        target_ids=("staff-zhangsan",),
        target_names=("张三",),
    )


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
        "raise_on_transport_error": True,
    }


async def test_dingtalk_file_caption_uses_durable_proactive_session_route(
    tmp_path,
    monkeypatch,
):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        external_conv_id="dingtalk_group_open-conversation-file",
    )
    app_id = f"ding-file-app-{uuid.uuid4().hex}"
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=app_id,
                app_secret="ding-file-secret",
                is_configured=True,
            )
        )
        await db.commit()
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4 durable file route")
    captured = {}

    async def fake_upload(*args, **kwargs):
        captured["upload"] = (args, kwargs)
        return "media-id"

    async def fake_media_send(*args, **kwargs):
        captured["media"] = (args, kwargs)
        return True

    async def fake_caption(**kwargs):
        captured["caption"] = kwargs
        return {"errcode": 0, "processQueryKey": "caption-process-key"}

    monkeypatch.setattr(
        "app.services.dingtalk_stream._upload_dingtalk_media",
        fake_upload,
    )
    monkeypatch.setattr(
        "app.services.dingtalk_stream._send_dingtalk_media_message",
        fake_media_send,
    )
    monkeypatch.setattr(
        turn_runtime,
        "send_dingtalk_proactive_markdown",
        fake_caption,
    )

    delivery_text, delivery_result = await agent_tools._send_file_to_session(
        owner.id,
        report,
        str(target.id),
        "文件说明",
    )
    expected_target_id = target.external_conv_id.removeprefix("dingtalk_group_")

    assert delivery_text == f"File 'report.pdf' sent to Session {target.id} via DingTalk."
    assert delivery_result.ok is True
    assert [part.artifact_role for part in delivery_result.parts] == [
        "channel_file",
        "file_caption",
    ]
    assert captured["media"][0][2:6] == (
        expected_target_id,
        "media-id",
        "file",
        "2",
    )
    assert captured["caption"] == {
        "app_id": app_id,
        "app_secret": "ding-file-secret",
        "target_id": expected_target_id,
        "is_group": True,
        "message": "文件说明",
    }


async def test_send_file_exact_session_rejects_cross_agent_and_archived_routes(tmp_path):
    owner, other = await _seed_agents()
    target = await _seed_session(owner.id)
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4 authorization")

    cross_agent_text, cross_agent_result = await agent_tools._send_file_to_session(
        other.id,
        report,
        str(target.id),
    )
    cross_agent_payload = json.loads(cross_agent_text)
    assert cross_agent_payload["code"] == "session_not_found_or_forbidden"
    assert cross_agent_result.status == "failed"

    archived = await _seed_session(
        owner.id,
        external_conv_id="dingtalk_group_old__archived_20260825",
    )
    archived_text, archived_result = await agent_tools._send_file_to_session(
        owner.id,
        report,
        str(archived.id),
    )
    archived_payload = json.loads(archived_text)
    assert archived_payload["code"] == "session_route_unavailable"
    assert archived_result.status == "failed"


async def test_send_file_exact_session_rejects_person_group_route_mismatch(tmp_path):
    owner, _ = await _seed_agents()
    recipient = await _seed_related_user(owner)
    target = await _seed_session(
        owner.id,
        external_conv_id="dingtalk_group_wrong-kind",
        is_group=False,
        user_id=recipient.id,
    )
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
        await db.commit()
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4 route mismatch")

    delivery_text, delivery_result = await agent_tools._send_file_to_session(
        owner.id,
        report,
        str(target.id),
    )

    payload = json.loads(delivery_text)
    assert payload["code"] == "session_route_mismatch"
    assert delivery_result.status == "failed"


async def test_send_file_targets_exact_dingtalk_person_session(tmp_path, monkeypatch):
    owner, _ = await _seed_agents()
    recipient = await _seed_related_user(owner)
    target = await _seed_session(
        owner.id,
        external_conv_id="dingtalk_p2p_staff-person",
        is_group=False,
        user_id=recipient.id,
    )
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
        await db.commit()
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4 person route")
    captured = {}

    async def fake_upload(*_args, **_kwargs):
        return "person-media-id"

    async def fake_media_send(*args, **kwargs):
        captured["args"] = args
        await kwargs["on_result"]({"processQueryKey": "person-file-process-key"})
        return True

    monkeypatch.setattr(
        "app.services.dingtalk_stream._upload_dingtalk_media",
        fake_upload,
    )
    monkeypatch.setattr(
        "app.services.dingtalk_stream._send_dingtalk_media_message",
        fake_media_send,
    )

    delivery_text, delivery_result = await agent_tools._send_file_to_session(
        owner.id,
        report,
        str(target.id),
    )

    expected_target = target.external_conv_id.removeprefix("dingtalk_p2p_")
    assert delivery_text == f"File 'report.pdf' sent to Session {target.id} via DingTalk."
    assert captured["args"][2:6] == (
        expected_target,
        "person-media-id",
        "file",
        "1",
    )
    assert delivery_result.parts[0].provider_message_id == "person-file-process-key"


@pytest.mark.parametrize("failure_mode", ["business_error", "exception"])
async def test_dingtalk_file_caption_failure_is_reported_as_partial(
    tmp_path,
    monkeypatch,
    failure_mode,
):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        external_conv_id="dingtalk_group_open-conversation-caption-partial",
    )
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
        await db.commit()
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4 partial caption")

    async def fake_upload(*_args, **_kwargs):
        return "media-id"

    async def fake_media_send(*_args, **_kwargs):
        return True

    async def fake_caption(**_kwargs):
        if failure_mode == "exception":
            raise httpx.ReadTimeout("caption transport unavailable")
        return {"errcode": 40035, "errmsg": "caption rejected"}

    monkeypatch.setattr(
        "app.services.dingtalk_stream._upload_dingtalk_media",
        fake_upload,
    )
    monkeypatch.setattr(
        "app.services.dingtalk_stream._send_dingtalk_media_message",
        fake_media_send,
    )
    monkeypatch.setattr(turn_runtime, "send_dingtalk_proactive_markdown", fake_caption)

    delivery_text, delivery_result = await agent_tools._send_file_to_session(
        owner.id,
        report,
        str(target.id),
        "文件说明",
    )

    expected_status = "unknown" if failure_mode == "exception" else "partial"
    expected_text = (
        "caption delivery is uncertain"
        if failure_mode == "exception"
        else "caption failed"
    )
    assert expected_text in delivery_text
    assert delivery_result.ok is False
    assert delivery_result.status == expected_status
    assert delivery_result.error.startswith("caption_failed:")
    assert [part.artifact_role for part in delivery_result.parts] == ["channel_file"]

    async with async_session() as db:
        receipt = ChatMessage(
            agent_id=owner.id,
            role="assistant",
            conversation_id=str(target.id),
            content="文件说明",
            message_meta=agent_tools.attach_delivery_to_meta(
                {},
                IMDeliveryResult.pending("dingtalk"),
            ),
        )
        db.add(receipt)
        await db.commit()
        receipt_id = receipt.id
    assert await agent_tools.register_delivery(receipt_id, delivery_result)
    async with async_session() as db:
        stored = await db.get(ChatMessage, receipt_id)
    assert stored.message_meta["delivery"]["status"] == "partial"
    assert bool(stored.message_meta["delivery"].get("uncertain")) is (
        failure_mode == "exception"
    )


async def test_send_video_targets_exact_group_session_with_custom_cover(
    tmp_path, monkeypatch
):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_group_media-room",
        is_group=True,
    )
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=owner.id,
                channel_type="dingtalk",
                app_id=f"ding-media-{uuid.uuid4().hex}",
                app_secret="ding-secret",
                is_configured=True,
            )
        )
        await db.commit()
    video = tmp_path / "demo.mp4"
    cover = tmp_path / "cover.png"
    video.write_bytes(b"video")
    cover.write_bytes(b"cover")
    calls = []

    async def fake_video(app_id, app_secret, target_id, file_path, conversation_type, **kwargs):
        calls.append({
            "target_id": target_id,
            "file_path": file_path,
            "conversation_type": conversation_type,
            "cover": kwargs.get("cover_image_path"),
        })
        await kwargs["on_result"]({"processQueryKey": "group-video-process-key"})
        return True, "MEDIA_SENT"

    async def fake_caption(**_kwargs):
        return IMDeliveryResult.sent("dingtalk")

    live_events = []

    async def fake_live_mirror(*args, **_kwargs):
        live_events.append(args[-1])

    monkeypatch.setattr(
        "app.services.dingtalk_stream._send_dingtalk_native_video",
        fake_video,
    )
    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fake_caption)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)
    kwargs = {
        "agent_id": owner.id,
        "session_id": str(target.id),
        "file_path": video,
        "workspace_path": "workspace/demo.mp4",
        "media_kind": "video",
        "caption": "群视频",
        "cover_path": cover,
        "intent_id": "media-group-call",
        "origin_session_id": str(uuid.uuid4()),
        "origin_turn_anchor_id": uuid.uuid4(),
        "tool_args": {
            "media_type": "video",
            "file_path": "workspace/demo.mp4",
            "title": "示例媒体标题",
        },
    }

    first = json.loads(await agent_tools._send_media_to_session(**kwargs))
    second = json.loads(await agent_tools._send_media_to_session(**kwargs))

    assert first["status"] == "sent"
    assert first["conversation_type"] == "group"
    assert first["session_id"] == str(target.id)
    assert first["title"] == "示例媒体标题"
    assert second["status"] == "already_sent"
    assert second["title"] == "示例媒体标题"
    assert calls == [{
        "target_id": target.external_conv_id.removeprefix("dingtalk_group_"),
        "file_path": video,
        "conversation_type": "2",
        "cover": cover,
    }]
    assert len(live_events) == 2
    assert live_events[0]["type"] == "tool_call"
    assert live_events[0]["name"] == "send_media"
    assert live_events[0]["call_id"] == "media-group-call"
    render_result = json.loads(live_events[0]["result"])
    assert render_result["type"] == "platform_media_delivery"
    assert render_result["path"] == "workspace/demo.mp4"
    assert render_result["title"] == "示例媒体标题"
    assert render_result["allow_download"] is False
    assert live_events[1]["type"] == "assistant_message_committed"
    assert live_events[1]["content"] == "群视频"
    assert live_events[1]["attachments"] == []
    async with async_session() as db:
        rows = list((
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(target.id),
                    ChatMessage.external_event_key.is_not(None),
                ).order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            )
        ).scalars().all())
    assert len(rows) == 2
    receipt = next(row for row in rows if row.message_meta.get("media_kind") == "video")
    caption_row = next(row for row in rows if row.message_meta.get("media_caption_for"))
    assert receipt.role == "tool_call"
    assert caption_row.content == "群视频"
    assert caption_row.message_meta["attachments"] == []
    assert caption_row.message_meta["media_caption_for"] == str(receipt.id)
    assert receipt.message_meta["attachments"] == [{
        "display_name": "demo.mp4",
        "path": "workspace/demo.mp4",
        "kind": "video",
        "mime_type": "video/mp4",
        "size_bytes": 5,
    }]
    assert receipt.message_meta["target_is_group"] is True
    assert receipt.message_meta["display_title"] == "示例媒体标题"
    assert receipt.message_meta["delivery_claim"] is True
    assert receipt.message_meta["delivery"]["status"] == "sent"
    assert receipt.message_meta["delivery"]["parts"][0]["provider_message_id"] == (
        "group-video-process-key"
    )
    assert receipt.message_meta["delivery"]["parts"][0]["recall_status"] == "available"
    assert "delivery_claim" not in caption_row.message_meta
    stored_render = json.loads(json.loads(receipt.content)["result"])
    assert stored_render["message_id"] == str(receipt.id)
    assert stored_render["allow_download"] is False
    assert stored_render["title"] == "示例媒体标题"


async def test_send_media_caption_failure_is_preserved_on_replay(tmp_path, monkeypatch):
    owner, _ = await _seed_agents()
    target = await _seed_session(
        owner.id,
        channel="dingtalk",
        external_conv_id="dingtalk_group_caption-failure",
        is_group=True,
    )
    async with async_session() as db:
        db.add(ChannelConfig(
            agent_id=owner.id,
            channel_type="dingtalk",
            app_id=f"ding-caption-{uuid.uuid4().hex}",
            app_secret="ding-secret",
            is_configured=True,
        ))
        await db.commit()

    video = tmp_path / "caption.mp4"
    video.write_bytes(b"video")
    provider_calls = []

    async def fake_video(*_args, **_kwargs):
        provider_calls.append(True)
        return True, "MEDIA_SENT"

    async def fail_caption(**_kwargs):
        return IMDeliveryResult.failed("dingtalk", "caption_failed")

    async def fake_live_mirror(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.dingtalk_stream._send_dingtalk_native_video",
        fake_video,
    )
    monkeypatch.setattr(agent_tools, "deliver_message_with_receipt", fail_caption)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", fake_live_mirror)
    kwargs = {
        "agent_id": owner.id,
        "session_id": str(target.id),
        "file_path": video,
        "workspace_path": "workspace/caption.mp4",
        "media_kind": "video",
        "caption": "应当失败的说明",
        "cover_path": None,
        "intent_id": "caption-failure-call",
        "origin_session_id": str(uuid.uuid4()),
        "origin_turn_anchor_id": uuid.uuid4(),
    }

    first = json.loads(await agent_tools._send_media_to_session(**kwargs))
    replay = json.loads(await agent_tools._send_media_to_session(**kwargs))

    assert first["status"] == "sent"
    assert first["code"] == "MEDIA_SENT_CAPTION_FAILED"
    assert "说明文字发送失败" in first["message"]
    assert "do not resend the media" in first["agent_action"]
    assert replay["status"] == "already_sent"
    assert replay["code"] == "MEDIA_SENT_CAPTION_FAILED"
    assert replay["caption_status"] == "failed"
    assert replay["message"] == first["message"]
    assert provider_calls == [True]
