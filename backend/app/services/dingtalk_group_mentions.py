"""Short-lived DingTalk group webhook support for native @mentions."""

from __future__ import annotations

import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.database import async_session
from app.models.chat_session import ChatSession
from app.services.recipient_resolver import resolve_human_channel_recipient

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


async def load_group_session_webhook_by_id(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    expected_external_conv_id: str,
) -> str | None:
    """Reload a current webhook for the exact authorized conversation generation."""
    try:
        session_id = uuid.UUID(str(conversation_id))
    except (TypeError, ValueError):
        return None
    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == session_id,
                    ChatSession.agent_id == agent_id,
                    ChatSession.source_channel == "dingtalk",
                    ChatSession.external_conv_id == expected_external_conv_id,
                    ChatSession.is_group.is_(True),
                )
            )
        ).scalar_one_or_none()
    return load_group_session_webhook(session) if session is not None else None


async def prepare_group_user_mentions(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    canonical_user_ids: list[str],
) -> tuple[list[str], list[str]]:
    """Resolve canonical users to ephemeral DingTalk staff IDs and names."""
    target_ids: list[str] = []
    display_names: list[str] = []
    for canonical_user_id in canonical_user_ids:
        route = await resolve_human_channel_recipient(
            db,
            agent_id,
            canonical_user_id,
            channel="dingtalk",
        )
        staff_id = str(route.member.external_id or "").strip()
        if not staff_id:
            raise ValueError("dingtalk_staff_id_unavailable")
        if staff_id not in target_ids:
            target_ids.append(staff_id)
            display_names.append(
                str(route.user.display_name or route.member.name or "用户")
            )
    return target_ids, display_names
