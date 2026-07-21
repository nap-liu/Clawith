"""Persistent stop condition for conversations whose context cannot be reduced."""

from __future__ import annotations

import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.chat_session import ChatSession


SESSION_CONTEXT_TERMINATED_MESSAGE = (
    "上下文过长，请新开会话。"
)

IM_SESSION_CONTEXT_TERMINATED_MESSAGE = "上下文过长，请发送 /new 指令重置上下文。"

CONTEXT_PREFLIGHT_CHECK_FAILED_MESSAGE = (
    "当前请求在发送模型前的上下文检查失败，平台未发起模型调用。"
    "请稍后重试；如果持续出现，请新建会话。"
)

CONTEXT_REQUEST_TOO_LARGE_MESSAGE = (
    "上下文过长，请新开会话。"
)


def _session_uuid(session_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(session_id))
    except (TypeError, ValueError, AttributeError):
        return None


async def get_session_context_termination(session_id: str) -> str | None:
    """Return the user-facing stop message when this session is terminated."""
    parsed = _session_uuid(session_id)
    if parsed is None:
        return None
    try:
        async with async_session() as db:
            reason = (
                await db.execute(
                    select(ChatSession.context_terminated_reason).where(ChatSession.id == parsed)
                )
            ).scalar_one_or_none()
        return SESSION_CONTEXT_TERMINATED_MESSAGE if reason else None
    except Exception as exc:
        # This read is defense in depth. Entry-point/dispatch guards still run;
        # a transient state lookup failure must not break every normal session.
        logger.warning(
            f"[context_guard] failed to read termination state session={session_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        return None


async def terminate_session_context(session_id: str, reason: str) -> str:
    """Persist terminal state, claiming success only after a committed write."""
    parsed = _session_uuid(session_id)
    if parsed is None:
        logger.warning(
            f"[context_guard] cannot persist termination for non-UUID session={session_id}: {reason}"
        )
        return CONTEXT_REQUEST_TOO_LARGE_MESSAGE

    try:
        async with async_session() as db:
            session = (
                await db.execute(select(ChatSession).where(ChatSession.id == parsed))
            ).scalar_one_or_none()
            if session is None:
                logger.warning(
                    f"[context_guard] no chat session for termination id={session_id}: {reason}"
                )
                return CONTEXT_REQUEST_TOO_LARGE_MESSAGE
            if not session.context_terminated_reason:
                session.context_terminated_reason = (reason or "context_limit")[:500]
                await db.commit()
        logger.warning(f"[context_guard] terminated session={session_id} reason={reason}")
        return SESSION_CONTEXT_TERMINATED_MESSAGE
    except Exception as exc:
        logger.error(
            f"[context_guard] failed to persist termination session={session_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        return CONTEXT_REQUEST_TOO_LARGE_MESSAGE
