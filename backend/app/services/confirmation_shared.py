"""Confirmation service — confirmation card as a normal, suspended tool_call.

A confirmation card is NOT a parallel system. It is one ordinary ``request_confirmation``
tool_call whose execution simply SUSPENDS the turn until the user clicks:

- ``suspend_for_confirmation``: persist the agent's intro text + a PENDING tool_call row
  (``status="pending"``, empty result) and deliver the card to the originating channel.
  The row id is the canonical confirmation handle. Web uses it as ``call_id``;
  DingTalk delivery instances use it as the stable prefix of ``outTrackId``.
- ``resolve_confirmation``: the user's click FILLS that row's tool result and flips status
  to ``done``, then RESUMES the loop from the now-complete history — no synthetic user
  message, no separate table/event/role.

The card's content is the tool_call's ``args``; its state is the tool_call's status/result
(``expand_tool_call_row`` replays the assistant(tool_call)+tool(result) pair). The platform
executes nothing — it faithfully relays the clicked button; the agent decides what to do.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.services.channel_dispatch import run_channel_send
from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_NAME
from app.services.turn_runtime import (
    deliver_reply_to_origin,
    load_turn_runtime,
)

logger = logging.getLogger(__name__)

CONFIRMATION_EXPIRY_HOURS = 24


def _im_send_lock_key(conversation_id: str) -> str:
    return f"im-send:{conversation_id}"


class ConfirmationActorMismatch(PermissionError):
    """The resolving user is not the human for whom this card was created."""


@dataclass(frozen=True)
class PendingConfirmation:
    """The durable suspended ``request_confirmation`` tool call for one session."""

    row_id: uuid.UUID
    agent_id: uuid.UUID
    conversation_id: str
    user_id: uuid.UUID | None
    args: dict
    created_at: datetime | None
    turn_anchor_id: uuid.UUID | None = None
    force_confirmation: bool = True


async def find_pending_confirmation(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> PendingConfirmation | None:
    """Return the session's normalized pending confirmation, if any.

    Confirmation remains part of the normalized message stream. The latest active
    tool call is read through the existing ``ix_chat_messages_active`` session
    history index and interpreted in Python. This avoids casting historical message
    text inside PostgreSQL while keeping the ordinary message row as the only truth.
    """
    from app.models.audit import ChatMessage

    row = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == str(conversation_id),
                ChatMessage.role == "tool_call",
                ChatMessage.compacted_into.is_(None),
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        payload = json.loads(row.content or "{}")
    except (TypeError, ValueError):
        return None
    if (
        isinstance(payload, dict)
        and payload.get("name") == REQUEST_CONFIRMATION_TOOL_NAME
        and payload.get("status") == "pending"
    ):
        args = payload.get("args")
        normalized_args = args if isinstance(args, dict) else {}
        return PendingConfirmation(
            row_id=row.id,
            agent_id=row.agent_id,
            conversation_id=row.conversation_id,
            user_id=row.user_id,
            args=normalized_args,
            created_at=row.created_at,
            turn_anchor_id=(
                uuid.UUID(str((row.message_meta or {}).get("turn_anchor_id")))
                if (row.message_meta or {}).get("turn_anchor_id")
                else None
            ),
            force_confirmation=normalized_args.get("force_confirmation") is not False,
        )
    return None


async def find_dingtalk_pending_confirmation(
    *,
    agent_id: uuid.UUID,
    external_conv_id: str,
) -> PendingConfirmation | None:
    """Read the pending card for an existing DingTalk session, if any."""
    from app.models.chat_session import ChatSession

    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.source_channel == "dingtalk",
                    ChatSession.external_conv_id == external_conv_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            return None
        return await find_pending_confirmation(
            db,
            agent_id=agent_id,
            conversation_id=str(session.id),
        )


def _assert_confirmation_actor(row, pending_meta: dict, resolving_user_id: uuid.UUID) -> None:
    intended_user_id = pending_meta.get("intended_user_id") or row.user_id
    if intended_user_id is None or str(intended_user_id) != str(resolving_user_id):
        raise ConfirmationActorMismatch("This confirmation belongs to another user")


def _action_preview(action: dict | None) -> str:
    """One-line, concrete preview of the action the agent intends to run (display only)."""
    if not action:
        return ""
    tool = str(action.get("tool") or "")
    args = action.get("args")
    if isinstance(args, dict) and args:
        parts = []
        for k, v in args.items():
            vs = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            parts.append(f"{k}={vs}")
        preview = f"{tool}({', '.join(parts)})"
    else:
        preview = tool
    return preview if len(preview) <= 300 else preview[:300] + "…"


def _build_card_args(
    title: str,
    summary: str,
    action: dict | None,
    risk_level: str,
    buttons: list | None,
    force_confirmation: bool,
) -> dict:
    """The tool_call args that ARE the card — what the agent passed to request_confirmation.
    Stored verbatim on the tool_call row; both renderers (web + DingTalk) read these and
    derive the action preview themselves via _action_preview (web mirrors it in TS)."""
    return {
        "title": title or "",
        "summary": summary or "",
        "action": action,
        "risk_level": risk_level or "medium",
        "buttons": buttons,
        "force_confirmation": force_confirmation,
    }


def _ago(created: datetime | None, now: datetime) -> str:
    """Human '约 N 分钟/小时前发出' suffix — cards are time-sensitive (24h)."""
    if not created:
        return ""
    mins = int((now - created).total_seconds() // 60)
    if mins >= 60:
        return f",约 {mins // 60} 小时前发出"
    if mins >= 1:
        return f",约 {mins} 分钟前发出"
    return ""


async def _broadcast(agent_id: uuid.UUID, conversation_id: str, payload: dict) -> None:
    """Best-effort broadcast to web WebSocket clients viewing this session. Never raises."""
    try:
        from app.api.websocket import manager  # lazy import — avoids services→api circular dependency

        await manager.send_to_session(str(agent_id), str(conversation_id), payload)
    except Exception:  # best-effort; broadcast failure must not affect DB write
        pass


async def _resolve_session_channel(
    conversation_id: str, chat_session_id: uuid.UUID | None, source_channel: str
) -> tuple[str, str | None, bool]:
    """Derive (channel, external_conv_id, is_group) from the ChatSession so a card is
    delivered to its TRUE origin (a DingTalk-originated turn → DingTalk, not web).
    conversation_id == ChatSession.id by convention."""
    resolved = source_channel or "web"
    ext: str | None = None
    is_group = False
    try:
        from app.models.chat_session import ChatSession

        sid = chat_session_id
        if sid is None:
            try:
                sid = uuid.UUID(str(conversation_id))
            except (ValueError, TypeError):
                sid = None
        if sid is not None:
            async with async_session() as db:
                sess = (
                    await db.execute(select(ChatSession).where(ChatSession.id == sid))
                ).scalar_one_or_none()
            if sess is not None:
                if sess.source_channel:
                    resolved = sess.source_channel
                ext = sess.external_conv_id
                is_group = bool(getattr(sess, "is_group", False))
    except Exception:
        logger.exception("_resolve_session_channel failed for conv %s", conversation_id)
    return resolved, ext, is_group

__all__ = [name for name in globals() if not name.startswith("__")]
