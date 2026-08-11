"""Small shared helpers for published-page authorization."""

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
