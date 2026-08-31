from __future__ import annotations

from datetime import datetime, timezone
import json
import mimetypes
import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.agent_tools_channel_file_receipts import (
    _build_outbound_operation_key,
)
from app.services.agent_tools_media_delivery_core import (
    _PLATFORM_SESSION_CHANNELS,
    _describe_media_delivery_result,
    _external_media_filename,
)
from app.services.agent_tools_media_delivery_support import (
    _lock_outbound_operation,
    _outbound_media_slots,
)
from app.services.media_tool_contract import normalize_media_display_title


async def _replay_terminal_media_delivery(
    *,
    agent_id: uuid.UUID,
    origin_session_id: str | None,
    intent_id: str,
    origin_turn_anchor_id: uuid.UUID | None,
) -> dict | None:
    """Bound replay waits before they reserve a database connection."""
    async with _outbound_media_slots:
        return await _replay_terminal_media_delivery_under_slot(
            agent_id=agent_id,
            origin_session_id=origin_session_id,
            intent_id=intent_id,
            origin_turn_anchor_id=origin_turn_anchor_id,
        )


async def _replay_terminal_media_delivery_under_slot(
    *,
    agent_id: uuid.UUID,
    origin_session_id: str | None,
    intent_id: str,
    origin_turn_anchor_id: uuid.UUID | None,
) -> dict | None:
    """Return or safely converge a durable receipt before touching a managed URL."""
    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=intent_id or None,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    if not operation_key:
        return None
    recovered_event: dict | None = None
    recovered_session_id = ""
    result_payload: dict | None = None
    async with async_session() as db:
        await _lock_outbound_operation(db, operation_key)
        existing = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.external_event_key == operation_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is None or existing.role != "tool_call":
            return None
        try:
            stored_call = json.loads(existing.content or "")
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            stored_call = {}
        meta = existing.message_meta if isinstance(existing.message_meta, dict) else {}
        if str(meta.get("delivery_status") or "") == "pending":
            next_meta = {
                **meta,
                "delivery_status": "unknown",
                "delivery_code": "MEDIA_DELIVERY_STATE_UNKNOWN",
            }
            existing.message_meta = next_meta
            stored_args = stored_call.get("args")
            if not isinstance(stored_args, dict):
                stored_args = {}
            media_kind = str(next_meta.get("media_kind") or stored_args.get("media_type") or "")
            result_payload = _describe_media_delivery_result(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "unknown",
                    "code": "MEDIA_DELIVERY_STATE_UNKNOWN",
                    "media_kind": media_kind or None,
                    "intent_id": intent_id,
                    "session_id": str(existing.conversation_id),
                    "channel": str(next_meta.get("source_channel") or ""),
                    "message_id": str(existing.id),
                }
            )
            if not stored_args and media_kind:
                stored_args = {"media_type": media_kind}
            existing.content = json.dumps(
                {
                    **stored_call,
                    "name": str(stored_call.get("name") or "send_media"),
                    "call_id": str(stored_call.get("call_id") or intent_id),
                    "args": stored_args,
                    "status": "done",
                    "result": json.dumps(result_payload, ensure_ascii=False),
                    "reasoning_content": stored_call.get("reasoning_content"),
                },
                ensure_ascii=False,
            )
            recovered_session_id = str(existing.conversation_id)
            recovered_event = {
                "type": "tool_call",
                "id": str(existing.id),
                "message_id": str(existing.id),
                "name": str(stored_call.get("name") or "send_media"),
                "call_id": str(stored_call.get("call_id") or intent_id),
                "args": stored_args,
                "status": "done",
                "result": json.dumps(result_payload, ensure_ascii=False),
                "created_at": existing.created_at.isoformat() if existing.created_at else None,
            }
            await db.commit()
        else:
            if stored_call.get("status") != "done":
                return None
            try:
                stored_result = json.loads(stored_call.get("result") or "")
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                return None
            if not isinstance(stored_result, dict):
                return None
            status = str(stored_result.get("status") or "")
            if status == "sent":
                code = str(stored_result.get("code") or "")
                result_payload = _describe_media_delivery_result(
                    {
                        **stored_result,
                        "status": "already_sent",
                        "code": (
                            "MEDIA_SENT_CAPTION_FAILED" if code == "MEDIA_SENT_CAPTION_FAILED" else "MEDIA_ALREADY_SENT"
                        ),
                    }
                )
            elif status in {"already_sent", "failed", "unsupported", "unknown"}:
                result_payload = _describe_media_delivery_result(stored_result)
            else:
                return None

    if recovered_event is not None and recovered_session_id:
        try:
            from app.api.websocket import manager as ws_manager

            recovered_event["session_id"] = recovered_session_id
            await ws_manager.send_to_session(
                str(agent_id),
                recovered_session_id,
                recovered_event,
            )
        except Exception:
            logger.opt(exception=True).warning("[SessionMedia] Unknown-state recovery live mirror failed")
    return result_payload


async def _publish_external_media_to_session(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    media_url: str,
    media_kind: str,
    caption: str,
    intent_id: str,
    origin_session_id: str | None,
    origin_turn_anchor_id: uuid.UUID | None,
    allow_download: bool,
    tool_args: dict,
) -> str:
    """Publish an external HTTPS URL as one standard tool-call record."""
    try:
        target_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return json.dumps(
            _describe_media_delivery_result(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                }
            ),
            ensure_ascii=False,
        )
    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=intent_id or None,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    if not operation_key:
        return json.dumps(
            _describe_media_delivery_result(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "MISSING_DELIVERY_INTENT_ID",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                }
            ),
            ensure_ascii=False,
        )

    filename = _external_media_filename(media_url, media_kind)
    display_title = normalize_media_display_title(tool_args.get("title"))
    guessed_mime = mimetypes.guess_type(filename)[0]
    mime_type = guessed_mime if str(guessed_mime or "").startswith(f"{media_kind}/") else None
    events: list[dict] = []
    result_payload: dict
    async with async_session() as db:
        await _lock_outbound_operation(db, operation_key)
        existing = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.external_event_key == operation_key).with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            try:
                existing_call = json.loads(existing.content or "")
                existing_result = json.loads(existing_call.get("result") or "")
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                existing_result = None
            if isinstance(existing_result, dict):
                if existing_result.get("status") == "sent":
                    existing_result = {**existing_result, "status": "already_sent", "code": "MEDIA_ALREADY_SENT"}
                return json.dumps(existing_result, ensure_ascii=False)
            return json.dumps(
                _describe_media_delivery_result(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "unknown",
                        "code": "MEDIA_DELIVERY_STATE_UNKNOWN",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "message_id": str(existing.id),
                    }
                ),
                ensure_ascii=False,
            )

        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == target_session_id,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            return json.dumps(
                _describe_media_delivery_result(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "failed",
                        "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                    }
                ),
                ensure_ascii=False,
            )
        channel = str(session.source_channel or "").strip()
        if channel not in _PLATFORM_SESSION_CHANNELS:
            return json.dumps(
                _describe_media_delivery_result(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "unsupported",
                        "code": "EXTERNAL_URL_NOT_SUPPORTED_BY_CHANNEL",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(session.id),
                        "channel": channel,
                    }
                ),
                ensure_ascii=False,
            )

        receipt: ChatMessage | None = None
        receipt_tool_payload: dict = {}
        if str(origin_session_id or "") == str(session.id) and origin_turn_anchor_id:
            running_rows = (
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.agent_id == agent_id,
                            ChatMessage.conversation_id == str(session.id),
                            ChatMessage.role == "tool_call",
                            ChatMessage.external_event_key.is_(None),
                            ChatMessage.message_meta["turn_anchor_id"].as_string() == str(origin_turn_anchor_id),
                        )
                        .order_by(ChatMessage.created_at.desc())
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
            for candidate in running_rows:
                try:
                    candidate_payload = json.loads(candidate.content or "")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(candidate_payload, dict)
                    and str(candidate_payload.get("name") or "") == "send_media"
                    and str(candidate_payload.get("call_id") or "") == intent_id
                ):
                    receipt = candidate
                    receipt_tool_payload = dict(candidate_payload)
                    break

        claim_meta = {
            "direction": "outbound",
            "source_channel": channel,
            "target_session_id": str(session.id),
            "origin_session_id": str(origin_session_id or ""),
            "tool_call_id": intent_id,
            "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
            "delivery_claim": str(origin_session_id or "") != str(session.id),
            "delivery_status": "sent",
            "delivery_code": "EXTERNAL_MEDIA_PUBLISHED",
            "delivery_mode": "external_url",
            "source_mode": "external_url",
            "media_kind": media_kind,
            "attachments": [],
            "allow_download": allow_download,
            "caption_status": "sent" if caption.strip() else "not_requested",
            "display_title": display_title,
        }
        if receipt is None:
            receipt = ChatMessage(
                agent_id=agent_id,
                user_id=session.user_id,
                role="tool_call",
                content="",
                conversation_id=str(session.id),
                external_event_key=operation_key,
                message_meta=claim_meta,
            )
            db.add(receipt)
        else:
            receipt.external_event_key = operation_key
            receipt.message_meta = {**dict(receipt.message_meta or {}), **claim_meta}
        session.last_message_at = datetime.now(timezone.utc)
        await db.flush()
        result_payload = {
            "type": "platform_media_delivery",
            "version": 1,
            "status": "sent",
            "code": "EXTERNAL_MEDIA_PUBLISHED",
            "media_kind": media_kind,
            "url": media_url,
            "source_mode": "external_url",
            "filename": filename,
            **({"title": display_title} if display_title else {}),
            **({"mime_type": mime_type} if mime_type else {}),
            "message_id": str(receipt.id),
            "allow_download": allow_download,
            "intent_id": intent_id,
            "session_id": str(session.id),
            "channel": channel,
            "conversation_type": "group" if session.is_group else "person",
            "delivery_mode": "external_url",
        }
        receipt.content = json.dumps(
            {
                **receipt_tool_payload,
                "name": "send_media",
                "call_id": intent_id,
                "args": dict(tool_args),
                "status": "done",
                "result": json.dumps(result_payload, ensure_ascii=False),
                "reasoning_content": receipt_tool_payload.get("reasoning_content"),
            },
            ensure_ascii=False,
        )
        caption_row: ChatMessage | None = None
        if caption.strip():
            caption_row = ChatMessage(
                agent_id=agent_id,
                user_id=session.user_id,
                role="assistant",
                content=caption.strip(),
                conversation_id=str(session.id),
                external_event_key=f"{operation_key}:caption",
                message_meta={
                    "direction": "outbound",
                    "source_channel": channel,
                    "target_session_id": str(session.id),
                    "origin_session_id": str(origin_session_id or ""),
                    "tool_call_id": intent_id,
                    "delivery_status": "sent",
                    "media_caption_for": str(receipt.id),
                    "attachments": [],
                },
            )
            db.add(caption_row)
        await db.commit()
        await db.refresh(receipt)
        events.append(
            {
                "type": "tool_call",
                "id": str(receipt.id),
                "message_id": str(receipt.id),
                "name": "send_media",
                "call_id": intent_id,
                "args": dict(tool_args),
                "status": "done",
                "result": json.dumps(result_payload, ensure_ascii=False),
                "created_at": receipt.created_at.isoformat() if receipt.created_at else None,
            }
        )
        if caption_row is not None:
            from app.services.chat_message_serializer import serialize_chat_message_for_client

            await db.refresh(caption_row)
            caption_event = serialize_chat_message_for_client(caption_row, source_channel=channel)
            caption_event["type"] = "assistant_message_committed"
            events.append(caption_event)

    try:
        from app.api.websocket import manager as ws_manager

        for event in events:
            event["session_id"] = str(target_session_id)
            await ws_manager.send_to_session(str(agent_id), str(target_session_id), event)
    except Exception:
        logger.opt(exception=True).warning("[SessionMedia] External URL live mirror failed")
    return json.dumps(result_payload, ensure_ascii=False)


__all__ = [
    "_replay_terminal_media_delivery",
    "_replay_terminal_media_delivery_under_slot",
    "_publish_external_media_to_session",
]
