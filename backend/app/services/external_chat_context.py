"""User-provided reference JSON and retry identity shared by chat transports."""

import hashlib
import json

from app.services.chat_attachments import normalize_chat_message_attachments
from app.services.llm.failure_outcome import render_message


class MessageConflict(ValueError):
    """A client message ID already belongs to different user content."""


class InvalidExternalContext(ValueError):
    """The optional reference must be JSON, including finite JSON numbers."""


def external_context_metadata(frame: dict) -> dict:
    if "external_context" not in frame:
        return {}
    context = frame["external_context"]
    try:
        json.dumps(context, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidExternalContext from exc
    return {"external_context": context}


def render_external_context(content: str, metadata: dict) -> str:
    if "external_context" not in metadata:
        return content
    return content + "\n\n" + render_message("openapi.externalContextReference") + "\n" + json.dumps(
        metadata["external_context"], ensure_ascii=False, allow_nan=False,
    )


def context_message_fingerprint(content: str, metadata: dict) -> str:
    question, attachments = normalize_chat_message_attachments(content, metadata, metadata.get("source_channel"))
    value = {
        "question": question,
        "attachments": attachments,
        "context_present": "external_context" in metadata,
        "context": metadata.get("external_context"),
    }
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def require_same_context_message(existing, content: str, metadata: dict | None, user_id=None):
    previous = dict(existing.message_meta or {})
    incoming = dict(metadata or {})
    if "external_context" not in previous and "external_context" not in incoming:
        return
    if user_id is not None and (existing.sender_user_id or existing.user_id) != user_id:
        raise MessageConflict
    incoming.setdefault("source_channel", previous.get("source_channel"))
    previous_fingerprint = previous.get("context_payload_fingerprint") or context_message_fingerprint(existing.content, previous)
    if previous_fingerprint != context_message_fingerprint(content, incoming):
        raise MessageConflict
