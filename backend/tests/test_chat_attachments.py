from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import chat_attachments
from app.services.chat_message_serializer import serialize_chat_message_for_client


def _names(items):
    return [item["display_name"] for item in items]


def test_dingtalk_legacy_multi_image_preserves_every_item_and_order():
    content = (
        "[file:dingtalk_richimg_a.jpg]\n"
        "[file:dingtalk_richimg_b.jpg]\n"
        "[file:dingtalk_richimg_b.jpg]\n"
        "请比较这些图片"
    )

    display, attachments = chat_attachments.parse_legacy_chat_attachments(content, "dingtalk")

    assert display == "请比较这些图片"
    assert _names(attachments) == [
        "dingtalk_richimg_a.jpg",
        "dingtalk_richimg_b.jpg",
        "dingtalk_richimg_b.jpg",
    ]
    assert all(item["kind"] == "image" for item in attachments)


def test_dingtalk_legacy_nine_images_are_not_capped_for_display():
    markers = [f"[file:image_{index}.jpg]" for index in range(9)]
    display, attachments = chat_attachments.parse_legacy_chat_attachments(
        "\n".join([*markers, "九张图"]),
        "dingtalk",
    )

    assert display == "九张图"
    assert len(attachments) == 9


def test_structured_metadata_is_authoritative_and_preserves_duplicate_paths():
    item = chat_attachments.attachment_from_workspace_path("workspace/uploads/a.jpg")
    display, attachments = chat_attachments.normalize_chat_message_attachments(
        "[file:a.jpg]\n[file:a.jpg]\n正文",
        {"attachments": [item, item]},
        "dingtalk",
    )

    assert display == "正文"
    assert attachments == [item, item]


def test_structured_display_content_handles_comma_in_filename_without_guessing():
    item = chat_attachments.attachment_from_workspace_path(
        "workspace/uploads/report,final.png"
    )
    display, attachments = chat_attachments.normalize_chat_message_attachments(
        "[file:report,final.png]\n[image_data:data:image/png;base64,AAAA]",
        {"attachments": [item], "display_content": "请分析"},
        "web",
    )

    assert display == "请分析"
    assert attachments == [item]


def test_explicit_empty_metadata_does_not_interpret_user_written_marker():
    display, attachments = chat_attachments.normalize_chat_message_attachments(
        "[file:not-an-attachment.jpg]\n这是用户正文",
        {"attachments": []},
        "dingtalk",
    )

    assert attachments == []
    assert display == "[file:not-an-attachment.jpg]\n这是用户正文"


def test_slack_trailing_markers_and_web_comma_envelope_are_legacy_compatible():
    slack_display, slack_attachments = chat_attachments.parse_legacy_chat_attachments(
        "正文\n[file:a.png] [file:report.pdf]",
        "slack",
    )
    web_display, web_attachments = chat_attachments.parse_legacy_chat_attachments(
        "[file:a.png, b.jpg, report.pdf]\n问题",
        "web",
    )

    assert slack_display == "正文"
    assert _names(slack_attachments) == ["a.png", "report.pdf"]
    assert web_display == "问题"
    assert _names(web_attachments) == ["a.png", "b.jpg", "report.pdf"]


def test_serializer_adds_attachment_fields_without_losing_message_fields():
    message_id = uuid.uuid4()
    message = SimpleNamespace(
        id=message_id,
        role="user",
        content="[file:a.jpg]\n说明",
        message_meta={"source_channel": "dingtalk"},
        thinking="保留思考",
        created_at=datetime(2026, 8, 7, tzinfo=UTC),
    )

    result = serialize_chat_message_for_client(
        message,
        sender_name="刘喜",
        sender_user_id=uuid.uuid4(),
    )

    assert result["id"] == str(message_id)
    assert result["content"] == "[file:a.jpg]\n说明"
    assert result["display_content"] == "说明"
    assert _names(result["attachments"]) == ["a.jpg"]
    assert result["thinking"] == "保留思考"
    assert result["sender_name"] == "刘喜"


def test_serializer_exposes_structured_agent_media_attachments():
    attachment = chat_attachments.attachment_from_workspace_path(
        "workspace/demo.mp4",
        mime_type="video/mp4",
        size_bytes=42,
    )
    message = SimpleNamespace(
        id=uuid.uuid4(),
        role="assistant",
        content="演示视频",
        message_meta={"source_channel": "web", "attachments": [attachment]},
        thinking=None,
        created_at=datetime(2026, 8, 7, tzinfo=UTC),
    )

    result = serialize_chat_message_for_client(message)

    assert result["display_content"] == "演示视频"
    assert result["attachments"] == [attachment]


def test_platform_managed_media_path_is_a_canonical_attachment():
    attachment = chat_attachments.attachment_from_workspace_path(
        "media/imported/managed-demo.mp4",
        mime_type="video/mp4",
        size_bytes=42,
    )

    assert attachment == {
        "display_name": "managed-demo.mp4",
        "path": "media/imported/managed-demo.mp4",
        "kind": "video",
        "mime_type": "video/mp4",
        "size_bytes": 42,
    }


@pytest.mark.parametrize(
    "path",
    [
        "exports/demo.mp4",
        "briefing.mp3",
        ".tool_results/session-1/result.mp4",
    ],
)
def test_any_canonical_agent_relative_path_can_become_an_attachment(path):
    attachment = chat_attachments.attachment_from_workspace_path(path)

    assert attachment["path"] == path
    assert chat_attachments.normalize_attachment_metadata([attachment]) == [attachment]


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../outside.mp4", "https://example.com/demo.mp4", "C:/demo.mp4"],
)
def test_non_agent_relative_attachment_path_is_rejected(path):
    with pytest.raises(ValueError, match="canonical agent file path"):
        chat_attachments.attachment_from_workspace_path(path)


def test_serializer_does_not_create_a_non_tool_media_render_protocol():
    attachment = chat_attachments.attachment_from_workspace_path(
        "workspace/demo.mp4", mime_type="video/mp4"
    )
    media_receipt = SimpleNamespace(
        id=uuid.uuid4(),
        role="assistant",
        content="",
        message_meta={
            "media_kind": "video",
            "delivery_status": "sent",
            "attachments": [attachment],
        },
        thinking=None,
        created_at=datetime(2026, 8, 7, tzinfo=UTC),
    )
    user_message = SimpleNamespace(
        id=uuid.uuid4(),
        role="user",
        content="用户正文",
        message_meta={"attachments": [attachment]},
        thinking=None,
        created_at=datetime(2026, 8, 7, tzinfo=UTC),
    )

    assert "standalone_media" not in serialize_chat_message_for_client(media_receipt)
    assert "standalone_media" not in serialize_chat_message_for_client(user_message)


@pytest.mark.parametrize("delivery_status", ["pending", "failed", "unknown"])
def test_serializer_hides_media_that_was_not_confirmed_sent(delivery_status):
    attachment = chat_attachments.attachment_from_workspace_path(
        "workspace/demo.mp4", mime_type="video/mp4"
    )
    message = SimpleNamespace(
        id=uuid.uuid4(),
        role="assistant",
        content="",
        message_meta={
            "source_channel": "dingtalk",
            "delivery_status": delivery_status,
            "attachments": [attachment],
        },
        thinking=None,
        created_at=datetime(2026, 8, 7, tzinfo=UTC),
    )

    assert serialize_chat_message_for_client(message)["attachments"] == []


@pytest.mark.parametrize(
    ("head", "misleading_name", "expected_kind", "expected_mime"),
    [
        (b"\x00\x00\x00\x18ftypmp42hdlr\x00\x00\x00\x00\x00\x00\x00\x00vide", "renamed.m4a", "video", "video/mp4"),
        (b"\x00\x00\x00\x18ftypisomhdlr\x00\x00\x00\x00\x00\x00\x00\x00soun", "renamed.mp4", "audio", "audio/mp4"),
        (b"\x1aE\xdf\xa3webm", "renamed.mp4", "video", "video/webm"),
        (b"RIFF\x00\x00\x00\x00WAVEdata", "renamed.mp3", "audio", "audio/wav"),
        (b"fLaCdata", "renamed.m4a", "audio", "audio/flac"),
    ],
)
def test_media_sniffing_keeps_concrete_container_mime(
    head, misleading_name, expected_kind, expected_mime
):
    mime = chat_attachments.sniff_media_mime_bytes(head, misleading_name)
    assert mime == expected_mime
    assert chat_attachments.sniff_media_kind_bytes(head, misleading_name) == expected_kind
    assert (
        chat_attachments.canonical_media_mime(
            misleading_name, expected_kind, mime
        )
        == expected_mime
    )


@pytest.mark.asyncio
async def test_client_attachment_validation_preserves_duplicates_and_checks_storage(monkeypatch):
    checked: list[str] = []

    class Storage:
        async def exists(self, key):
            checked.append(key)
            return True

        async def is_file(self, _key):
            return True

    monkeypatch.setattr(chat_attachments, "get_storage_backend", lambda: Storage())
    attachment = {
        "display_name": "同名.jpg",
        "path": "workspace/uploads/a.jpg",
        "kind": "image",
        "mime_type": "image/jpeg",
        "size_bytes": 12,
    }

    result = await chat_attachments.validate_client_attachments(
        uuid.uuid4(),
        [attachment, attachment],
    )

    assert len(result) == 2
    assert len(checked) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["private/secret.mp4", ".tool_results/session-1/result.mp4"],
)
async def test_client_attachment_validation_keeps_the_inbound_path_allowlist(
    path,
):
    attachment = chat_attachments.attachment_from_workspace_path(path)

    with pytest.raises(ValueError, match="client attachment paths are not allowed"):
        await chat_attachments.validate_client_attachments(
            uuid.uuid4(),
            [attachment],
        )
