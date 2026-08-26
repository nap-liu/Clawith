import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import agent_tools
from app.services.llm import caller as llm_caller
from app.services.media_tool_contract import (
    MAX_MEDIA_DISPLAY_TITLE_LENGTH,
    SEND_MEDIA_DESCRIPTION,
    SEND_MEDIA_PARAMETERS_SCHEMA,
    normalize_media_display_title,
)
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.tool_seeder import BUILTIN_TOOLS
from app.services.im_delivery import IMDeliveryPart, IMDeliveryResult
from app.services.im_delivery import DeliveryReceiptPersistenceError


@pytest.mark.asyncio
async def test_send_channel_file_web_fallback_returns_platform_delivery_json(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    workspace = tmp_path / str(agent_id)
    report = workspace / "workspace" / "report.pdf"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"%PDF-1.4 test")

    result = await agent_tools._send_channel_file(
        agent_id,
        workspace,
        {
            "file_path": "workspace/report.pdf",
            "message": "这是报告",
        },
    )

    payload = json.loads(result)
    assert payload == {
        "type": "platform_file_delivery",
        "path": "workspace/report.pdf",
        "filename": "report.pdf",
        "message": "这是报告",
        "mime_type": "application/pdf",
        "size": len(b"%PDF-1.4 test"),
    }


@pytest.mark.asyncio
async def test_channel_file_claims_pending_before_sender_and_finalizes_receipt(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    workspace = tmp_path / str(agent_id)
    report = workspace / "workspace" / "report.pdf"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"%PDF-1.4 test")
    receipt_id = uuid.uuid4()
    events = []

    async def fake_claim(**kwargs):
        events.append(("claim", kwargs["tool_call_id"]))
        return receipt_id

    async def fake_register(message_id, result):
        events.append(("finalize", message_id, result.status))
        return True

    async def no_exact_session_route(_agent_id, _session_id):
        return False

    async def file_sender(_path, _message):
        events.append(("send",))
        return IMDeliveryResult.sent(
            "slack",
            IMDeliveryPart(
                transport="slack_file",
                provider_message_id="F1",
                conversation_ref="D1",
                recallable=False,
            ),
        )

    monkeypatch.setattr(agent_tools, "_claim_channel_file_receipt", fake_claim)
    monkeypatch.setattr(agent_tools, "register_delivery", fake_register)
    monkeypatch.setattr(
        agent_tools,
        "_supports_exact_file_session_route",
        no_exact_session_route,
    )
    token = agent_tools.channel_file_sender.set(file_sender)
    try:
        result = await agent_tools._send_channel_file(
            agent_id,
            workspace,
            {"file_path": "workspace/report.pdf"},
            tool_call_id="call-file-1",
            origin_session_id=str(uuid.uuid4()),
        )
    finally:
        agent_tools.channel_file_sender.reset(token)

    assert "sent to user" in result
    assert events == [
        ("claim", "call-file-1"),
        ("send",),
        ("finalize", receipt_id, "sent"),
    ]


@pytest.mark.asyncio
async def test_feishu_receipt_failure_after_provider_success_does_not_send_fallback(
    tmp_path,
    monkeypatch,
):
    from unittest.mock import AsyncMock

    from app.services.feishu_service import feishu_service

    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4 test")
    provider_calls = 0

    async def upload_and_send(*_args, on_result, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        await on_result(
            "channel_file",
            {"code": 0, "data": {"message_id": "provider-file-id"}},
        )

    async def fail_receipt(_part):
        raise RuntimeError("database unavailable")

    fallback = AsyncMock()
    monkeypatch.setattr(feishu_service, "upload_and_send_file", upload_and_send)
    monkeypatch.setattr(feishu_service, "send_message", fallback)
    recorder_token = agent_tools.channel_file_part_recorder.set(fail_receipt)
    try:
        with pytest.raises(DeliveryReceiptPersistenceError):
            await agent_tools._send_file_via_feishu(
                uuid.uuid4(),
                SimpleNamespace(app_id="app", app_secret="secret"),
                report,
                SimpleNamespace(external_id="user-1", open_id=None),
                "Recipient",
                "caption",
            )
    finally:
        agent_tools.channel_file_part_recorder.reset(recorder_token)

    assert provider_calls == 1
    fallback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_path",
    [
        "../secret.txt",
        "/etc/passwd",
        "https://evil.example/file.txt",
        "C:/Users/secret.txt",
        "workspace\\..\\secret.txt",
    ],
)
async def test_send_channel_file_rejects_unsafe_relative_path(tmp_path, monkeypatch, file_path):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)

    result = await agent_tools._send_channel_file(
        agent_id,
        workspace,
        {"file_path": file_path},
    )

    assert result == "Error: Invalid file_path"


@pytest.mark.asyncio
async def test_send_channel_file_rejects_audio_and_points_to_specialized_tool(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    workspace = tmp_path / str(agent_id)
    audio = workspace / "workspace" / "briefing.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ID3-test")

    payload = json.loads(await agent_tools._send_channel_file(
        agent_id, workspace, {"file_path": "workspace/briefing.mp3"}
    ))

    assert payload["status"] == "failed"
    assert payload["code"] == "WRONG_MEDIA_TOOL"
    assert payload["media_kind"] == "audio"


@pytest.mark.asyncio
async def test_send_channel_file_routes_exact_session_before_current_context(
    tmp_path,
    monkeypatch,
):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    workspace = tmp_path / str(agent_id)
    report = workspace / "workspace" / "report.pdf"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"%PDF-1.4 exact session")
    target_session_id = str(uuid.uuid4())
    receipt_id = uuid.uuid4()
    captured = {}

    async def fake_session_send(*args):
        captured["args"] = args
        return "sent-to-exact-session", IMDeliveryResult.sent("dingtalk")

    async def fake_claim(**_kwargs):
        return receipt_id

    async def fake_register(candidate_id, result):
        assert candidate_id == receipt_id
        assert result.ok
        return True

    async def current_sender(*_args):
        raise AssertionError("explicit session_id must not use the current-session sender")

    monkeypatch.setattr(agent_tools, "_send_file_to_session", fake_session_send)
    monkeypatch.setattr(agent_tools, "_claim_channel_file_receipt", fake_claim)
    monkeypatch.setattr(agent_tools, "register_delivery", fake_register)
    token = agent_tools.channel_file_sender.set(current_sender)
    try:
        result = await agent_tools._send_channel_file(
            agent_id,
            workspace,
            {
                "file_path": "workspace/report.pdf",
                "session_id": target_session_id,
                "message": "请查收",
            },
            tool_call_id="call-exact-file",
            origin_session_id=str(uuid.uuid4()),
        )
    finally:
        agent_tools.channel_file_sender.reset(token)

    assert result == "sent-to-exact-session"
    assert captured["args"] == (
        agent_id,
        report,
        target_session_id,
        "请查收",
    )


@pytest.mark.asyncio
async def test_send_channel_file_routes_current_durable_session_before_legacy_sender(
    tmp_path,
    monkeypatch,
):
    agent_id = uuid.uuid4()
    current_session_id = str(uuid.uuid4())
    receipt_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    workspace = tmp_path / str(agent_id)
    report = workspace / "workspace" / "report.pdf"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"%PDF-1.4 durable current session")
    captured = {}

    async def supports_exact_route(candidate_agent_id, candidate_session_id):
        assert candidate_agent_id == agent_id
        assert candidate_session_id == current_session_id
        return True

    async def fake_session_send(*args):
        captured["args"] = args
        return "sent-to-current-session", IMDeliveryResult.sent("dingtalk")

    async def fake_claim(**_kwargs):
        return receipt_id

    async def fake_register(candidate_id, result):
        assert candidate_id == receipt_id
        assert result.ok
        return True

    async def legacy_sender(*_args):
        raise AssertionError("durable current Session must win over a legacy sender")

    monkeypatch.setattr(
        agent_tools,
        "_supports_exact_file_session_route",
        supports_exact_route,
    )
    monkeypatch.setattr(agent_tools, "_send_file_to_session", fake_session_send)
    monkeypatch.setattr(agent_tools, "_claim_channel_file_receipt", fake_claim)
    monkeypatch.setattr(agent_tools, "register_delivery", fake_register)
    token = agent_tools.channel_file_sender.set(legacy_sender)
    try:
        result = await agent_tools._send_channel_file(
            agent_id,
            workspace,
            {
                "file_path": "workspace/report.pdf",
                "message": "当前会话附件",
            },
            tool_call_id="call-current-file",
            origin_session_id=current_session_id,
        )
    finally:
        agent_tools.channel_file_sender.reset(token)

    assert result == "sent-to-current-session"
    assert captured["args"] == (
        agent_id,
        report,
        current_session_id,
        "当前会话附件",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_code"),
    [
        (
            {"session_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4())},
            "ambiguous_file_target",
        ),
        ({"session_id": str(uuid.uuid4()), "channel": "feishu"}, "channel_requires_user_target"),
    ],
)
async def test_send_channel_file_rejects_ambiguous_session_target(
    tmp_path,
    monkeypatch,
    arguments,
    expected_code,
):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    workspace = tmp_path / str(agent_id)
    report = workspace / "workspace" / "report.pdf"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"%PDF-1.4 target validation")

    payload = json.loads(
        await agent_tools._send_channel_file(
            agent_id,
            workspace,
            {"file_path": "workspace/report.pdf", **arguments},
        )
    )

    assert payload["code"] == expected_code


@pytest.mark.asyncio
async def test_send_file_to_session_rejects_non_uuid_without_database_access(
    tmp_path,
):
    delivery_text, delivery_result = await agent_tools._send_file_to_session(
        uuid.uuid4(),
        tmp_path / "report.pdf",
        "not-a-session-uuid",
    )
    payload = json.loads(
        delivery_text
    )

    assert payload["code"] == "session_not_found_or_forbidden"
    assert delivery_result.status == "failed"


def test_send_channel_file_schema_exposes_exact_session_target_consistently():
    runtime_schema = next(
        item["function"]["parameters"]
        for item in agent_tools.AGENT_TOOLS
        if item["function"]["name"] == "send_channel_file"
    )
    seeded_schema = next(
        item["parameters_schema"]
        for item in BUILTIN_TOOLS
        if item["name"] == "send_channel_file"
    )

    assert runtime_schema == seeded_schema
    assert runtime_schema["properties"]["session_id"]["type"] == "string"
    assert "Session UUID" in runtime_schema["properties"]["session_id"]["description"]


@pytest.mark.asyncio
async def test_send_media_without_current_or_explicit_session_fails_clearly(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    workspace = tmp_path / str(agent_id)
    audio = workspace / "workspace" / "briefing.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ID3-test")

    payload = json.loads(await agent_tools._send_channel_media(
        agent_id,
        workspace,
        {"file_path": "workspace/briefing.mp3", "message": "晨会录音"},
        media_kind="audio",
        tool_call_id="call-123",
    ))

    assert payload["type"] == "media_delivery_result"
    assert payload["status"] == "failed"
    assert payload["code"] == "SESSION_REQUIRED"
    assert payload["intent_id"] == "call-123"
    assert payload["media_kind"] == "audio"


@pytest.mark.asyncio
async def test_send_media_download_is_one_agent_config_not_a_call_argument(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    workspace = tmp_path / str(agent_id)
    audio = workspace / "workspace" / "briefing.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ID3-test")
    captured = {}

    async def fake_config(_agent_id, tool_name):
        assert tool_name == "send_media"
        return {"allow_download": True}

    async def fake_send_to_session(**kwargs):
        captured.update(kwargs)
        return json.dumps({"type": "platform_media_delivery", "status": "sent"})

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_config)
    monkeypatch.setattr(agent_tools, "_send_media_to_session", fake_send_to_session)

    payload = json.loads(await agent_tools._send_channel_media(
        agent_id,
        workspace,
        {
            "file_path": "workspace/briefing.mp3",
            "session_id": str(uuid.uuid4()),
        },
        media_kind="audio",
        tool_call_id="call-config",
    ))

    assert payload["status"] == "sent"
    assert captured["allow_download"] is True
    assert "allow_download" not in captured.get("arguments", {})


@pytest.mark.asyncio
async def test_legacy_file_sender_does_not_bypass_session_routing(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    workspace = tmp_path / str(agent_id)
    audio = workspace / "workspace" / "briefing.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ID3-test")

    async def file_sender(_path, _message):
        return None

    token = agent_tools.channel_file_sender.set(file_sender)
    try:
        payload = json.loads(await agent_tools._send_channel_media(
            agent_id,
            workspace,
            {"file_path": "workspace/briefing.mp3"},
            media_kind="audio",
            tool_call_id="call-im",
        ))
    finally:
        agent_tools.channel_file_sender.reset(token)

    assert payload["status"] == "failed"
    assert payload["code"] == "SESSION_REQUIRED"


@pytest.mark.asyncio
async def test_legacy_media_sender_does_not_bypass_session_routing(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    workspace = tmp_path / str(agent_id)
    video = workspace / "workspace" / "briefing.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42hdlr\x00\x00\x00\x00\x00\x00\x00\x00vide")
    received = []

    async def video_sender(path, message):
        received.append((path.name, message))

    token = agent_tools.channel_video_sender.set(video_sender)
    try:
        payload = json.loads(await agent_tools._send_channel_media(
            agent_id,
            workspace,
            {"file_path": "workspace/briefing.mp4", "message": "演示"},
            media_kind="video",
            tool_call_id="call-video",
        ))
    finally:
        agent_tools.channel_video_sender.reset(token)

    assert payload["status"] == "failed"
    assert payload["code"] == "SESSION_REQUIRED"
    assert received == []


@pytest.mark.asyncio
async def test_media_kind_is_checked_from_file_bytes(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    workspace = tmp_path / str(agent_id)
    renamed = workspace / "workspace" / "fake.mp4"
    renamed.parent.mkdir(parents=True)
    renamed.write_bytes(b"ID3-this-is-audio")

    payload = json.loads(await agent_tools._send_channel_media(
        agent_id,
        workspace,
        {"file_path": "workspace/fake.mp4", "session_id": str(uuid.uuid4())},
        media_kind="video",
        tool_call_id="call-mismatch",
    ))

    assert payload["status"] == "failed"
    assert payload["code"] == "MEDIA_KIND_MISMATCH"
    assert payload["actual_kind"] == "audio"


@pytest.mark.asyncio
async def test_send_media_accepts_any_file_under_current_agent_root(
    tmp_path,
    monkeypatch,
):
    agent_id = uuid.uuid4()
    workspace = tmp_path / str(agent_id)
    video = workspace / "exports" / "review" / "demo.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(
        b"\x00\x00\x00\x18ftypmp42hdlr\x00\x00\x00\x00\x00\x00\x00\x00vide"
    )
    captured = {}

    async def fake_config(_agent_id, _tool_name):
        return {}

    async def fake_send_to_session(**kwargs):
        captured.update(kwargs)
        return json.dumps({"type": "platform_media_delivery", "status": "sent"})

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_config)
    monkeypatch.setattr(agent_tools, "_send_media_to_session", fake_send_to_session)

    payload = json.loads(await agent_tools._send_channel_media(
        agent_id,
        workspace,
        {
            "file_path": "exports/review/demo.mp4",
            "session_id": str(uuid.uuid4()),
        },
        media_kind="video",
        tool_call_id="call-agent-root-file",
    ))

    assert payload["status"] == "sent"
    assert captured["file_path"] == video
    assert captured["workspace_path"] == "exports/review/demo.mp4"


@pytest.mark.asyncio
async def test_send_media_rejects_symlink_that_escapes_current_agent_root(tmp_path):
    agent_id = uuid.uuid4()
    workspace = tmp_path / str(agent_id)
    outside = tmp_path / "outside.mp4"
    workspace.mkdir(parents=True)
    outside.write_bytes(
        b"\x00\x00\x00\x18ftypmp42hdlr\x00\x00\x00\x00\x00\x00\x00\x00vide"
    )
    (workspace / "linked.mp4").symlink_to(outside)

    payload = json.loads(await agent_tools._send_channel_media(
        agent_id,
        workspace,
        {
            "file_path": "linked.mp4",
            "session_id": str(uuid.uuid4()),
        },
        media_kind="video",
        tool_call_id="call-symlink-escape",
    ))

    assert payload["status"] == "failed"
    assert payload["code"] == "INVALID_FILE_PATH"


@pytest.mark.asyncio
async def test_send_media_runtime_does_not_materialize_another_agent_symlink(
    tmp_path,
    monkeypatch,
):
    agent_id = uuid.uuid4()
    other_agent_id = uuid.uuid4()
    storage_root = tmp_path / "storage"
    agent_root = storage_root / str(agent_id)
    other_media = storage_root / str(other_agent_id) / "private" / "secret.mp4"
    agent_root.mkdir(parents=True)
    other_media.parent.mkdir(parents=True)
    other_media.write_bytes(
        b"\x00\x00\x00\x18ftypmp42hdlr\x00\x00\x00\x00\x00\x00\x00\x00vide"
    )
    (agent_root / "linked.mp4").symlink_to(other_media)
    storage = LocalStorageBackend(str(storage_root))
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    async def run_send_media(temp_workspace):
        return await agent_tools._send_channel_media(
            agent_id,
            temp_workspace,
            {
                "file_path": "linked.mp4",
                "session_id": str(uuid.uuid4()),
            },
            media_kind="video",
            tool_call_id="call-cross-agent-symlink",
        )

    result = await agent_tools._run_with_temp_workspace(
        agent_id,
        None,
        run_send_media,
        paths=["linked.mp4"],
        source_paths=["linked.mp4"],
    )

    payload = json.loads(result)
    assert payload["status"] == "failed"
    assert payload["code"] == "MEDIA_NOT_FOUND"


def test_media_tools_are_fixed_core_tools():
    definitions = [item["function"]["name"] for item in agent_tools.AGENT_TOOLS]
    assert definitions.count("send_media") == 1
    assert "send_audio" not in definitions
    assert "send_video" not in definitions
    assert "send_media" in agent_tools._ALWAYS_INCLUDE_CORE
    media_schema = next(
        item["function"]["parameters"]
        for item in agent_tools.AGENT_TOOLS
        if item["function"]["name"] == "send_media"
    )
    assert "allow_download" not in media_schema["properties"]
    assert media_schema["properties"]["title"] == {
        "type": "string",
        "maxLength": MAX_MEDIA_DISPLAY_TITLE_LENGTH,
        "description": (
            "Optional concise display title for the media card. This does not rename "
            "the file and is not delivered as message text."
        ),
    }
    assert media_schema == SEND_MEDIA_PARAMETERS_SCHEMA
    assert set(media_schema["properties"]["url_mode"]["enum"]) == {"external", "managed"}
    headers_schema = media_schema["properties"]["headers"]
    assert headers_schema["additionalProperties"] == {"type": "string"}
    assert "managed" in headers_schema["description"]
    assert "Authorization" in headers_schema["description"]
    assert "silently" in headers_schema["description"]
    assert "redirects" in headers_schema["description"]
    assert "Clawith" not in headers_schema["description"]
    assert "Agent" not in SEND_MEDIA_DESCRIPTION
    assert "platform" not in SEND_MEDIA_DESCRIPTION
    seeded = next(tool for tool in BUILTIN_TOOLS if tool["name"] == "send_media")
    assert seeded["parameters_schema"] == media_schema
    assert seeded["config"] == {"allow_download": False}
    assert seeded["config_schema"]["fields"] == [{
        "key": "allow_download",
        "label": "Allow media download",
        "type": "boolean",
        "default": False,
        "description": "Show the download action on send_media cards in supported chat clients.",
    }]


def test_media_display_title_is_safe_compact_and_bounded():
    raw = "  示例媒体\n\x00展示\t标题  " + ("占位" * 100)

    title = normalize_media_display_title(raw)

    assert title.startswith("示例媒体 展示 标题")
    assert "\n" not in title
    assert "\x00" not in title
    assert len(title) == MAX_MEDIA_DISPLAY_TITLE_LENGTH
    assert normalize_media_display_title(None) == ""


@pytest.mark.parametrize(
    ("status", "code", "message_fragment"),
    [
        ("failed", "MEDIA_SEND_FAILED", "发送"),
        ("unsupported", "CHANNEL_MEDIA_UNSUPPORTED", "不支持"),
        ("unknown", "MEDIA_DELIVERY_STATE_UNKNOWN", "不要自动重试"),
    ],
)
def test_media_error_result_explicitly_informs_agent(status, code, message_fragment):
    result = agent_tools._describe_media_delivery_result({
        "type": "media_delivery_result",
        "version": 1,
        "status": status,
        "code": code,
        "media_kind": "video",
    })

    assert message_fragment in result["message"]
    assert result["retryable"] is False
    assert result["agent_action"]


def test_current_session_media_result_prevents_duplicate_outer_done_row():
    payload = json.dumps({
        "type": "platform_media_delivery",
        "status": "sent",
        "session_id": "session-1",
        "message_id": str(uuid.uuid4()),
    })

    assert llm_caller._send_media_result_is_durable_in_current_session(
        "send_media", payload, "session-1"
    ) is True
    assert llm_caller._send_media_result_is_durable_in_current_session(
        "send_media", payload, "different-session"
    ) is False
    assert llm_caller._send_media_result_is_durable_in_current_session(
        "send_channel_file", payload, "session-1"
    ) is False


@pytest.mark.parametrize(
    ("media_size", "cover_size", "expected"),
    [
        (11 * 1024 * 1024, None, None),
        (100 * 1024 * 1024 + 1, None, "MEDIA_TOO_LARGE"),
        (1, 10 * 1024 * 1024 + 1, "VIDEO_COVER_TOO_LARGE"),
        (95 * 1024 * 1024, 9 * 1024 * 1024, "MEDIA_BUNDLE_TOO_LARGE"),
    ],
)
def test_media_materialization_size_errors_are_precise(media_size, cover_size, expected):
    assert agent_tools._media_materialization_size_error(media_size, cover_size) == expected


def test_media_tool_schemas_are_canonical_across_dynamic_tool_states():
    canonical = {
        item["function"]["name"]: item
        for item in agent_tools.AGENT_TOOLS
        if item["function"]["name"] == "send_media"
    }
    rogue = [
        {"type": "function", "function": {"name": "send_video", "description": "tenant override"}},
        {"type": "function", "function": {"name": "custom_tool", "parameters": {}}},
        {"type": "function", "function": {"name": "send_audio", "description": "disabled override"}},
    ]

    stabilized = agent_tools._stabilize_media_tool_definitions(rogue)

    assert [item["function"]["name"] for item in stabilized] == [
        "custom_tool",
        "send_media",
    ]
    assert stabilized[-1] == canonical["send_media"]
    description = stabilized[-1]["function"]["description"]
    assert "CURRENT conversation" in description
    assert "session_id" in description
    assert "user_id" in description
    assert "Groups require session_id" in description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_code"),
    [
        ({}, "INVALID_MEDIA_SOURCE"),
        ({"file_path": "workspace/a.mp3", "url": "https://example.com/a.mp3"}, "INVALID_MEDIA_SOURCE"),
        ({"url": "https://example.com/a.mp3"}, "INVALID_URL_MODE"),
        ({"url": "https://example.com/a.mp3", "url_mode": "copy"}, "INVALID_URL_MODE"),
        ({"file_path": "workspace/a.mp3", "headers": {}}, "INVALID_MEDIA_HEADERS"),
        ({
            "url": "https://example.com/a.mp3",
            "url_mode": "external",
            "headers": {"Authorization": "Bearer demo"},
        }, "INVALID_MEDIA_HEADERS"),
    ],
)
async def test_send_media_requires_one_explicit_source(tmp_path, arguments, expected_code):
    payload = json.loads(await agent_tools._send_channel_media(
        uuid.uuid4(),
        tmp_path,
        {**arguments, "session_id": str(uuid.uuid4())},
        media_kind="audio",
        tool_call_id="call-source",
    ))

    assert payload["status"] == "failed"
    assert payload["code"] == expected_code


@pytest.mark.asyncio
async def test_external_url_publishes_without_downloading(tmp_path, monkeypatch):
    captured = {}

    async def fake_validate(url, *, external):
        assert external is True
        return url

    async def fake_publish(**kwargs):
        captured.update(kwargs)
        return json.dumps({"type": "platform_media_delivery", "status": "sent"})

    async def fail_import(*args, **kwargs):
        raise AssertionError("external URL must not be downloaded")

    monkeypatch.setattr(agent_tools, "validate_media_url", fake_validate)
    monkeypatch.setattr(agent_tools, "_publish_external_media_to_session", fake_publish)
    monkeypatch.setattr(agent_tools, "import_managed_media_url", fail_import)
    monkeypatch.setattr(agent_tools, "_get_tool_config", lambda *_args: _async_value({"allow_download": True}))

    target_session = str(uuid.uuid4())
    payload = json.loads(await agent_tools._send_channel_media(
        uuid.uuid4(),
        tmp_path,
        {
            "media_type": "video",
            "url": "https://media.example/demo.mp4",
            "url_mode": "external",
            "session_id": target_session,
        },
        media_kind="video",
        tool_call_id="call-external",
    ))

    assert payload["status"] == "sent"
    assert captured["media_url"] == "https://media.example/demo.mp4"
    assert captured["session_id"] == target_session
    assert captured["allow_download"] is True


@pytest.mark.asyncio
async def test_managed_url_replay_returns_before_preflight_or_download(tmp_path, monkeypatch):
    async def fake_replay(**_kwargs):
        return {
            "type": "platform_media_delivery",
            "status": "already_sent",
            "code": "MEDIA_ALREADY_SENT",
        }

    async def should_not_run(**_kwargs):
        raise AssertionError("terminal replay must not preflight or download")

    monkeypatch.setattr(agent_tools, "_replay_terminal_media_delivery", fake_replay)
    monkeypatch.setattr(agent_tools, "_preflight_managed_media_target", should_not_run)
    monkeypatch.setattr(agent_tools, "import_managed_media_url", should_not_run)

    payload = json.loads(await agent_tools._send_channel_media(
        uuid.uuid4(),
        tmp_path,
        {
            "url": "https://expired.example/audio.mp3",
            "url_mode": "managed",
            "session_id": str(uuid.uuid4()),
        },
        media_kind="audio",
        tool_call_id="call-replay",
        origin_session_id=str(uuid.uuid4()),
    ))

    assert payload["status"] == "already_sent"


@pytest.mark.asyncio
async def test_managed_url_invalid_target_is_rejected_before_download(tmp_path, monkeypatch):
    async def no_replay(**_kwargs):
        return None

    async def fake_preflight(**_kwargs):
        return {
            "type": "media_delivery_result",
            "version": 1,
            "status": "failed",
            "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
            "media_kind": "audio",
        }

    async def should_not_download(*_args, **_kwargs):
        raise AssertionError("invalid target must not download")

    monkeypatch.setattr(agent_tools, "_replay_terminal_media_delivery", no_replay)
    monkeypatch.setattr(agent_tools, "_preflight_managed_media_target", fake_preflight)
    monkeypatch.setattr(agent_tools, "import_managed_media_url", should_not_download)

    payload = json.loads(await agent_tools._send_channel_media(
        uuid.uuid4(),
        tmp_path,
        {
            "url": "https://media.example/audio.mp3",
            "url_mode": "managed",
            "session_id": str(uuid.uuid4()),
        },
        media_kind="audio",
        tool_call_id="call-invalid-target",
    ))

    assert payload["status"] == "failed"
    assert payload["code"] == "SESSION_NOT_FOUND_OR_FORBIDDEN"


@pytest.mark.asyncio
async def test_managed_url_uses_origin_session_result_scope_and_agent_media_storage(
    tmp_path,
    monkeypatch,
):
    captured = {}
    agent_id = uuid.uuid4()
    origin_session_id = str(uuid.uuid4())
    target_session_id = str(uuid.uuid4())
    managed_file = tmp_path / "media" / "imported" / "managed-demo.mp4"
    managed_file.parent.mkdir(parents=True)
    managed_file.write_bytes(
        b"\x00\x00\x00\x18ftypmp42hdlr\x00\x00\x00\x00\x00\x00\x00\x00vide"
    )

    async def no_replay(**_kwargs):
        return None

    async def valid_target(**_kwargs):
        return None

    async def fake_import(_url, **kwargs):
        captured["import"] = kwargs
        def close_import():
            captured["import_closed"] = True

        return SimpleNamespace(
            file_path=managed_file,
            workspace_path="media/imported/managed-demo.mp4",
            mime_type="video/mp4",
            close=close_import,
        )

    class Storage:
        async def write_local_file(self, key, path, *, content_type=None):
            captured["storage"] = (key, path, content_type)

    async def fake_send(**kwargs):
        captured["send"] = kwargs
        return json.dumps({"type": "platform_media_delivery", "status": "sent"})

    monkeypatch.setattr(agent_tools, "_replay_terminal_media_delivery", no_replay)
    monkeypatch.setattr(agent_tools, "_preflight_managed_media_target", valid_target)
    monkeypatch.setattr(agent_tools, "import_managed_media_url", fake_import)
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: Storage())
    monkeypatch.setattr(agent_tools, "_get_tool_config", lambda *_args: _async_value({}))
    monkeypatch.setattr(agent_tools, "_send_media_to_session", fake_send)
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _agent_id: tmp_path)

    payload = json.loads(await agent_tools._send_channel_media(
        agent_id,
        tmp_path,
        {
            "url": "https://media.example/demo.mp4",
            "url_mode": "managed",
            "headers": {
                "Authorization": "Bearer media-token",
                "X-Media-Tenant": "tenant-a",
            },
            "session_id": target_session_id,
        },
        media_kind="video",
        tool_call_id="call-managed-layout",
        origin_session_id=origin_session_id,
    ))

    assert payload["status"] == "sent"
    assert captured["import"]["session_id"] == origin_session_id
    assert captured["import"]["operation_scope"] == (
        f"outbound:{agent_id}:{origin_session_id}:unanchored:call-managed-layout"
    )
    assert captured["import"]["request_headers"] == {
        "Authorization": "Bearer media-token",
        "X-Media-Tenant": "tenant-a",
    }
    assert captured["storage"] == (
        f"{agent_id}/media/imported/managed-demo.mp4",
        managed_file,
        "video/mp4",
    )
    assert captured["send"]["workspace_path"] == "media/imported/managed-demo.mp4"
    assert captured["import_closed"] is True


async def _async_value(value):
    return value
