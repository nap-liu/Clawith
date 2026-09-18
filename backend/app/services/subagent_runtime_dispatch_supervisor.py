"""Bounded, parent-keyed supervision for durable Subagent event dispatch."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from loguru import logger


class ParentDispatchSupervisor:
    """Run unrelated parents concurrently while serializing each parent."""

    def __init__(
        self,
        *,
        concurrency: int,
        retry_seconds: float,
        wake_event: asyncio.Event,
    ) -> None:
        self._retry_seconds = retry_seconds
        self._wake_event = wake_event
        self._execution_slots = asyncio.Semaphore(concurrency)
        self._active: dict[uuid.UUID, asyncio.Task[bool]] = {}
        self._message_ids: dict[uuid.UUID, tuple[uuid.UUID, ...]] = {}
        self._closed = False

    def schedule(
        self,
        *,
        parent_session_id: uuid.UUID,
        message_ids: list[uuid.UUID],
        work: Callable[[], Awaitable[bool]],
    ) -> bool:
        if (
            self._closed
            or not message_ids
            or parent_session_id in self._active
        ):
            return False
        task = asyncio.create_task(
            self._run_until_processed(
                parent_session_id=parent_session_id,
                message_ids=tuple(message_ids),
                work=work,
            ),
            name=f"subagent-parent-dispatch:{parent_session_id}",
        )
        self._active[parent_session_id] = task
        self._message_ids[parent_session_id] = tuple(message_ids)
        task.add_done_callback(
            lambda finished: self._on_done(
                parent_session_id,
                tuple(message_ids),
                finished,
            )
        )
        return True

    def active_message_ids(self) -> set[uuid.UUID]:
        """Return rows already owned so bounded scans can move past them."""
        return {
            message_id
            for message_ids in self._message_ids.values()
            for message_id in message_ids
        }

    async def _run_until_processed(
        self,
        *,
        parent_session_id: uuid.UUID,
        message_ids: tuple[uuid.UUID, ...],
        work: Callable[[], Awaitable[bool]],
    ) -> bool:
        logged_failure = False
        while True:
            try:
                async with self._execution_slots:
                    processed = await work()
                if processed:
                    return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - durable work is retried
                if not logged_failure:
                    logged_failure = True
                    logger.exception(
                        "[subagent] supervised parent dispatch failed; "
                        "further failures are suppressed until this batch succeeds "
                        "parent={} messages={}: {}",
                        parent_session_id,
                        message_ids,
                        exc,
                    )
            await asyncio.sleep(self._retry_seconds)

    def _on_done(
        self,
        parent_session_id: uuid.UUID,
        message_ids: tuple[uuid.UUID, ...],
        task: asyncio.Task[bool],
    ) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - guard unexpected supervisor faults
            logger.exception(
                "[subagent] parent dispatch supervisor stopped unexpectedly "
                "parent={} messages={}: {}",
                parent_session_id,
                message_ids,
                exc,
            )
        if self._closed:
            self._active.pop(parent_session_id, None)
            self._message_ids.pop(parent_session_id, None)
            return
        self._release_and_wake(parent_session_id)

    def _release_and_wake(self, parent_session_id: uuid.UUID) -> None:
        self._active.pop(parent_session_id, None)
        self._message_ids.pop(parent_session_id, None)
        if not self._closed:
            self._wake_event.set()

    async def close(self) -> None:
        """Cancel and await every owned task before the daemon exits."""
        self._closed = True
        tasks = list(set(self._active.values()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        self._message_ids.clear()
