"""Helpers for sending model thinking progress to external IM channels."""

from __future__ import annotations

import time
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from loguru import logger

THINKING_OUTPUT_KEY = "thinking_output"
THINKING_ON = "on"
THINKING_OFF = "off"
THINKING_INHERIT = "inherit"

SendText = Callable[[str], Awaitable[None]]


def resolve_im_thinking_enabled(agent, session) -> bool:
    """Return the effective IM thinking-output setting for this digital employee."""
    return bool(getattr(agent, "im_thinking_output_enabled", False))


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
