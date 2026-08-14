from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

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


async def test_dingtalk_rich_text_keeps_text_and_paths_without_inline_image_data(monkeypatch):
    workspace_path = "workspace/uploads/dingtalk_richimg_test.jpg"
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
        {
            "msgtype": "richText",
            "content": {
                "richText": [[
                    {"text": "比较这张图"},
                    {"downloadCode": "download-code"},
                ]]
            },
        },
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert user_text == "比较这张图"
    assert saved_paths == [workspace_path]
    assert "base64" not in user_text
    assert "image_data" not in user_text
