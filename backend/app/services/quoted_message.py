"""Provider-neutral quoted-message metadata and LLM context rendering."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.identity import IdentityProvider
from app.services.channel_user_service import channel_user_service
from app.services.chat_attachments import normalize_attachment_metadata

QUOTED_MESSAGE_STATUSES = {"available", "partial", "unavailable", "failed"}
QUOTED_SENDER_STATUSES = {"resolved", "unknown"}
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

    sender_status = _string(raw.get("sender_status"), limit=32).lower()
    sender_user_id = _canonical_actor_id(raw.get("sender_user_id"))
    sender_agent_id = _canonical_actor_id(raw.get("sender_agent_id"))
    if sender_status == "resolved" and bool(sender_user_id) != bool(sender_agent_id):
        result["sender_status"] = "resolved"
        if sender_user_id:
            result["sender_user_id"] = sender_user_id
        else:
            result["sender_agent_id"] = sender_agent_id
    elif sender_status in QUOTED_SENDER_STATUSES:
        result["sender_status"] = "unknown"
        result.pop("sender_name", None)

    created_at_ms = raw.get("created_at_ms")
    if isinstance(created_at_ms, (int, float)) and created_at_ms >= 0:
        result["created_at_ms"] = int(created_at_ms)

    if not text and not attachments and content_status == "available":
        result["content_status"] = "unavailable"
    return result


def _canonical_actor_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except (ValueError, TypeError, AttributeError):
        return ""


async def resolve_quoted_message_sender(
    db: AsyncSession,
    raw_quote: Any,
    *,
    provider: IdentityProvider | None,
    channel_type: str,
    provider_sender_id_type: str,
    agent: Agent | None = None,
    agent_provider_ref: str = "",
) -> dict[str, Any] | None:
    """Resolve a provider quote author to one canonical platform actor.

    Channel adapters only supply their provider field semantics.  The result is
    always the same cross-channel contract: a platform User, a platform Agent,
    or an explicit unknown actor.  Raw provider identifiers are never used as
    actor identities in LLM context.
    """
    quote = normalize_quoted_message(raw_quote)
    if quote is None:
        return None

    sender_ref = str(quote.get("sender_ref") or "").strip()
    quote.pop("sender_name", None)
    quote.pop("sender_user_id", None)
    quote.pop("sender_agent_id", None)
    quote["sender_status"] = "unknown"
    if not sender_ref:
        return quote

    normalized_agent_ref = str(agent_provider_ref or "").strip()
    agent_tenant_matches = (
        provider is None or agent is not None and agent.tenant_id == provider.tenant_id
    )
    if (
        agent is not None
        and agent_tenant_matches
        and normalized_agent_ref
        and sender_ref == normalized_agent_ref
    ):
        quote.update(
            {
                "sender_status": "resolved",
                "sender_agent_id": str(agent.id),
                "sender_name": agent.name,
            }
        )
        return quote

    if provider is None:
        return quote

    user = await channel_user_service.resolve_provider_user_alias(
        db,
        provider=provider,
        channel_type=channel_type,
        id_type=provider_sender_id_type,
        subject=sender_ref,
    )
    if user is not None:
        quote.update(
            {
                "sender_status": "resolved",
                "sender_user_id": str(user.id),
                "sender_name": user.display_name,
            }
        )
    return quote


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
        "created_at_ms",
    ):
        if key in quote:
            context[key] = quote[key]
    sender: dict[str, Any] = {"type": "unknown"}
    if quote.get("sender_status") == "resolved" and quote.get("sender_user_id"):
        sender = {
            "type": "user",
            "id": quote["sender_user_id"],
            "name": quote.get("sender_name") or "",
        }
    elif quote.get("sender_status") == "resolved" and quote.get("sender_agent_id"):
        sender = {
            "type": "agent",
            "id": quote["sender_agent_id"],
            "name": quote.get("sender_name") or "",
        }
    context["sender"] = sender
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
