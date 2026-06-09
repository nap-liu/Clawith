"""Guards for the thinking-persistence path shared by all IM channels.

Every IM channel (Feishu/DingTalk/WeCom/Slack/Discord/Teams/WhatsApp) collects
the agent's reasoning via on_thinking and stores it through
``persist_assistant_reply(..., thinking=...)``. These tests lock that contract
plus the ``cap_thinking`` truncation guard, without needing a live channel.
"""
import uuid

import pytest


class _FakeDB:
    def __init__(self, sink):
        self._sink = sink

    def add(self, m):
        self._sink.append(m)

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_persist_assistant_reply_stores_thinking():
    """The thinking passed by every IM channel lands on the assistant row."""
    from app.services.chat_history import persist_assistant_reply

    captured: list = []
    await persist_assistant_reply(
        lambda: _FakeDB(captured),
        agent_id=uuid.uuid4(), user_id=uuid.uuid4(),
        conversation_id="conv-1", content="answer", thinking="my reasoning here",
    )
    assert len(captured) == 1
    assert captured[0].content == "answer"
    assert captured[0].thinking == "my reasoning here"


@pytest.mark.asyncio
async def test_persist_assistant_reply_blank_thinking_is_none():
    from app.services.chat_history import persist_assistant_reply

    captured: list = []
    await persist_assistant_reply(
        lambda: _FakeDB(captured),
        agent_id=uuid.uuid4(), user_id=uuid.uuid4(),
        conversation_id="conv-1", content="answer", thinking="",
    )
    assert len(captured) == 1
    assert captured[0].thinking is None


def test_cap_thinking_truncates_and_handles_blank():
    from app.services.chat_history import cap_thinking, THINKING_MAX_CHARS

    assert cap_thinking(None) is None
    assert cap_thinking("") is None
    assert cap_thinking("short") == "short"
    big = "x" * (THINKING_MAX_CHARS + 5000)
    assert len(cap_thinking(big)) == THINKING_MAX_CHARS
