"""Helpers for sending model thinking progress to external IM channels."""

from __future__ import annotations

import time
import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

THINKING_OUTPUT_KEY = "thinking_output"
THINKING_ON = "on"
THINKING_OFF = "off"
THINKING_INHERIT = "inherit"

SendText = Callable[[str], Awaitable[None]]


def resolve_im_progress_enabled(agent, session) -> bool:
    """Return whether public, Agent-authored turn progress is visible in IM."""
    return bool(getattr(agent, "im_thinking_output_enabled", False))


def resolve_im_thinking_enabled(agent, session) -> bool:
    """Raw provider reasoning is never projected to external IM users."""
    return False


@dataclass
class BufferedIMThinkingSender:
    """Bounded, best-effort sender for thinking chunks on IM channels."""

    enabled: bool
    send_text: SendText
    min_interval_seconds: float = 2.0
    max_messages: int = 8
    max_chars_per_message: int = 1000
    _buffer: list[str] = field(default_factory=list)
    _sent_count: int = 0
    _next_send_at: float = 0.0
    _flush_task: asyncio.Task | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def for_runtime(
        cls,
        *,
        enabled: bool,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: str,
        turn_anchor_id: uuid.UUID | None = None,
        delivery_kwargs: dict[str, Any] | None = None,
        **buffer_kwargs: Any,
    ) -> "BufferedIMThinkingSender":
        """Build a sender whose every visible segment uses the normalized outbox."""

        async def _send(text: str) -> None:
            from app.services.im_delivery import persist_and_deliver_runtime_message
            from app.services.turn_runtime import load_turn_runtime

            runtime = await load_turn_runtime(
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
            await persist_and_deliver_runtime_message(
                agent_id=agent_id,
                user_id=user_id,
                runtime=runtime,
                message=text,
                turn_anchor_id=turn_anchor_id,
                artifact_role="thinking",
                **dict(delivery_kwargs or {}),
            )

        return cls(enabled=enabled, send_text=_send, **buffer_kwargs)

    @classmethod
    def for_callback(
        cls,
        *,
        enabled: bool,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: str,
        channel: str,
        transport: str,
        conversation_ref: str,
        send_text: SendText,
        turn_anchor_id: uuid.UUID | None = None,
        recallable: bool = False,
        **buffer_kwargs: Any,
    ) -> "BufferedIMThinkingSender":
        """Build a sender around an exact callback-only transport adapter."""

        async def _send(text: str) -> None:
            from app.services.im_delivery import (
                IMDeliveryPart,
                IMDeliveryResult,
                persist_and_deliver_message,
            )

            async def _deliver(delivery_message: str, on_part) -> IMDeliveryResult:
                await send_text(delivery_message)
                part = IMDeliveryPart(
                    transport=transport,
                    conversation_ref=conversation_ref,
                    artifact_role="thinking",
                    recallable=recallable,
                )
                await on_part(part)
                return IMDeliveryResult.sent(channel, part)

            await persist_and_deliver_message(
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conversation_id,
                channel=channel,
                message=text,
                deliver=_deliver,
                turn_anchor_id=turn_anchor_id,
                artifact_role="thinking",
            )

        return cls(enabled=enabled, send_text=_send, **buffer_kwargs)

    async def push(self, text: str) -> None:
        should_schedule = False
        async with self._lock:
            if not self.enabled or not text or self._sent_count >= self.max_messages:
                return
            was_empty = not self._buffer
            self._buffer.append(text)
            now = time.monotonic()
            if was_empty and self._next_send_at <= 0:
                self._next_send_at = now + self.min_interval_seconds
            buffered_chars = sum(len(chunk) for chunk in self._buffer)
            should_schedule = (
                now >= self._next_send_at
                or buffered_chars >= self.max_chars_per_message
            )
        if should_schedule:
            self._schedule_flush()

    async def flush(self) -> None:
        task = self._flush_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            await task
        await self._flush_now()

    def _schedule_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = asyncio.create_task(self._flush_now())

    async def _flush_now(self) -> None:
        async with self._lock:
            if not self.enabled or not self._buffer or self._sent_count >= self.max_messages:
                return
            content = "".join(self._buffer).strip()
            self._buffer = []
        while content and self._sent_count < self.max_messages:
            segment = content[: self.max_chars_per_message]
            content = content[self.max_chars_per_message :]
            try:
                await self.send_text(segment)
            except Exception as exc:  # noqa: BLE001 - channel feedback is best-effort
                logger.warning(f"[im_thinking_output] send failed (ignored): {exc}")
            async with self._lock:
                self._sent_count += 1
                self._next_send_at = time.monotonic() + self.min_interval_seconds
