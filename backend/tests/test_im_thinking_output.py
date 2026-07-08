from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.im_thinking_output import (
    BufferedIMThinkingSender,
    resolve_im_thinking_enabled,
)


def test_resolve_im_thinking_enabled_defaults_to_agent_setting():
    agent = SimpleNamespace(im_thinking_output_enabled=True)
    session = SimpleNamespace(im_config={})

    assert resolve_im_thinking_enabled(agent, session) is True


def test_resolve_im_thinking_enabled_ignores_legacy_session_on_override():
    agent = SimpleNamespace(im_thinking_output_enabled=False)
    session = SimpleNamespace(im_config={"thinking_output": "on"})

    assert resolve_im_thinking_enabled(agent, session) is False


def test_resolve_im_thinking_enabled_ignores_legacy_session_off_override():
    agent = SimpleNamespace(im_thinking_output_enabled=True)
    session = SimpleNamespace(im_config={"thinking_output": "off"})

    assert resolve_im_thinking_enabled(agent, session) is True


def test_resolve_im_thinking_enabled_missing_fields_is_false():
    assert resolve_im_thinking_enabled(SimpleNamespace(), SimpleNamespace()) is False


@pytest.mark.asyncio
async def test_buffered_sender_sends_aggregated_thinking_without_prefix():
    sent: list[str] = []

    async def send_text(text: str) -> None:
        sent.append(text)

    sender = BufferedIMThinkingSender(
        enabled=True,
        send_text=send_text,
        min_interval_seconds=999,
        max_messages=4,
        max_chars_per_message=1000,
    )

    await sender.push("正在分析")
    await sender.push("上下文")
    await sender.flush()

    assert sent == ["正在分析上下文"]


@pytest.mark.asyncio
async def test_buffered_sender_noops_when_disabled():
    sent: list[str] = []

    async def send_text(text: str) -> None:
        sent.append(text)

    sender = BufferedIMThinkingSender(enabled=False, send_text=send_text)
    await sender.push("不会发送")
    await sender.flush()

    assert sent == []


@pytest.mark.asyncio
async def test_buffered_sender_limits_message_count_and_size():
    sent: list[str] = []

    async def send_text(text: str) -> None:
        sent.append(text)

    sender = BufferedIMThinkingSender(
        enabled=True,
        send_text=send_text,
        min_interval_seconds=999,
        max_messages=2,
        max_chars_per_message=5,
    )

    await sender.push("1234567890abcdef")
    await sender.flush()

    assert sent == ["12345", "67890"]


@pytest.mark.asyncio
async def test_buffered_sender_swallows_send_errors():
    async def send_text(text: str) -> None:
        raise RuntimeError(f"send failed: {text}")

    sender = BufferedIMThinkingSender(
        enabled=True,
        send_text=send_text,
        min_interval_seconds=999,
    )

    await sender.push("发送失败也不影响主流程")
    await sender.flush()


@pytest.mark.asyncio
async def test_buffered_sender_push_does_not_wait_for_slow_send():
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    sent: list[str] = []

    async def send_text(text: str) -> None:
        send_started.set()
        await release_send.wait()
        sent.append(text)

    sender = BufferedIMThinkingSender(
        enabled=True,
        send_text=send_text,
        min_interval_seconds=0,
    )

    await asyncio.wait_for(sender.push("正在分析上下文"), timeout=0.1)
    await asyncio.wait_for(send_started.wait(), timeout=1)
    assert sent == []

    release_send.set()
    await sender.flush()

    assert sent == ["正在分析上下文"]
