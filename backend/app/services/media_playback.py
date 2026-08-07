"""Short-lived, cookie-bound playback tickets for protected chat media."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass

from jose import JWTError, jwt

from app.config import get_settings
from app.core.events import get_redis

settings = get_settings()

PLAYBACK_COOKIE = "clawith_media_playback"
IDLE_TTL_SECONDS = int(os.getenv("MEDIA_PLAYBACK_IDLE_TTL_SECONDS", "600"))
ABSOLUTE_TTL_SECONDS = int(os.getenv("MEDIA_PLAYBACK_ABSOLUTE_TTL_SECONDS", "7200"))
MAX_RANGE_BYTES = int(os.getenv("MEDIA_PLAYBACK_MAX_RANGE_BYTES", str(8 * 1024 * 1024)))
_KEY_PREFIX = "media:playback:"


@dataclass(frozen=True)
class PlaybackTicket:
    ticket_id: str
    user_id: str
    agent_id: str
    path: str
    mime_type: str
    size_bytes: int
    absolute_expires_at: int
    version_token: str = ""
    message_id: str = ""


def _signature(ticket_id: str) -> str:
    return hmac.new(
        settings.JWT_SECRET_KEY.encode("utf-8"),
        f"media-playback:{ticket_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_signature(ticket_id: str, signature: str) -> bool:
    return bool(signature) and hmac.compare_digest(_signature(ticket_id), signature)


def create_cookie_token(user_id: str, absolute_expires_at: int) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "purpose": "media_playback",
            "exp": absolute_expires_at,
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_cookie_user(token: str | None) -> str | None:
    if not token:
        return None
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError:
        return None
    if payload.get("purpose") != "media_playback":
        return None
    return str(payload.get("sub") or "") or None


async def create_ticket(
    *,
    user_id: str,
    agent_id: str,
    path: str,
    mime_type: str,
    size_bytes: int,
    version_token: str = "",
    message_id: str = "",
) -> tuple[PlaybackTicket, str, str]:
    now = int(time.time())
    ticket = PlaybackTicket(
        ticket_id=secrets.token_urlsafe(24),
        user_id=user_id,
        agent_id=agent_id,
        path=path,
        mime_type=mime_type,
        size_bytes=size_bytes,
        absolute_expires_at=now + ABSOLUTE_TTL_SECONDS,
        version_token=version_token,
        message_id=message_id,
    )
    redis = await get_redis()
    await redis.setex(
        f"{_KEY_PREFIX}{ticket.ticket_id}",
        min(IDLE_TTL_SECONDS, ABSOLUTE_TTL_SECONDS),
        json.dumps(ticket.__dict__, separators=(",", ":")),
    )
    return ticket, _signature(ticket.ticket_id), create_cookie_token(user_id, ticket.absolute_expires_at)


async def load_ticket(ticket_id: str, *, touch: bool = False) -> PlaybackTicket | None:
    redis = await get_redis()
    key = f"{_KEY_PREFIX}{ticket_id}"
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        ticket = PlaybackTicket(**json.loads(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        await redis.delete(key)
        return None
    remaining = ticket.absolute_expires_at - int(time.time())
    if remaining <= 0:
        await redis.delete(key)
        return None
    if touch:
        await redis.expire(key, min(IDLE_TTL_SECONDS, remaining))
    return ticket


def error_detail(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    retry_after_ms: int | None = None,
    trace_id: str | None = None,
) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            **({"retry_after_ms": retry_after_ms} if retry_after_ms is not None else {}),
            **({"trace_id": trace_id} if trace_id else {}),
        }
    }
