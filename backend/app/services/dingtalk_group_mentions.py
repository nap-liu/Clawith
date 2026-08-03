"""Short-lived DingTalk group webhook support for native @mentions."""

from __future__ import annotations

import time

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.models.chat_session import ChatSession

_WEBHOOK_KEY = "dingtalk_session_webhook_encrypted"
_WEBHOOK_EXPIRES_AT_KEY = "dingtalk_session_webhook_expires_at_ms"
_DEFAULT_WEBHOOK_TTL_MS = 60 * 60 * 1000
_EXPIRY_SAFETY_MS = 30 * 1000


def cache_group_session_webhook(
    session: ChatSession,
    *,
    webhook: str,
    expires_at_ms: int | str | None,
) -> None:
    """Store the latest temporary session webhook encrypted on a group Session."""
    value = str(webhook or "").strip()
    if not value or not session.is_group or session.source_channel != "dingtalk":
        return
    now_ms = int(time.time() * 1000)
    try:
        expiry = int(expires_at_ms) if expires_at_ms is not None else 0
    except (TypeError, ValueError):
        expiry = 0
    if expiry and expiry <= now_ms:
        return
    if not expiry:
        expiry = now_ms + _DEFAULT_WEBHOOK_TTL_MS

    config = dict(session.im_config or {})
    config[_WEBHOOK_KEY] = encrypt_data(value, get_settings().SECRET_KEY)
    config[_WEBHOOK_EXPIRES_AT_KEY] = expiry
    session.im_config = config


def load_group_session_webhook(session: ChatSession) -> str | None:
    """Return a still-valid decrypted webhook without leaking it to tool output."""
    if not session.is_group or session.source_channel != "dingtalk":
        return None
    config = session.im_config or {}
    encrypted = str(config.get(_WEBHOOK_KEY) or "")
    try:
        expires_at_ms = int(config.get(_WEBHOOK_EXPIRES_AT_KEY) or 0)
    except (TypeError, ValueError):
        return None
    if not encrypted or expires_at_ms <= int(time.time() * 1000) + _EXPIRY_SAFETY_MS:
        return None
    try:
        return decrypt_data(encrypted, get_settings().SECRET_KEY).strip() or None
    except ValueError:
        return None
