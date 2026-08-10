"""Google Workspace OAuth callback routes."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import create_access_token, encrypt_data, get_current_admin
from app.database import get_db
from app.models.identity import SSOScanSession
from app.models.user import User
from app.services.auth_provider import GoogleWorkspaceAuthProvider
from app.services.google_workspace_oauth import (
    GOOGLE_CALLBACK_PATH,
    GOOGLE_SSO_STATE_KIND,
    GOOGLE_SYNC_STATE_KIND,
    get_google_provider,
    get_google_redirect_uri,
    parse_google_oauth_state,
    probe_google_directory,
    sign_google_oauth_state,
)

router = APIRouter(tags=["google_workspace"])
settings = get_settings()


@router.get("/enterprise/identity-providers/{provider_id}/google-workspace-sync/authorize-url")
async def get_google_workspace_sync_authorize_url(
    provider_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    provider = await get_google_provider(db, provider_id)
    if current_user.role != "platform_admin" and provider.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorized to manage this provider")

    config = provider.config or {}
    auth_provider = GoogleWorkspaceAuthProvider(provider=provider, config=config)
    if not auth_provider.client_id or not auth_provider.client_secret:
        raise HTTPException(status_code=400, detail="Please save Client ID and Client Secret first")

    redirect_uri = await get_google_redirect_uri(db, provider, request)
    state = sign_google_oauth_state(GOOGLE_SYNC_STATE_KIND, provider_id)
    url = await auth_provider.get_admin_authorization_url(redirect_uri, state)
    return {"authorization_url": url}


async def _handle_google_sso_callback(
    code: str,
    sid: uuid.UUID | None,
    provider_id: uuid.UUID | None,
    request: Request | None,
    db: AsyncSession,
    login_query: str = "",
):
    from app.services.sso_login_state import (
        get_enabled_sso_provider,
        sso_browser_cookie_name,
        sso_completion_url,
        sso_error_url,
        verify_sso_browser_binding,
    )
    if sid is None or provider_id is None:
        return RedirectResponse(sso_error_url("invalid_state", login_query), status_code=302)
    if not verify_sso_browser_binding(sid, request.cookies.get(sso_browser_cookie_name(sid))):
        return RedirectResponse(sso_error_url("browser_mismatch", login_query), status_code=302)
    tenant_id = None
    if sid:
        s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
        session = s_res.scalar_one_or_none()
        if session and session.expires_at >= datetime.now(timezone.utc) and session.status in {"pending", "scanned"}:
            tenant_id = session.tenant_id
        else:
            return RedirectResponse(sso_error_url("invalid_session", login_query), status_code=302)

    provider = await get_enabled_sso_provider(db, provider_id, "google_workspace", tenant_id)
    if not provider:
        return RedirectResponse(sso_error_url("provider_unavailable", login_query), status_code=302)

    auth_provider = GoogleWorkspaceAuthProvider(provider=provider, config=provider.config or {})

    redirect_uri = await get_google_redirect_uri(db, provider, request)
    auth_provider.config["redirect_uri"] = redirect_uri

    try:
        token_data = await auth_provider.exchange_code_for_token(code)
        access_token = token_data.get("access_token")
        if not access_token:
            logger.error(f"Google Workspace token exchange failed: {token_data}")
            return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)

        user_info = await auth_provider.get_user_info(access_token)
        user, _is_new = await auth_provider.find_or_create_user(
            db, user_info, tenant_id=str(tenant_id) if tenant_id else None
        )
        if not user:
            return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)
    except Exception as e:
        logger.error(f"Google Workspace login error: {e}")
        return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)

    token = create_access_token(str(user.id), user.role)

    if sid:
        try:
            s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
            session = s_res.scalar_one_or_none()
            if session:
                session.status = "authorized"
                session.provider_type = "google_workspace"
                session.user_id = user.id
                session.access_token = token
                session.error_msg = None
                await db.commit()
                return RedirectResponse(sso_completion_url(sid, login_query), status_code=302)
        except Exception as e:
            logger.exception("Failed to update SSO session (google_workspace) %s", e)

    return RedirectResponse(sso_error_url("session_update_failed", login_query), status_code=302)


async def _handle_google_admin_sync_callback(
    code: str,
    provider_id: uuid.UUID,
    request: Request,
    db: AsyncSession,
):
    provider = await get_google_provider(db, provider_id)
    redirect_uri = await get_google_redirect_uri(db, provider, request)
    config = provider.config or {}
    customer_id = config.get("customer_id") or "my_customer"
    auth_provider = GoogleWorkspaceAuthProvider(provider=provider, config=config)

    try:
        token_data = await auth_provider.exchange_code_for_token(code, redirect_uri=redirect_uri)
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        if not access_token or not refresh_token:
            raise RuntimeError("Google did not return a refresh token. Re-authorize with consent.")

        profile = await auth_provider.fetch_openid_profile(access_token)
        await probe_google_directory(access_token, customer_id)

        new_config = dict(config)
        new_config["google_admin_refresh_token_encrypted"] = encrypt_data(refresh_token, settings.SECRET_KEY)
        new_config["google_admin_authorized_email"] = profile.get("email", "")
        new_config["google_admin_authorized_at"] = datetime.now(timezone.utc).isoformat()
        provider.config = new_config
        await db.commit()
    except Exception as e:
        logger.error(f"Google Workspace admin sync authorization failed: {e}")
        await db.rollback()
        return RedirectResponse(
            "/oauth/admin-result?provider=google_workspace&status=error&code=authorization_failed",
            status_code=302,
        )
    return RedirectResponse(
        "/oauth/admin-result?provider=google_workspace&status=success",
        status_code=302,
    )


@router.get(GOOGLE_CALLBACK_PATH)
async def google_workspace_callback(
    code: str,
    state: str | None = None,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """Unified callback for Google Workspace SSO login and admin authorization."""
    from app.services.sso_login_state import parse_sso_login_state

    carried_state = parse_sso_login_state(state)
    if carried_state:
        sid, provider_id, login_query = carried_state
        return await _handle_google_sso_callback(code, sid, provider_id, request, db, login_query)
    parsed_state = parse_google_oauth_state(state) if state else None
    if parsed_state:
        state_kind, state_value = parsed_state
        if state_kind == GOOGLE_SYNC_STATE_KIND:
            return await _handle_google_admin_sync_callback(code, state_value[0], request, db)
        if state_kind == GOOGLE_SSO_STATE_KIND:
            sid = state_value[0]
            provider_id = state_value[1] if len(state_value) > 1 else None
            return await _handle_google_sso_callback(code, sid, provider_id, request, db)

    sid: uuid.UUID | None = None
    if state:
        try:
            sid = uuid.UUID(state)
        except (ValueError, AttributeError):
            return RedirectResponse("/sso/entry?error=invalid_state", status_code=302)

    return await _handle_google_sso_callback(code, sid, None, request, db)
