"""Client-facing serialization shared by history APIs and live broadcasts."""

from __future__ import annotations

from typing import Any

from app.services.chat_attachments import (
    normalize_attachment_metadata,
    normalize_chat_message_attachments,
)


def serialize_chat_message_for_client(
    message: Any,
    *,
    source_channel: str | None = None,
    sender_name: str | None = None,
    sender_user_id: Any = None,
    sender_agent_id: Any = None,
) -> dict[str, Any]:
    raw_content = str(getattr(message, "content", "") or "")
    role = str(getattr(message, "role", "") or "")
    meta = getattr(message, "message_meta", None)
    delivery = meta.get("delivery") if isinstance(meta, dict) and isinstance(meta.get("delivery"), dict) else {}
    recall = delivery.get("recall") if isinstance(delivery.get("recall"), dict) else {}
    recall_status = str(recall.get("status") or "")
    resolved_source = source_channel or (
        meta.get("source_channel") if isinstance(meta, dict) else None
    )
    if role == "user":
        display_content, attachments = normalize_chat_message_attachments(
            raw_content,
            meta,
            resolved_source,
        )
    else:
        display_content = "该消息已撤回" if recall_status == "recalled" else raw_content
        delivery_status = meta.get("delivery_status") if isinstance(meta, dict) else None
        attachments = (
            normalize_attachment_metadata(meta.get("attachments"))
            if isinstance(meta, dict) and delivery_status in {None, "sent"}
            else []
        )

    created_at = getattr(message, "created_at", None)
    message_id = getattr(message, "id", None)
    entry: dict[str, Any] = {
        "id": str(message_id) if message_id is not None else None,
        "role": role,
        "content": display_content,
        "display_content": display_content,
        "attachments": attachments,
        "created_at": created_at.isoformat() if created_at else None,
    }
    thinking = getattr(message, "thinking", None)
    if thinking and recall_status != "recalled":
        entry["thinking"] = thinking
    if recall_status:
        entry["recall_status"] = recall_status
    if sender_name:
        entry["sender_name"] = sender_name
    if sender_user_id:
        entry["sender_user_id"] = str(sender_user_id)
    if sender_agent_id:
        entry["sender_agent_id"] = str(sender_agent_id)
    return entry
