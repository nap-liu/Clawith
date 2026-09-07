from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.services import dingtalk_stream


async def test_dingtalk_picture_is_forwarded_as_workspace_attachment_without_base64(monkeypatch):
    workspace_path = "workspace/uploads/dingtalk_img_test.jpg"
    monkeypatch.setattr(
        dingtalk_stream,
        "_download_dingtalk_media",
        AsyncMock(return_value=b"jpeg-bytes"),
    )
    monkeypatch.setattr(
        dingtalk_stream,
        "store_agent_upload",
        AsyncMock(return_value=("storage-key", workspace_path, None)),
    )

    user_text, saved_paths = await dingtalk_stream._process_media_message(
        {"msgtype": "picture", "content": {"downloadCode": "download-code"}},
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert user_text == "[用户发送了图片]"
    assert saved_paths == [workspace_path]
    assert "base64" not in user_text
    assert "image_data" not in user_text


@pytest.mark.parametrize(
    ("message", "expected_text"),
    [
        ({"msgtype": "text", "text": {"content": " hello "}}, "hello"),
        ({"msgtype": "picture", "content": {}}, "[用户发送了图片，但无法下载]"),
        ({"msgtype": "audio", "content": {"recognition": "识别结果"}}, "[语音消息] 识别结果"),
        ({"msgtype": "audio", "content": {}}, "[用户发送了语音消息，但无法处理]"),
        ({"msgtype": "video", "content": {}}, "[用户发送了视频，但无法下载]"),
        ({"msgtype": "file", "content": {"fileName": "a.pdf"}}, "[用户发送了文件 a.pdf，但无法下载]"),
        ({"msgtype": "unknown"}, "[用户发送了 unknown 类型消息，暂不支持]"),
    ],
)
async def test_dingtalk_non_download_branches_always_return_two_values(
    message,
    expected_text,
):
    result = await dingtalk_stream._process_media_message(
        message,
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert result == (expected_text, None)


@pytest.mark.parametrize(
    ("msgtype", "content", "expected_text_prefix", "workspace_path"),
    [
        ("audio", {"downloadCode": "c", "duration": 12}, "[用户发送了语音消息", "workspace/uploads/audio.amr"),
        ("video", {"downloadCode": "c", "duration": 34}, "[用户发送了视频", "workspace/uploads/video.mp4"),
        ("file", {"downloadCode": "c", "fileName": "a.pdf"}, "[file:a.pdf]", "workspace/uploads/a.pdf"),
    ],
)
async def test_dingtalk_downloaded_non_image_media_returns_one_path(
    monkeypatch,
    msgtype,
    content,
    expected_text_prefix,
    workspace_path,
):
    monkeypatch.setattr(dingtalk_stream, "_download_dingtalk_media", AsyncMock(return_value=b"bytes"))
    monkeypatch.setattr(
        dingtalk_stream,
        "store_agent_upload",
        AsyncMock(return_value=("storage-key", workspace_path, None)),
    )

    text, paths = await dingtalk_stream._process_media_message(
        {"msgtype": msgtype, "content": content},
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert text.startswith(expected_text_prefix)
    assert paths == [workspace_path]


@pytest.mark.parametrize("msgtype", ["picture", "audio", "video", "file"])
async def test_dingtalk_download_failure_returns_two_values(monkeypatch, msgtype):
    monkeypatch.setattr(dingtalk_stream, "_download_dingtalk_media", AsyncMock(return_value=None))
    content = {"downloadCode": "c", "fileName": "a.pdf"}

    result = await dingtalk_stream._process_media_message(
        {"msgtype": msgtype, "content": content},
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert len(result) == 2
    assert result[1] is None


@pytest.mark.parametrize("msgtype", ["picture", "audio", "video", "file"])
async def test_dingtalk_storage_failure_degrades_without_callback_crash(monkeypatch, msgtype):
    monkeypatch.setattr(dingtalk_stream, "_download_dingtalk_media", AsyncMock(return_value=b"bytes"))
    monkeypatch.setattr(
        dingtalk_stream,
        "store_agent_upload",
        AsyncMock(side_effect=OSError("disk unavailable")),
    )
    content = {"downloadCode": "c", "fileName": "a.pdf"}

    result = await dingtalk_stream._process_media_message(
        {"msgtype": msgtype, "content": content},
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert len(result) == 2
    assert result[1] is None


async def test_dingtalk_rich_text_keeps_successes_when_one_image_fails(monkeypatch):
    monkeypatch.setattr(
        dingtalk_stream,
        "_download_dingtalk_media",
        AsyncMock(side_effect=[b"first", None]),
    )
    monkeypatch.setattr(
        dingtalk_stream,
        "store_agent_upload",
        AsyncMock(return_value=("key", "workspace/uploads/first.jpg", None)),
    )

    text, paths = await dingtalk_stream._process_media_message(
        {
            "msgtype": "richText",
            "content": {"richText": [[
                {"text": "正文"},
                {"downloadCode": "one"},
                {"downloadCode": "two"},
            ]]},
        },
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert text == "正文"
    assert "base64" not in text
    assert "image_data" not in text
    assert paths == ["workspace/uploads/first.jpg"]
