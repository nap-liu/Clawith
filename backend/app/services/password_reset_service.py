"""Password reset token lifecycle helpers."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.events import get_redis
from app.models.system_settings import SystemSetting

# Key prefixes for Redis
TOKEN_PREFIX = "pwd_reset:token:"
USER_PREFIX = "pwd_reset:user:"


def _hash_token(token: str) -> str:
    """Hash a raw reset token before persistence or lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_password_reset_token(user_id: uuid.UUID) -> tuple[str, datetime]:
    """Create a new single-use token and invalidate older unused tokens in Redis."""
    redis = await get_redis()
    user_key = f"{USER_PREFIX}{user_id}"
    
    # Invalidate previous token for this user if exists
    old_token_hash = await redis.get(user_key)
    if old_token_hash:
        await redis.delete(f"{TOKEN_PREFIX}{old_token_hash}")

    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    
    now = datetime.now(timezone.utc)
    expiry_minutes = get_settings().PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    expires_at = now + timedelta(minutes=expiry_minutes)
    
    # Store the new token (bi-directional mapping for easy invalidation)
    token_key = f"{TOKEN_PREFIX}{token_hash}"
    ttl_seconds = int(expiry_minutes * 60)
    
    async with redis.pipeline(transaction=True) as pipe:
        pipe.setex(token_key, ttl_seconds, str(user_id))
        pipe.setex(user_key, ttl_seconds, token_hash)
        await pipe.execute()
        
    return raw_token, expires_at


async def get_public_base_url(db: AsyncSession) -> str:
    """Resolve the public base URL used for user-facing links."""
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == "platform"))
    setting = result.scalar_one_or_none()
    if setting and setting.value and setting.value.get("public_base_url"):
        return str(setting.value["public_base_url"]).strip().rstrip("/")

    env_value = getattr(get_settings(), "PUBLIC_BASE_URL", "") if hasattr(get_settings(), "PUBLIC_BASE_URL") else ""
    env_value = str(env_value).strip().rstrip("/")
    if env_value:
        return env_value

    raise RuntimeError(
        "Public base URL is not configured. Set platform public_base_url or PUBLIC_BASE_URL "
        "(required in production for reset links)."
    )


async def build_password_reset_url(db: AsyncSession, raw_token: str) -> str:
    """Build the user-facing reset URL."""
    base_url = await get_public_base_url(db)
    return f"{base_url}/reset-password?token={raw_token}"


async def consume_password_reset_token(raw_token: str) -> dict | None:
    """Load a valid reset token from Redis and mark it used (by deleting)."""
    redis = await get_redis()
    token_hash = _hash_token(raw_token)
    token_key = f"{TOKEN_PREFIX}{token_hash}"
    
    user_id_str = await redis.get(token_key)
    if not user_id_str:
        return None
        
    user_id = uuid.UUID(user_id_str)
    user_key = f"{USER_PREFIX}{user_id}"
    
    # Atomic delete to ensure single-use
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(token_key)
        pipe.delete(user_key)
        await pipe.execute()
    
    return {"user_id": user_id}
