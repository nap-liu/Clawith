from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import dingtalk_stream
from app.services.chat_history import build_llm_message_from_row
from app.services.chat_message_serializer import serialize_chat_message_for_client
from app.services.quoted_message import (
    normalize_quoted_message,
    render_quoted_message_for_llm,
)

pytestmark = pytest.mark.asyncio


async def test_text_quote_is_normalized_with_sender_and_provider_identity():
    quote = await dingtalk_stream._parse_dingtalk_quoted_message(
        {
            "msgtype": "text",
            "text": {
                "content": "这是什么？",
                "isReplyMsg": True,
                "repliedMsg": {
                    "msgType": "text",
                    "msgId": "quoted-message-id",
                    "senderId": "quoted-sender-id",
                    "senderNick": "张三",
                    "createdAt": 1785405000000,
                    "content": {"text": "被引用的原文"},
                },
            },
        },
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert quote == {
        "message_type": "text",
        "content_status": "available",
        "text": "被引用的原文",
        "attachments": [],
        "provider_message_id": "quoted-message-id",
        "sender_ref": "quoted-sender-id",
        "sender_name": "张三",
        "provider_message_type": "text",
        "created_at_ms": 1785405000000,
    }


@pytest.mark.parametrize(
    ("raw_type", "content", "payload", "expected_type", "expected_kind"),
    [
        ("picture", {"downloadCode": "pic"}, b"\xff\xd8\xffjpeg", "image", "image"),
        ("voice", {"downloadCode": "voice"}, b"#!AMR\nvoice", "audio", "audio"),
        ("audio", {"downloadCode": "audio"}, b"ID3audio", "audio", "audio"),
        ("video", {"downloadCode": "video"}, b"\x00\x00\x00\x18ftypmp42", "video", "video"),
        ("file", {"downloadCode": "file", "fileName": "report.pdf"}, b"%PDF", "file", "file"),
    ],
)
async def test_all_quoted_media_types_become_workspace_attachments(
    monkeypatch,
    raw_type,
    content,
    payload,
    expected_type,
    expected_kind,
):
    monkeypatch.setattr(
        dingtalk_stream,
        "_download_dingtalk_media",
        AsyncMock(return_value=payload),
    )

    async def store(_agent_id, filename, _payload, *, content_type=None):
        return "storage-key", f"workspace/uploads/{filename}", None

    monkeypatch.setattr(dingtalk_stream, "store_agent_upload", store)

    quote = await dingtalk_stream._parse_dingtalk_quoted_message(
        {
            "text": {
                "isReplyMsg": True,
                "repliedMsg": {
                    "msgType": raw_type,
                    "msgId": f"{raw_type}-id",
                    "content": content,
                },
            }
        },
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert quote is not None
    assert quote["message_type"] == expected_type
    assert quote["content_status"] == "available"
    assert len(quote["attachments"]) == 1
    assert quote["attachments"][0]["kind"] == expected_kind
    assert quote["attachments"][0]["size_bytes"] == len(payload)
    assert quote["attachments"][0]["path"].startswith("workspace/uploads/dingtalk_quote_")


async def test_voice_quote_keeps_recognition_when_media_download_fails(monkeypatch):
    monkeypatch.setattr(
        dingtalk_stream,
        "_download_dingtalk_media",
        AsyncMock(return_value=None),
    )

    quote = await dingtalk_stream._parse_dingtalk_quoted_message(
        {
            "text": {
                "isReplyMsg": True,
                "repliedMsg": {
                    "msgType": "voice",
                    "content": {
                        "recognition": "这是语音识别结果",
                        "downloadCode": "expired",
                    },
                },
            }
        },
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert quote is not None
    assert quote["text"] == "语音识别：这是语音识别结果"
    assert quote["content_status"] == "partial"
    assert quote["attachments"] == []


async def test_rich_text_quote_preserves_text_order_and_successful_images(monkeypatch):
    monkeypatch.setattr(
        dingtalk_stream,
        "_download_dingtalk_media",
        AsyncMock(side_effect=[b"\x89PNG\r\n\x1a\nfirst", None]),
    )

    async def store(_agent_id, filename, _payload, *, content_type=None):
        return "storage-key", f"workspace/uploads/{filename}", None

    monkeypatch.setattr(dingtalk_stream, "store_agent_upload", store)

    quote = await dingtalk_stream._parse_dingtalk_quoted_message(
        {
            "text": {
                "isReplyMsg": True,
                "repliedMsg": {
                    "msgType": "richText",
                    "content": {
                        "richText": [
                            [{"msgType": "text", "content": "第一段"}],
                            [
                                {"msgType": "picture", "downloadCode": "one"},
                                {"text": "第二段"},
                                {"msgType": "picture", "downloadCode": "two"},
                            ],
                        ]
                    },
                },
            }
        },
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert quote is not None
    assert quote["message_type"] == "rich_text"
    assert quote["text"] == "第一段\n第二段"
    assert quote["content_status"] == "partial"
    assert [item["kind"] for item in quote["attachments"]] == ["image"]


async def test_rich_text_quote_supports_nested_picture_content(monkeypatch):
    monkeypatch.setattr(
        dingtalk_stream,
        "_download_dingtalk_media",
        AsyncMock(return_value=b"\x89PNG\r\n\x1a\nimage"),
    )

    async def store(_agent_id, filename, _payload, *, content_type=None):
        return "storage-key", f"workspace/uploads/{filename}", None

    monkeypatch.setattr(dingtalk_stream, "store_agent_upload", store)

    quote = await dingtalk_stream._parse_dingtalk_quoted_message(
        {
            "text": {
                "isReplyMsg": True,
                "repliedMsg": {
                    "msgType": "richText",
                    "content": {
                        "richText": [
                            {
                                "msgType": "picture",
                                "content": {
                                    "downloadCode": "nested-picture",
                                    "fileName": "inline.png",
                                },
                            }
                        ]
                    },
                },
            }
        },
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert quote is not None
    assert quote["content_status"] == "available"
    assert quote["attachments"][0]["display_name"] == "inline.png"


@pytest.mark.parametrize("raw_type", ["markdown", "actionCard", "link", "oa", "interactiveCard"])
async def test_card_quotes_extract_available_human_text(raw_type):
    quote = await dingtalk_stream._parse_dingtalk_quoted_message(
        {
            "text": {
                "isReplyMsg": True,
                "repliedMsg": {
                    "msgType": raw_type,
                    "content": {
                        "title": "卡片标题",
                        "body": {"text": "卡片正文"},
                        "internalId": "must-not-enter-visible-text",
                    },
                },
            }
        },
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert quote is not None
    assert quote["message_type"] == "card"
    assert quote["text"] == "卡片标题\n卡片正文"
    assert "must-not-enter" not in quote["text"]


async def test_bot_card_quote_without_callback_content_is_explicitly_unavailable():
    quote = await dingtalk_stream._parse_dingtalk_quoted_message(
        {
            "text": {
                "isReplyMsg": True,
                "repliedMsg": {
                    "msgType": "interactiveCard",
                    "msgId": "bot-card-id",
                    "senderNick": "小智",
                },
            }
        },
        "app-key",
        "app-secret",
        uuid.uuid4(),
    )

    assert quote is not None
    assert quote["message_type"] == "card"
    assert quote["content_status"] == "unavailable"
    assert quote["provider_message_id"] == "bot-card-id"


async def test_quote_metadata_is_visible_to_llm_history_and_clients():
    attachment = {
        "display_name": "quoted.png",
        "path": "workspace/uploads/quoted.png",
        "kind": "image",
        "mime_type": "image/png",
    }
    quote = normalize_quoted_message(
        {
            "message_type": "rich_text",
            "provider_message_id": "quoted-id",
            "sender_name": "张三",
            "content_status": "available",
            "text": "原始内容",
            "attachments": [attachment],
        }
    )
    row = SimpleNamespace(
        id=uuid.uuid4(),
        role="user",
        content="请分析这条消息",
        message_meta={
            "source_channel": "dingtalk",
            "attachments": [attachment],
            "quoted_message": quote,
        },
        thinking=None,
        created_at=datetime(2026, 8, 26, tzinfo=UTC),
        sender_user_id=None,
        user_id=None,
    )

    llm_message = build_llm_message_from_row(row)
    client_message = serialize_chat_message_for_client(row)

    assert llm_message["role"] == "user"
    assert "引用消息上下文" in llm_message["content"]
    assert "原始内容" in llm_message["content"]
    assert "请分析这条消息" in llm_message["content"]
    assert llm_message["attachments"] == [attachment]
    assert client_message["display_content"] == "请分析这条消息"
    assert client_message["quoted_message"] == quote


async def test_rendered_current_message_is_not_lost_when_quote_is_unavailable():
    rendered = render_quoted_message_for_llm(
        "当前问题",
        {
            "message_type": "card",
            "content_status": "unavailable",
            "attachments": [],
        },
    )

    assert '"content_status":"unavailable"' in rendered
    assert rendered.endswith("当前消息：\n当前问题")
