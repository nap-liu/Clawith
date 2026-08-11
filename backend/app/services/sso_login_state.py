"""Signed SSO state that carries the original login query without storage."""

import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.identity import IdentityProvider


SSO_STATE_HOURS = 12
SSO_BROWSER_COOKIE_PREFIX = "sso_browser_binding_"


def create_sso_login_state(session_id: uuid.UUID, provider_id: uuid.UUID, login_query: str = "") -> str:
    return jwt.encode(
        {
            "sid": str(session_id),
            "provider_id": str(provider_id),
            "login_query": login_query.lstrip("?"),
            "typ": "sso_login",
            "exp": datetime.now(timezone.utc) + timedelta(hours=SSO_STATE_HOURS),
        },
        get_settings().SECRET_KEY,
        algorithm="HS256",
    )


def parse_sso_login_state(state: str | None) -> tuple[uuid.UUID, uuid.UUID, str] | None:
    if not state:
        return None
    try:
        payload = jwt.decode(state, get_settings().SECRET_KEY, algorithms=["HS256"])
        if payload.get("typ") != "sso_login":
            return None
        return uuid.UUID(payload["sid"]), uuid.UUID(payload["provider_id"]), str(payload.get("login_query") or "")
    except (JWTError, KeyError, TypeError, ValueError):
        return None


def sso_browser_cookie_name(session_id: uuid.UUID) -> str:
    return f"{SSO_BROWSER_COOKIE_PREFIX}{session_id.hex}"


def create_sso_browser_binding(session_id: uuid.UUID) -> str:
    return jwt.encode(
        {
            "sid": str(session_id),
            "typ": "sso_browser_binding",
            "exp": datetime.now(timezone.utc) + timedelta(hours=SSO_STATE_HOURS),
        },
        get_settings().SECRET_KEY,
        algorithm="HS256",
    )


def verify_sso_browser_binding(session_id: uuid.UUID, token: str | None) -> bool:
    if not token:
        return False
    try:
        payload = jwt.decode(token, get_settings().SECRET_KEY, algorithms=["HS256"])
        return payload.get("typ") == "sso_browser_binding" and payload.get("sid") == str(session_id)
    except JWTError:
        return False


def sso_completion_url(session_id: uuid.UUID, login_query: str = "") -> str:
    base = f"/sso/entry?sid={session_id}&complete=1"
    return f"{base}&{login_query}" if login_query else base


def sso_error_url(error_code: str, login_query: str = "") -> str:
    base = f"/sso/entry?error={error_code}"
    return f"{base}&{login_query}" if login_query else base


async def get_enabled_sso_provider(
    db: AsyncSession,
    provider_id: uuid.UUID,
    provider_type: str,
    tenant_id: uuid.UUID | None,
) -> IdentityProvider | None:
    query = select(IdentityProvider).where(
        IdentityProvider.id == provider_id,
        IdentityProvider.provider_type == provider_type,
        IdentityProvider.is_active.is_(True),
        IdentityProvider.sso_login_enabled.is_(True),
    )
    query = query.where(
        IdentityProvider.tenant_id == tenant_id if tenant_id else IdentityProvider.tenant_id.is_(None)
    )
    return await db.scalar(query)


def parse_sso_or_legacy_state(state: str | None) -> tuple[uuid.UUID | None, uuid.UUID | None, str]:
    parsed = parse_sso_login_state(state)
    if parsed:
        return parsed
    try:
        return uuid.UUID(str(state)), None, ""
    except (TypeError, ValueError):
        return None, None, ""
