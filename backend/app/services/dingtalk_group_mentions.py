"""Short-lived DingTalk group webhook support for native @mentions."""

from __future__ import annotations

import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.models.chat_session import ChatSession

_WEBHOOK_KEY = "dingtalk_session_webhook_encrypted"
_WEBHOOK_EXPIRES_AT_KEY = "dingtalk_session_webhook_expires_at_ms"
_EXPIRY_SAFETY_MS = 30 * 1000


async def cache_group_session_webhook(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    external_conv_id: str,
    webhook: str,
    expires_at_ms: int | str | None,
) -> ChatSession | None:
    """Merge the latest temporary webhook into a freshly locked group Session."""
    value = str(webhook or "").strip()
    if not value:
        return None
    now_ms = int(time.time() * 1000)
    try:
        expiry = int(expires_at_ms)
    except (TypeError, ValueError):
        return None
    if expiry <= now_ms:
        return None

    # Lock and refresh only im_config. Suppressing autoflush is essential: a
    # stale identity-map copy must not be written before the fresh row is read.
    with db.no_autoflush:
        session = (
            await db.execute(
                select(ChatSession)
                .options(load_only(ChatSession.im_config))
                .where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.source_channel == "dingtalk",
                    ChatSession.external_conv_id == external_conv_id,
                    ChatSession.is_group.is_(True),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    if session is None:
        return None

    config = dict(session.im_config or {})
    try:
        existing_expiry = int(config.get(_WEBHOOK_EXPIRES_AT_KEY) or 0)
    except (TypeError, ValueError):
        existing_expiry = 0
    if config.get(_WEBHOOK_KEY) and existing_expiry >= expiry:
        return session

    config[_WEBHOOK_KEY] = encrypt_data(value, get_settings().SECRET_KEY)
    config[_WEBHOOK_EXPIRES_AT_KEY] = expiry
    session.im_config = config
    await db.flush()
    return session


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
