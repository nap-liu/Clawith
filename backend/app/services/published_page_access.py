"""Small shared helpers for published-page authorization."""

import uuid
import math
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import _request_is_secure
from app.core.permissions import is_platform_admin_user
from app.models.agent import Agent
from app.models.published_page import PublishedPage, PublishedPageAccess
from app.models.user import User

PAGE_SESSION_COOKIE = "published_page_session"
PAGE_SESSION_HOURS = 12


def create_page_session(user_id: uuid.UUID, *, expires_at: float | None = None) -> str:
    settings = get_settings()
    payload = {
            "sub": str(user_id),
            "typ": "published_page_session",
            "exp": expires_at if expires_at is not None else datetime.now(timezone.utc) + timedelta(hours=PAGE_SESSION_HOURS),
        }
    if expires_at is not None:
        payload["absolute_exp"] = expires_at
    return jwt.encode(
        payload,
        settings.SECRET_KEY,
        algorithm="HS256",
    )


def set_page_session_cookie(response, request, user_id, *, expires_at=None):
    """Preserve the expiry of a temporary login across report refreshes."""
    access = getattr(request.state, "access_token_payload", None)
    if expires_at is None and access is not None:
        if access.get("login_code_hash"):
            expires_at = access["exp"]
    elif expires_at is None:
        try:
            payload = jwt.decode(request.cookies.get(PAGE_SESSION_COOKIE, ""),
                                 get_settings().SECRET_KEY, algorithms=["HS256"])
            if payload.get("typ") == "published_page_session" and payload.get("sub") == str(user_id):
                expires_at = payload.get("absolute_exp")
        except JWTError:
            pass
    max_age = PAGE_SESSION_HOURS * 3600 if expires_at is None else max(
        0, math.ceil(expires_at - datetime.now(timezone.utc).timestamp()),
    )
    response.delete_cookie(PAGE_SESSION_COOKIE, path="/p/", samesite="lax")
    response.set_cookie(PAGE_SESSION_COOKIE, create_page_session(user_id, expires_at=expires_at),
                        max_age=max_age, httponly=True, samesite="lax",
                        secure=_request_is_secure(request), path="/")


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
    if is_platform_admin_user(user) or user.role == "org_admin":
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
    page_tenant_id = page.tenant_id
    if page_tenant_id is None:
        page_tenant_id = await db.scalar(select(Agent.tenant_id).where(Agent.id == page.agent_id))
    if page_tenant_id != user.tenant_id:
        return False
    if is_platform_admin_user(user) or user.role == "org_admin":
        return True
    if page.user_id == user.id:
        return True
    creator_id = await db.scalar(select(Agent.creator_id).where(Agent.id == page.agent_id))
    return creator_id == user.id
