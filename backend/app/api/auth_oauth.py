"""Authentication OAuth routes."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.schemas import (
    AuthCodeExchangeRequest,
    IdentityBindRequest,
    IdentityUnbindRequest,
    MultiTenantResponse,
    OAuthAuthorizeResponse,
    OAuthCallbackRequest,
    TenantChoice,
    TokenResponse,
    UserOut,
)

router = APIRouter()


@router.get("/providers")
async def list_providers(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID | None = Query(None, description="Optional tenant ID"),
):
    """List all available identity providers."""
    from app.services.auth_registry import auth_provider_registry

    providers = await auth_provider_registry.list_providers(db, str(tenant_id) if tenant_id else None)
    return [{"id": str(p.id), "provider_type": p.provider_type, "name": p.name, "is_active": p.is_active} for p in providers]


# Redis keys for OAuth two-step tenant selection
_OAUTH_PENDING_PREFIX = "oauth_pending:"
_OAUTH_PENDING_TTL = 600  # 10 minutes


async def _cache_oauth_pending(
    pending_token: str,
    provider_type: str,
    provider_id: uuid.UUID,
    user_info_dict: dict,
    token_data: dict,
) -> None:
    """Store OAuth intermediate data in Redis for the two-step tenant-selection flow."""
    import json
    from app.core.events import get_redis
    r = await get_redis()
    payload = json.dumps({
        "provider_type": provider_type,
        "provider_id": str(provider_id),
        "user_info": user_info_dict,
        "token_data": token_data,
    })
    await r.set(f"{_OAUTH_PENDING_PREFIX}{pending_token}", payload, ex=_OAUTH_PENDING_TTL)


async def _get_oauth_pending(pending_token: str) -> dict | None:
    """Retrieve (and delete) cached OAuth data from Redis. Returns None if expired/missing."""
    import json
    from app.core.events import get_redis
    r = await get_redis()
    raw = await r.get(f"{_OAUTH_PENDING_PREFIX}{pending_token}")
    if not raw:
        return None
    # Single-use: delete immediately after retrieval
    await r.delete(f"{_OAUTH_PENDING_PREFIX}{pending_token}")
    return json.loads(raw)


@router.get("/{provider}/authorize", response_model=OAuthAuthorizeResponse)
async def authorize(
    provider: str,
    redirect_uri: str = Query(..., description="OAuth callback URI"),
    state: str = Query("", description="CSRF state parameter"),
    db: AsyncSession = Depends(get_db),
):
    """Start OAuth authorization flow for a provider."""
    from app.services.auth_registry import auth_provider_registry
    from app.services.sso_service import sso_service

    # Get provider
    auth_provider = await auth_provider_registry.get_provider(db, provider)
    if not auth_provider:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not supported")

    # Generate authorization URL
    try:
        auth_url = await auth_provider.get_authorization_url(redirect_uri, state)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to generate authorization URL for {provider}: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate authorization URL")

    return OAuthAuthorizeResponse(authorization_url=auth_url)


@router.post("/code/exchange", response_model=TokenResponse)
async def exchange_auth_code(
    data: AuthCodeExchangeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange an OAuth authorization code for a platform login token."""

    from app.services.auth_code_exchange import (
        resolve_platform_login_provider,
        validate_platform_login_channel,
    )
    from app.services.oauth_login import OAuthCodeLoginError, exchange_oauth_code_for_user

    validate_platform_login_channel(data.channel)
    auth_provider = await resolve_platform_login_provider(
        db,
        data.provider,
        purpose=data.purpose,
        redirect_uri=data.redirect_uri,
    )

    try:
        provider_tenant_id = getattr(getattr(auth_provider, "provider", None), "tenant_id", None)
        login_result = await exchange_oauth_code_for_user(
            db,
            auth_provider,
            data.code,
            tenant_id=str(provider_tenant_id) if provider_tenant_id else None,
        )
        user = login_result.user
    except OAuthCodeLoginError as e:
        raise HTTPException(status_code=e.status_code, detail=e.public_message) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Auth code exchange failed: provider={} error_type={}",
            data.provider,
            type(e).__name__,
        )
        raise HTTPException(status_code=500, detail="OAuth authentication failed")

    jwt_token = create_access_token(str(user.id), user.role)
    return TokenResponse(
        access_token=jwt_token,
        user=UserOut.model_validate(user),
        needs_company_setup=user.tenant_id is None,
    )


@router.post("/{provider}/callback", response_model=Any)
async def oauth_callback(
    provider: str,
    data: OAuthCallbackRequest,
    db: AsyncSession = Depends(get_db),
):
    """Handle OAuth callback — supports a two-step flow for multi-tenant selection.

    Step 1 (code provided): exchange code with provider, detect multiple tenants,
    cache user_info in Redis, return MultiTenantResponse with opaque pending_token.

    Step 2 (pending_token + tenant_id provided): retrieve cached user_info from Redis,
    call find_or_create_user with the chosen tenant_id, return TokenResponse.
    """
    import uuid as _uuid
    from app.models.tenant import Tenant
    from app.services.auth_registry import auth_provider_registry

    # ── Step 2: User has selected a tenant ───────────────────────────────────
    if data.pending_token and data.tenant_id:
        pending = await _get_oauth_pending(data.pending_token)
        if not pending:
            raise HTTPException(
                status_code=400,
                detail="OAuth session expired or invalid. Please sign in again.",
            )

        from app.models.identity import IdentityProvider
        from app.services.auth_provider import PROVIDER_CLASSES

        try:
            pending_provider_id = _uuid.UUID(pending["provider_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="OAuth session has no exact provider") from exc
        provider_model = await db.get(IdentityProvider, pending_provider_id)
        provider_class = PROVIDER_CLASSES.get(pending["provider_type"])
        if (
            provider_model is None
            or provider_class is None
            or str(provider_model.provider_type) != pending["provider_type"]
            or not provider_model.is_active
            or (
                provider_model.tenant_id is not None
                and str(provider_model.tenant_id) != str(data.tenant_id)
            )
        ):
            raise HTTPException(
                status_code=404,
                detail=f"Provider '{pending['provider_type']}' not supported",
            )
        auth_provider = provider_class(provider=provider_model)

        from app.services.auth_provider import ExternalUserInfo
        user_info = ExternalUserInfo(**pending["user_info"])

        user, _ = await auth_provider.find_or_create_user(db, user_info, tenant_id=data.tenant_id)
        if not user:
            raise HTTPException(status_code=500, detail="Failed to create user")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account is disabled")

        jwt_token = create_access_token(str(user.id), user.role)
        return TokenResponse(
            access_token=jwt_token,
            user=UserOut.model_validate(user),
            needs_company_setup=user.tenant_id is None,
        )

    # ── Step 1: Exchange code, detect multi-tenant ────────────────────────────
    if not data.code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    auth_provider = await auth_provider_registry.get_provider(db, provider)
    if not auth_provider:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not supported")

    try:
        token_data = await auth_provider.exchange_code_for_token(data.code, data.redirect_uri)
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to get access token from provider")

        user_info = await auth_provider.get_user_info(access_token)
        user, is_new = await auth_provider.find_or_create_user(db, user_info)

        if not user:
            raise HTTPException(status_code=500, detail="Failed to create user")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account is disabled")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OAuth callback failed for {provider}: {e}")
        raise HTTPException(status_code=500, detail="OAuth authentication failed")

    # Check if this identity has multiple tenant memberships
    if user.identity_id:
        all_users_result = await db.execute(
            select(User).where(User.identity_id == user.identity_id)
        )
        all_users = list(all_users_result.scalars().all())
        tenant_users = [u for u in all_users if u.tenant_id is not None]

        if len(tenant_users) > 1:
            # Cache the full user_info in Redis so Step 2 can reconstruct it
            pending_token = _uuid.uuid4().hex
            await _cache_oauth_pending(
                pending_token,
                provider,
                auth_provider.provider.id,
                {
                    "provider_type": user_info.provider_type,
                    "provider_union_id": user_info.provider_union_id,
                    "provider_user_id": user_info.provider_user_id,
                    "name": user_info.name,
                    "email": user_info.email,
                    "avatar_url": user_info.avatar_url,
                    "mobile": user_info.mobile,
                    "raw_data": user_info.raw_data,
                },
                token_data,
            )

            tenant_ids = [u.tenant_id for u in tenant_users]
            tenants_result = await db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))
            tenants_map = {str(t.id): t for t in tenants_result.scalars().all()}

            tenant_choices = [
                TenantChoice(
                    tenant_id=u.tenant_id,
                    tenant_name=tenants_map[str(u.tenant_id)].name if str(u.tenant_id) in tenants_map else "Unknown",
                    tenant_slug=tenants_map[str(u.tenant_id)].slug if str(u.tenant_id) in tenants_map else "",
                    logo_url=tenants_map[str(u.tenant_id)].logo_url if str(u.tenant_id) in tenants_map else None,
                )
                for u in tenant_users
            ]

            return MultiTenantResponse(
                requires_tenant_selection=True,
                login_identifier=user_info.email or "",
                tenants=tenant_choices,
                pending_token=pending_token,
            )

    # Single tenant (or new user with no tenant yet) — issue token directly
    jwt_token = create_access_token(str(user.id), user.role)
    return TokenResponse(
        access_token=jwt_token,
        user=UserOut.model_validate(user),
        needs_company_setup=user.tenant_id is None,
    )


@router.post("/{provider}/bind", response_model=UserOut)
async def bind_identity(
    provider: str,
    data: IdentityBindRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bind an external identity to the current user."""
    from app.services.auth_registry import auth_provider_registry
    from app.services.sso_service import sso_service

    # Get provider
    auth_provider = await auth_provider_registry.get_provider(db, provider)
    if not auth_provider:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not supported")

    try:
        # Exchange code for token
        token_data = await auth_provider.exchange_code_for_token(data.code)
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to get access token from provider")

        # Get user info
        user_info = await auth_provider.get_user_info(access_token)

        # Check if identity is already linked to another user
        lookup_provider_user_id = user_info.provider_user_id
        existing_user = await sso_service.check_duplicate_identity(
            db,
            provider,
            lookup_provider_user_id,
            identity_data=user_info.raw_data,
        )
        if existing_user and existing_user.id != current_user.id:
            raise HTTPException(
                status_code=409,
                detail="This identity is already linked to another account",
            )

        # Link identity to current user
        await sso_service.link_identity(
            db,
            str(current_user.id),
            provider,
            lookup_provider_user_id,
            user_info.raw_data,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Identity bind failed for {provider}: {e}")
        raise HTTPException(status_code=500, detail="Failed to bind identity")

    return UserOut.model_validate(current_user)


@router.post("/{provider}/unbind", response_model=UserOut)
async def unbind_identity(
    provider: str,
    data: IdentityUnbindRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unlink an external identity from the current user."""
    from app.services.sso_service import sso_service

    # Unlink identity
    success = await sso_service.unlink_identity(db, str(current_user.id), provider)
    if not success:
        raise HTTPException(status_code=404, detail=f"No linked identity found for provider '{provider}'")

    return UserOut.model_validate(current_user)
