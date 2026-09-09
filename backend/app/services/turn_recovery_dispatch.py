"""Dispatch recovery candidates without blocking other conversations."""

import asyncio
from loguru import logger
from app.database import async_session
from app.models.audit import ChatMessage
from app.services.redis_lease_lock import RedisLeaseBusyError, RedisLeaseLock
from app.services.turn_recovery_types import RecoveryStats
from app.services.turn_recovery_startup import resume_startup_anchor
from app.services.turn_interruption import TurnInterrupted

STARTUP_RECOVERY_LEASE_RESOURCE = "startup-turn-recovery"

async def _resume_one(
    anchor: ChatMessage,
    *, resume_promoted_turn: bool = True,
) -> RecoveryStats:
    """Resume one independently cancellable anchor."""
    result = RecoveryStats()
    try:
        # A recovered turn owns its cancellation lifecycle. Running it in a
        # child task lets one stopped turn remain isolated while an actual
        # shutdown still cancels the whole startup recovery set.
        did_resume = await asyncio.create_task(resume_startup_anchor(
            anchor, **({"resume_promoted_turn": False} if not resume_promoted_turn else {}),
        ))
    except TurnInterrupted:
        result.skipped = 1
        logger.info("[turn_recovery] execution interrupted; original anchor remains pending={}", anchor.id)
    except asyncio.CancelledError:
        recovery_task = asyncio.current_task()
        if recovery_task is not None and recovery_task.cancelling():
            raise
        result.skipped = 1
        logger.info(
            "[turn_recovery] recovery cancelled for anchor={}",
            anchor.id,
        )
    except Exception as exc:  # noqa: BLE001 - one failed turn must not cancel its batch
        result.failed = 1
        logger.exception(f"[turn_recovery] failed to resume anchor={anchor.id}: {exc}")
    else:
        if did_resume:
            result.resumed = 1
        else:
            result.skipped = 1
    return result


_inflight: dict[str, asyncio.Task] = {}


async def dispatch_recovery_candidates() -> list[asyncio.Task]:
    """Discover lost owners in the existing worker tick; never wait for a model."""
    from app.core.events import get_redis
    from app.services.turn_recovery_scanner import _load_recoverable_anchors

    try:
        async with RedisLeaseLock(STARTUP_RECOVERY_LEASE_RESOURCE, namespace="turn-recovery"):
            async with async_session() as db:
                anchors = await _load_recoverable_anchors(db, include_legacy=False)
            redis = await get_redis()
            tasks = []
            for anchor in anchors:
                key = str(anchor.id)
                existing = _inflight.get(key)
                if existing is not None and not existing.done():
                    continue
                lease_key = RedisLeaseLock(
                    str(anchor.conversation_id), namespace="conversation-execution",
                ).key
                if anchor.role != "assistant" and await redis.exists(lease_key):
                    continue
                task = asyncio.create_task(_resume_one(anchor), name=f"turn_recovery:{key}")
                _inflight[key] = task
                def completed(done, anchor_key=key):
                    if _inflight.get(anchor_key) is done:
                        _inflight.pop(anchor_key, None)
                    if not done.cancelled():
                        done.exception()
                task.add_done_callback(completed)
                tasks.append(task)
            return tasks
    except RedisLeaseBusyError:
        return []


async def startup_turn_resume_once(*, limit: int = 50) -> RecoveryStats:
    """Start through the same worker dispatcher, then await this startup batch."""
    from app.services.turn_inbox import cleanup_stale_channel_receipt_anchors

    try:
        await cleanup_stale_channel_receipt_anchors(limit=limit)
    except Exception:
        logger.opt(exception=True).warning("[turn_recovery] feedback cleanup failed")
    tasks = await dispatch_recovery_candidates()
    stats = RecoveryStats(scanned=len(tasks))
    if tasks:
        results = await asyncio.gather(*tasks)
        for result in results:
            stats.resumed += result.resumed
            stats.skipped += result.skipped
            stats.failed += result.failed
    return stats


async def cancel_recovery_dispatch() -> None:
    """Release this process's recovery owners while keeping their durable turns."""
    tasks = list(_inflight.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
