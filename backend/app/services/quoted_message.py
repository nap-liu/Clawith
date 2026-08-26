"""Provider-neutral quoted-message metadata and LLM context rendering."""

from __future__ import annotations

import json
from typing import Any

from app.services.chat_attachments import normalize_attachment_metadata

QUOTED_MESSAGE_STATUSES = {"available", "partial", "unavailable", "failed"}
MAX_QUOTED_TEXT_LENGTH = 20_000
MAX_QUOTED_LABEL_LENGTH = 255
MAX_QUOTED_ID_LENGTH = 1024
MAX_QUOTED_ATTACHMENTS = 20


def _string(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def normalize_quoted_message(raw: Any) -> dict[str, Any] | None:
    """Return the safe cross-channel quoted-message contract."""
    if not isinstance(raw, dict):
        return None

    message_type = _string(raw.get("message_type"), limit=64).lower() or "unknown"
    provider_message_type = _string(raw.get("provider_message_type"), limit=128)
    content_status = _string(raw.get("content_status"), limit=32).lower()
    if content_status not in QUOTED_MESSAGE_STATUSES:
        content_status = "available"

    attachments = normalize_attachment_metadata(raw.get("attachments"))[:MAX_QUOTED_ATTACHMENTS]
    text = _string(raw.get("text"), limit=MAX_QUOTED_TEXT_LENGTH)
    result: dict[str, Any] = {
        "message_type": message_type,
        "content_status": content_status,
        "text": text,
        "attachments": attachments,
    }

    optional_strings = {
        "provider_message_id": MAX_QUOTED_ID_LENGTH,
        "sender_ref": MAX_QUOTED_ID_LENGTH,
        "sender_name": MAX_QUOTED_LABEL_LENGTH,
    }
    for key, limit in optional_strings.items():
        value = _string(raw.get(key), limit=limit)
        if value:
            result[key] = value
    if provider_message_type:
        result["provider_message_type"] = provider_message_type

    created_at_ms = raw.get("created_at_ms")
    if isinstance(created_at_ms, (int, float)) and created_at_ms >= 0:
        result["created_at_ms"] = int(created_at_ms)

    if not text and not attachments and content_status == "available":
        result["content_status"] = "unavailable"
    return result


def render_quoted_message_for_llm(content: str, raw_quote: Any) -> str:
    """Prepend one unambiguous quoted-message context block for the LLM."""
    quote = normalize_quoted_message(raw_quote)
    if quote is None:
        return content

    context: dict[str, Any] = {
        "message_type": quote["message_type"],
        "content_status": quote["content_status"],
    }
    for key in (
        "provider_message_type",
        "provider_message_id",
        "sender_ref",
        "sender_name",
        "created_at_ms",
    ):
        if key in quote:
            context[key] = quote[key]
    if quote.get("text"):
        context["text"] = quote["text"]
    if quote.get("attachments"):
        context["attachments"] = [
            {
                "display_name": item["display_name"],
                "path": item["path"],
                "kind": item["kind"],
            }
            for item in quote["attachments"]
        ]

    encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    return f"引用消息上下文：\n{encoded}\n\n当前消息：\n{content}"
