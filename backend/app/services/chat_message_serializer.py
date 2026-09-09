"""Client-facing serialization shared by history APIs and live broadcasts."""

from __future__ import annotations

from typing import Any

from app.services.chat_attachments import (
    normalize_attachment_metadata,
    normalize_chat_message_attachments,
)
from app.services.chat_history import parse_tool_call_for_display
from app.services.quoted_message import normalize_quoted_message


def serialize_chat_message_for_client(
    message: Any,
    *,
    source_channel: str | None = None,
    sender_name: str | None = None,
    sender_user_id: Any = None,
    sender_agent_id: Any = None,
    sender_avatar_url: str | None = None,
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
            if isinstance(meta, dict)
            and recall_status != "recalled"
            and delivery_status in {None, "sent"}
            else []
        )

    created_at = getattr(message, "created_at", None)
    message_id = getattr(message, "id", None)
    entry: dict[str, Any] = {
        "id": str(message_id) if message_id is not None else None,
        "role": role,
        "content": raw_content if role == "user" else display_content,
        "display_content": display_content,
        "attachments": attachments,
        "created_at": created_at.isoformat() if created_at else None,
    }
    if role == "user" and isinstance(meta, dict):
        if "external_context" in meta:
            entry["external_context"] = meta["external_context"]
        quoted_message = normalize_quoted_message(meta.get("quoted_message"))
        if quoted_message is not None:
            entry["quoted_message"] = quoted_message
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
    if sender_avatar_url:
        entry["sender_avatar_url"] = sender_avatar_url
    return entry


def serialize_tool_call_for_client(
    message: Any,
    *,
    source_channel: str | None = None,
    sender_name: str | None = None,
    sender_user_id: Any = None,
    sender_agent_id: Any = None,
    sender_avatar_url: str | None = None,
) -> dict[str, Any]:
    """Serialize one durable tool event through the shared client contract.

    The parsed fields are the public representation. Keeping the raw JSON in
    ``display_content`` duplicates every argument/result in history responses
    and bypasses the argument sanitizer, so both generic text fields are empty
    whenever parsing succeeds.
    """

    entry = serialize_chat_message_for_client(
        message,
        source_channel=source_channel,
        sender_name=sender_name,
        sender_user_id=sender_user_id,
        sender_agent_id=sender_agent_id,
        sender_avatar_url=sender_avatar_url,
    )
    entry["toolCallId"] = str(message.id)
    parsed = parse_tool_call_for_display(message.content)
    explicit_tool_call_id = bool(parsed.get("toolCallId"))
    if parsed:
        entry["content"] = ""
        entry["display_content"] = ""
        entry.update(parsed)
    if entry.get("toolName") == "request_confirmation":
        entry["toolCallId"] = str(message.id)
        explicit_tool_call_id = True
    entry["toolCallIdExplicit"] = explicit_tool_call_id
    return entry


def merge_tool_call_update_for_client(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """Project one append-only tool update without moving its timeline slot."""

    if previous.get("toolStatus") == "done" and current.get("toolStatus") == "running":
        return previous
    current["created_at"] = previous.get("created_at") or current.get("created_at")
    return current
