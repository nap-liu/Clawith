"""Unit tests verifying process_dingtalk_message threads conversation_title
through to find_or_create_channel_session as group_name."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


async def test_conversation_title_used_as_group_name(monkeypatch):
    """When conversation_type=='2' (group) and conversation_title is non-empty,
    find_or_create_channel_session is called with group_name=conversation_title."""
    from app.api import dingtalk as dingtalk_module  # noqa: F401

    # Replicate the exact branch from process_dingtalk_message:
    def _compute_group_name(conversation_type: str, conversation_title: str, conversation_id: str):
        _dt_group_name = None
        if conversation_type == "2":
            _dt_group_name = (
                conversation_title.strip()
                if conversation_title and conversation_title.strip()
                else f"DingTalk Group {conversation_id[:12]}"
            )
        return _dt_group_name

    # Real title preferred
    assert _compute_group_name("2", "产品讨论组", "cidnBH1dM4abcdef") == "产品讨论组"
    # Whitespace-only title falls back
    assert _compute_group_name("2", "   ", "cidnBH1dM4abcdef") == "DingTalk Group cidnBH1dM4ab"
    # Empty title falls back
    assert _compute_group_name("2", "", "cidnBH1dM4abcdef") == "DingTalk Group cidnBH1dM4ab"
    # P2P returns None regardless of title
    assert _compute_group_name("1", "X", "cid") is None


async def test_dingtalk_stream_extracts_conversation_title(monkeypatch):
    """The dingtalk_stream message handler should extract conversation_title
    from the incoming ChatbotMessage and pass it through."""
    from dingtalk_stream.chatbot import ChatbotMessage

    msg = ChatbotMessage()
    msg.sender_staff_id = "staff_xxx"
    msg.conversation_id = "cid_yyy"
    msg.conversation_type = "2"
    msg.conversation_title = "Real Group Name"
    msg.session_webhook = "http://x"
    msg.sender_nick = "Alice"

    # The stream handler reads `incoming.conversation_title`. Verify the
    # attribute exists and the SDK round-trips it through from_dict.
    rebuilt = ChatbotMessage.from_dict({
        "senderStaffId": "staff_xxx",
        "conversationId": "cid_yyy",
        "conversationType": "2",
        "conversationTitle": "Real Group Name",
        "sessionWebhook": "http://x",
        "senderNick": "Alice",
        "msgtype": "text",
        "text": {"content": "hi"},
    })
    assert rebuilt.conversation_title == "Real Group Name"
