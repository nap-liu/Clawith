from __future__ import annotations

import json
import mimetypes
import re
import uuid
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable

from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.im_delivery import (
    DeliveryReceiptPersistenceError,
    IMDeliveryPart,
    IMDeliveryResult,
    append_delivery_part,
    attach_delivery_to_meta,
    register_delivery,
)

channel_file_sender: ContextVar = ContextVar('channel_file_sender', default=None)
channel_file_part_recorder: ContextVar[Callable[[IMDeliveryPart], Awaitable[None]] | None] = ContextVar(
    "channel_file_part_recorder", default=None
)

def _build_outbound_operation_key(
    *,
    agent_id: uuid.UUID,
    origin_session_id: str | None,
    tool_call_id: str | None,
    origin_turn_anchor_id: uuid.UUID | str | None = None,
) -> str | None:
    """Return the durable replay key for one messaging tool invocation."""
    if not tool_call_id:
        return None
    session_scope = str(origin_session_id or "no-session")
    turn_scope = str(origin_turn_anchor_id or "unanchored")
    return f"outbound:{agent_id}:{session_scope}:{turn_scope}:{tool_call_id}"[:500]

async def _claim_channel_file_receipt(
    *,
    agent_id: uuid.UUID,
    tool_call_id: str,
    origin_session_id: str,
    origin_turn_anchor_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Put the existing tool-call row in pending before any channel side effect."""
    if not tool_call_id or not origin_session_id:
        return None
    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=tool_call_id,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    if not operation_key:
        return None
    async with async_session() as db:
        existing = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.external_event_key == operation_key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None
        candidates = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == str(origin_session_id),
                    ChatMessage.role == "tool_call",
                    ChatMessage.external_event_key.is_(None),
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(20)
                .with_for_update()
            )
        ).scalars().all()
        for candidate in candidates:
            try:
                payload = json.loads(candidate.content or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                isinstance(payload, dict)
                and str(payload.get("name") or "") == "send_channel_file"
                and str(payload.get("call_id") or "") == tool_call_id
            ):
                current_meta = (
                    candidate.message_meta
                    if isinstance(candidate.message_meta, dict)
                    else {}
                )
                current_delivery = (
                    current_meta.get("delivery")
                    if isinstance(current_meta.get("delivery"), dict)
                    else {}
                )
                candidate.external_event_key = operation_key
                if str(payload.get("status") or "") != "running" or str(
                    current_delivery.get("status")
                    or current_meta.get("delivery_status")
                    or ""
                ) not in {"", "pending"}:
                    await db.commit()
                    return None
                candidate.message_meta = attach_delivery_to_meta(
                    {
                        **dict(current_meta),
                        "direction": "outbound",
                        "artifact_role": "channel_file",
                        "tool_call_id": tool_call_id,
                        "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
                    },
                    IMDeliveryResult.pending("im"),
                )
                await db.commit()
                return candidate.id
    return None

async def record_channel_file_part(part: IMDeliveryPart) -> None:
    """Persist one observed file artifact through the active tool receipt."""
    recorder = channel_file_part_recorder.get()
    if recorder is not None:
        try:
            await recorder(part)
        except DeliveryReceiptPersistenceError:
            raise
        except Exception as exc:
            raise DeliveryReceiptPersistenceError(
                "provider artifact receipt persistence failed"
            ) from exc

async def _supports_exact_file_session_route(
    agent_id: uuid.UUID,
    session_id: str,
) -> bool:
    try:
        target_session_id = uuid.UUID(session_id)
    except (TypeError, ValueError):
        return False
    async with async_session() as db:
        channel = (
            await db.execute(
                select(ChatSession.source_channel).where(
                    ChatSession.id == target_session_id,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
    return str(channel or "").strip() in {"dingtalk", "feishu", "slack"}

def _normalize_tool_workspace_rel_path(raw_path: str) -> str | None:
    path = raw_path.strip().replace("\\", "/")
    if not path:
        return None
    if path.startswith("/") or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", path):
        return None
    normalized = PurePosixPath(path)
    if normalized.is_absolute() or any(part == ".." for part in normalized.parts):
        return None
    return normalized.as_posix()

def _platform_file_delivery_result(file_path: Path, rel_path: str, message: str = "") -> str:
    payload = {
        "type": "platform_file_delivery",
        "path": rel_path,
        "filename": file_path.name,
        "message": message or "",
        "mime_type": mimetypes.guess_type(file_path.name)[0] or "application/octet-stream",
        "size": file_path.stat().st_size,
    }
    return json.dumps(payload, ensure_ascii=False)

__all__ = [
    "channel_file_sender",
    "channel_file_part_recorder",
    "_build_outbound_operation_key",
    "_claim_channel_file_receipt",
    "record_channel_file_part",
    "_supports_exact_file_session_route",
    "_normalize_tool_workspace_rel_path",
    "_platform_file_delivery_result",
    "append_delivery_part",
    "register_delivery",
]
