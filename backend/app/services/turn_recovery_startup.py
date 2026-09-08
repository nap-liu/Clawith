"""Wait for a recoverable startup anchor's existing execution owner to leave."""

from __future__ import annotations

from contextlib import AsyncExitStack

from loguru import logger

from app.models.audit import ChatMessage
from app.services.conversation_execution_lock import conversation_execution_lock
from app.services.redis_lease_lock import RedisLeaseBusyError


async def resume_startup_anchor(anchor: ChatMessage) -> bool:
    """Acquire the shared lease before recovery writes, without stealing ownership.

    A live owner may keep renewing beyond one acquisition window. Recheck the
    original durable boundary between attempts so STOP, completion, or a new
    generation ends this wait. No database transaction spans a lease wait.
    """
    from app.services import turn_recovery

    expected_origin = await turn_recovery._load_fresh_recovery_origin(anchor)
    if expected_origin is None:
        return False

    while True:
        if await turn_recovery._load_fresh_recovery_origin(anchor) != expected_origin:
            return False
        async with AsyncExitStack() as stack:
            try:
                await stack.enter_async_context(conversation_execution_lock(
                    agent_id=anchor.agent_id,
                    session_id=anchor.conversation_id,
                ))
            except RedisLeaseBusyError:
                logger.debug(
                    "[turn_recovery] waiting for current owner anchor={}", anchor.id,
                )
                continue

            if await turn_recovery._load_fresh_recovery_origin(anchor) != expected_origin:
                return False
            # The same task owns this lease through all recovery writes and the
            # original reentrant LLM entry point. Errors from resume are not retried.
            return await turn_recovery.resume_turn(anchor)
