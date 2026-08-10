"""Small shared helpers for published-page authorization."""

import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent import Agent
from app.models.published_page import PublishedPage, PublishedPageAccess
from app.models.user import User


PAGE_SESSION_COOKIE = "published_page_session"
PAGE_SESSION_HOURS = 12
PAGE_FRAME_TOKEN_MINUTES = 10
PAGE_FRAME_RECEIPT_COOKIE_PREFIX = "published_page_frame_receipt_"
PAGE_FRAME_RECEIPT_MINUTES = PAGE_FRAME_TOKEN_MINUTES


def create_page_session(user_id: uuid.UUID) -> str:
    settings = get_settings()
    return jwt.encode(
        {
            "sub": str(user_id),
            "typ": "published_page_session",
            "exp": datetime.now(timezone.utc) + timedelta(hours=PAGE_SESSION_HOURS),
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )


def decode_page_session(token: str | None) -> uuid.UUID | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, get_settings().SECRET_KEY, algorithms=["HS256"])
        if payload.get("typ") != "published_page_session":
            return None
        return uuid.UUID(payload["sub"])
    except (JWTError, KeyError, TypeError, ValueError):
        return None


def create_page_frame_token(page_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return jwt.encode(
        {
            "sub": str(user_id),
            "page_id": str(page_id),
            "jti": str(uuid.uuid4()),
            "typ": "published_page_frame",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=PAGE_FRAME_TOKEN_MINUTES),
        },
        get_settings().SECRET_KEY,
        algorithm="HS256",
    )


def verify_page_frame_token(token: str | None, page_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    if not token:
        return False
    try:
        payload = jwt.decode(token, get_settings().SECRET_KEY, algorithms=["HS256"])
        return (
            payload.get("typ") == "published_page_frame"
            and uuid.UUID(payload["page_id"]) == page_id
            and uuid.UUID(payload["sub"]) == user_id
        )
    except (JWTError, KeyError, TypeError, ValueError):
        return False


def create_page_frame_receipt(frame_token: str, page_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return jwt.encode(
        {
            "sub": str(user_id),
            "page_id": str(page_id),
            "frame_hash": hashlib.sha256(frame_token.encode()).hexdigest(),
            "typ": "published_page_frame_receipt",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=PAGE_FRAME_RECEIPT_MINUTES),
        },
        get_settings().SECRET_KEY,
        algorithm="HS256",
    )


def page_frame_receipt_cookie_name(frame_token: str) -> str:
    token_key = hashlib.sha256(frame_token.encode()).hexdigest()[:16]
    return f"{PAGE_FRAME_RECEIPT_COOKIE_PREFIX}{token_key}"


def verify_page_frame_receipt(
    receipt: str | None,
    frame_token: str,
    page_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    if not receipt:
        return False
    try:
        payload = jwt.decode(receipt, get_settings().SECRET_KEY, algorithms=["HS256"])
        expected_hash = hashlib.sha256(frame_token.encode()).hexdigest()
        return (
            payload.get("typ") == "published_page_frame_receipt"
            and uuid.UUID(payload["page_id"]) == page_id
            and uuid.UUID(payload["sub"]) == user_id
            and hmac.compare_digest(str(payload.get("frame_hash", "")), expected_hash)
        )
    except (JWTError, KeyError, TypeError, ValueError):
        return False


async def page_user_from_session(db: AsyncSession, token: str | None) -> User | None:
    user_id = decode_page_session(token)
    if not user_id:
        return None
    result = await db.execute(select(User).where(User.id == user_id, User.is_active.is_(True)))
    return result.scalar_one_or_none()


async def can_view_page(db: AsyncSession, page: PublishedPage, user: User | None) -> bool:
    if page.access_mode == "public":
        return True
    if user is None:
        return False
    if page.tenant_id is None:
        if page.user_id == user.id:
            return True
        creator_id = await db.scalar(select(Agent.creator_id).where(Agent.id == page.agent_id))
        return creator_id == user.id
    if user.tenant_id != page.tenant_id:
        return False
    if page.access_mode == "authenticated":
        return True
    if user.id == page.user_id:
        return True

    agent_creator = await db.scalar(select(Agent.creator_id).where(Agent.id == page.agent_id))
    if agent_creator == user.id:
        return True
    approved = await db.scalar(
        select(PublishedPageAccess.id).where(
            PublishedPageAccess.page_id == page.id,
            PublishedPageAccess.user_id == user.id,
            PublishedPageAccess.status == "approved",
        )
    )
    return approved is not None


async def can_manage_page(db: AsyncSession, page: PublishedPage, user: User) -> bool:
    if page.tenant_id is not None and page.tenant_id != user.tenant_id:
        return False
    if page.user_id == user.id:
        return True
    creator_id = await db.scalar(select(Agent.creator_id).where(Agent.id == page.agent_id))
    return creator_id == user.id
