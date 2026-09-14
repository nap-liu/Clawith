"""WeCom SSO callback; IM routing remains in the channel adapter."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.database import get_db
from app.models.identity import SSOScanSession

router = APIRouter()


@router.get("/auth/wecom/callback")
async def wecom_callback(
    code: str,
    request: Request,
    state: str = None,
    db: AsyncSession = Depends(get_db),
):
    from app.services.auth_provider import WeComAuthProvider
    from app.services.sso_login_state import (
        get_enabled_sso_provider,
        parse_sso_or_legacy_state,
        sso_browser_cookie_name,
        sso_completion_url,
        sso_error_url,
        verify_sso_browser_binding,
    )
    # 1. Resolve session to get tenant context
    sid, provider_id, login_query = parse_sso_or_legacy_state(state)
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

    # 1. Get WeCom provider config
    provider = await get_enabled_sso_provider(db, provider_id, "wecom", tenant_id)
    if not provider:
        return RedirectResponse(sso_error_url("provider_unavailable", login_query), status_code=302)

    # 2. Extract user info and login/register via RegistrationService
    try:
        auth_provider = WeComAuthProvider(provider=provider, config=provider.config or {})
        
        token_data = await auth_provider.exchange_code_for_token(code)
        access_token_str = token_data.get("access_token")
        if not access_token_str:
            return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)
            
        user_info = await auth_provider.get_user_info(access_token_str)
        if not user_info.provider_user_id:
            return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)
            
        # Find or Create User (handles Identity and OrgMember linking)
        user, _is_new = await auth_provider.find_or_create_user(
            db, user_info, tenant_id=tenant_id or provider.tenant_id
        )
    except Exception as e:
        logger.exception(f"WeCom login/register error: {e}")
        return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)


    # Standard login
    token = create_access_token(str(user.id), user.role)

    if sid:
        try:
            s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
            session = s_res.scalar_one_or_none()
            if session:
                session.status = "authorized"
                session.provider_type = "wecom"
                session.user_id = user.id
                session.access_token = token
                session.error_msg = None
                await db.commit()
                return RedirectResponse(sso_completion_url(sid, login_query), status_code=302)
        except Exception as e:
            logger.exception("Failed to update SSO session (wecom) %s", e)

    return RedirectResponse(sso_error_url("session_update_failed", login_query), status_code=302)
