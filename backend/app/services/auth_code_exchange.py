"""Platform-login OAuth authorization-code exchange helpers."""

from __future__ import annotations

import fnmatch
import uuid
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import IdentityProvider
from app.services.auth_provider import BaseAuthProvider, PROVIDER_CLASSES


PLATFORM_LOGIN_CHANNELS = {"web", "wechat_miniprogram"}


def validate_platform_login_channel(channel: str) -> str:
    normalized = (channel or "web").strip()
    if normalized not in PLATFORM_LOGIN_CHANNELS:
        raise HTTPException(status_code=400, detail="Unsupported channel")
    return normalized


def _matches_provider_key(model: IdentityProvider, provider_key: str) -> bool:
    config = model.config if isinstance(model.config, dict) else {}
    return str(config.get("provider_key") or "") == provider_key


async def resolve_platform_login_provider(
    db: AsyncSession,
    provider: str,
    *,
    purpose: str,
    redirect_uri: str,
) -> BaseAuthProvider:
    """Resolve an active SSO provider for platform-login code exchange."""

    provider = (provider or "").strip()
    if not provider:
        raise HTTPException(status_code=400, detail="Missing provider")

    model = await _resolve_provider_model(db, provider, purpose=purpose, redirect_uri=redirect_uri)
    _validate_provider_model(model, purpose=purpose, redirect_uri=redirect_uri)

    provider_class = PROVIDER_CLASSES.get(str(model.provider_type))
    if provider_class is None:
        raise HTTPException(status_code=400, detail="Provider type is not supported for code exchange")
    return provider_class(provider=model)


async def _resolve_provider_model(
    db: AsyncSession,
    provider: str,
    *,
    purpose: str,
    redirect_uri: str,
) -> IdentityProvider:
    try:
        provider_id = uuid.UUID(provider)
    except (TypeError, ValueError):
        provider_id = None

    if provider_id is not None:
        result = await db.execute(
            select(IdentityProvider).where(
                IdentityProvider.id == provider_id,
                IdentityProvider.is_active.is_(True),
                IdentityProvider.sso_login_enabled.is_(True),
            )
        )
        model = result.scalar_one_or_none()
        if not model:
            raise HTTPException(status_code=404, detail="OAuth provider not found")
        return model

    result = await db.execute(
        select(IdentityProvider).where(
            IdentityProvider.is_active.is_(True),
            IdentityProvider.sso_login_enabled.is_(True),
        )
    )
    active_models = list(result.scalars().all())

    by_key = [model for model in active_models if _matches_provider_key(model, provider)]
    if len(by_key) == 1:
        return by_key[0]
    if len(by_key) > 1:
        raise HTTPException(status_code=400, detail="OAuth provider key is ambiguous")

    by_type = [model for model in active_models if str(model.provider_type) == provider]
    if len(by_type) == 1:
        return by_type[0]
    eligible_by_type = [
        model for model in by_type if _provider_allows_purpose_and_redirect(model, purpose=purpose, redirect_uri=redirect_uri)
    ]
    if len(eligible_by_type) == 1:
        return eligible_by_type[0]
    if len(by_type) > 1:
        raise HTTPException(status_code=400, detail="OAuth provider type is ambiguous; use provider_key or provider id")

    raise HTTPException(status_code=404, detail="OAuth provider not found")


def _provider_allows_purpose_and_redirect(model: IdentityProvider, *, purpose: str, redirect_uri: str) -> bool:
    try:
        _validate_provider_model(model, purpose=purpose, redirect_uri=redirect_uri)
        return True
    except HTTPException:
        return False


def _validate_provider_model(model: IdentityProvider, *, purpose: str, redirect_uri: str) -> None:
    config = model.config if isinstance(model.config, dict) else {}
    allowed_purposes = config.get("allowed_purposes") or []
    if purpose not in allowed_purposes:
        raise HTTPException(status_code=403, detail="OAuth provider is not allowed for this purpose")

    parsed = urlparse(redirect_uri or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or "@" in parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")

    allowed_hosts = config.get("allowed_redirect_hosts") or []
    if not allowed_hosts or parsed.hostname not in allowed_hosts:
        raise HTTPException(status_code=400, detail="redirect_uri host not allowed")

    allowed_paths = config.get("allowed_redirect_paths") or []
    if not allowed_paths or not any(fnmatch.fnmatch(parsed.path, pattern) for pattern in allowed_paths):
        raise HTTPException(status_code=400, detail="redirect_uri path not allowed")
