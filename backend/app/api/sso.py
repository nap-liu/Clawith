import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from loguru import logger
from app.models.identity import SSOScanSession, IdentityProvider
from app.schemas.schemas import UserOut
from app.services.oauth_login import OAuthCodeLoginError, exchange_oauth_code_for_user

router = APIRouter(tags=["sso"])

SSO_SESSION_HOURS = 12


class SSOStartRequest(BaseModel):
    tenant_id: uuid.UUID | None = None
    provider_type: str | None = None
    login_query: str = Field("", max_length=4000)


async def _enabled_providers(db: AsyncSession, tenant_id: uuid.UUID | None):
    query = select(IdentityProvider).where(
        IdentityProvider.is_active.is_(True),
        IdentityProvider.sso_login_enabled.is_(True),
    )
    query = query.where(
        IdentityProvider.tenant_id == tenant_id if tenant_id else IdentityProvider.tenant_id.is_(None)
    )
    result = await db.execute(
        query.order_by(IdentityProvider.sso_enabled_at.desc().nullslast(), IdentityProvider.id.desc())
    )
    return result.scalars().all()


async def _provider_auth_url(
    provider: IdentityProvider,
    session: SSOScanSession,
    request: Request,
    db: AsyncSession,
    login_query: str = "",
) -> str | None:
    from app.core.domain import resolve_base_url
    from app.services.sso_login_state import create_sso_login_state

    public_base = await resolve_base_url(
        db, request=request, tenant_id=str(session.tenant_id) if session.tenant_id else None
    )
    state = create_sso_login_state(session.id, provider.id, login_query)
    if provider.provider_type == "feishu":
        app_id = provider.config.get("app_id")
        if app_id:
            redir = f"{public_base}/api/auth/feishu/callback"
            return f"https://open.feishu.cn/open-apis/authen/v1/index?app_id={app_id}&redirect_uri={quote(redir)}&state={state}"
    elif provider.provider_type == "dingtalk":
        from app.services.auth_provider import DingTalkAuthProvider
        auth_provider = DingTalkAuthProvider(provider=provider, config=provider.config or {})
        return await auth_provider.get_authorization_url(f"{public_base}/api/auth/dingtalk/callback", state)
    elif provider.provider_type == "wecom":
        corp_id = provider.config.get("corp_id")
        agent_id = provider.config.get("agent_id")
        if corp_id and agent_id:
            redir = f"{public_base}/api/auth/wecom/callback"
            return f"https://open.work.weixin.qq.com/wwopen/sso/qrConnect?appid={corp_id}&agentid={agent_id}&redirect_uri={quote(redir)}&state={state}"
    elif provider.provider_type == "google_workspace":
        from app.services.auth_provider import GoogleWorkspaceAuthProvider
        from app.services.google_workspace_oauth import get_google_redirect_uri
        auth_provider = GoogleWorkspaceAuthProvider(provider=provider, config=provider.config or {})
        redir = await get_google_redirect_uri(db, provider, request)
        auth_provider.config["redirect_uri"] = redir
        return await auth_provider.get_authorization_url(redir, state)
    elif provider.provider_type == "oauth2":
        from app.services.auth_provider import OAuth2AuthProvider
        auth_provider = OAuth2AuthProvider(provider=provider)
        return await auth_provider.get_authorization_url(f"{public_base}/api/auth/oauth2/callback", state)
    return None


def _set_sso_browser_binding(response: Response, request: Request, session_id: uuid.UUID) -> None:
    from app.services.sso_login_state import (
        SSO_BROWSER_COOKIE_PREFIX,
        SSO_STATE_HOURS,
        create_sso_browser_binding,
        sso_browser_cookie_name,
    )

    forwarded_scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
    cookie_name = sso_browser_cookie_name(session_id)
    for stale_cookie_name in request.cookies:
        if stale_cookie_name.startswith(SSO_BROWSER_COOKIE_PREFIX) and stale_cookie_name != cookie_name:
            response.delete_cookie(stale_cookie_name, path="/", samesite="lax")
    response.set_cookie(
        cookie_name,
        create_sso_browser_binding(session_id),
        max_age=SSO_STATE_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=forwarded_scheme == "https",
        path="/",
    )


def _has_sso_browser_binding(request: Request, session_id: uuid.UUID) -> bool:
    from app.services.sso_login_state import sso_browser_cookie_name, verify_sso_browser_binding

    return verify_sso_browser_binding(session_id, request.cookies.get(sso_browser_cookie_name(session_id)))

@router.post("/sso/session")
async def create_sso_session(
    response: Response,
    request: Request,
    tenant_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db)
):
    """Create a new SSO scan session for QR code login."""
    session = SSOScanSession(
        id=uuid.uuid4(),
        status="pending",
        tenant_id=tenant_id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=SSO_SESSION_HOURS)
    )
    db.add(session)
    await db.commit()
    _set_sso_browser_binding(response, request, session.id)
    return {"session_id": str(session.id), "expires_at": session.expires_at}


@router.get("/sso/providers")
async def list_sso_providers(tenant_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db)):
    """Return provider metadata without creating expiring login state."""
    providers = await _enabled_providers(db, tenant_id)
    return [{"provider_type": p.provider_type, "name": p.name} for p in providers]


@router.post("/sso/start")
async def start_sso_login(
    payload: SSOStartRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Create a fresh 12-hour session only when login actually starts."""
    providers = await _enabled_providers(db, payload.tenant_id)
    provider = next((p for p in providers if p.provider_type == payload.provider_type), None) if payload.provider_type else (providers[0] if providers else None)
    if provider is None:
        return {"authorization_url": None}
    session = SSOScanSession(
        id=uuid.uuid4(), status="pending", tenant_id=payload.tenant_id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=SSO_SESSION_HOURS),
    )
    db.add(session)
    await db.flush()
    url = await _provider_auth_url(provider, session, request, db, payload.login_query)
    if not url:
        await db.rollback()
        return {"authorization_url": None}
    await db.commit()
    _set_sso_browser_binding(response, request, session.id)
    return {"authorization_url": url, "session_id": str(session.id), "expires_at": session.expires_at}

@router.get("/sso/session/{sid}/status")
async def get_sso_session_status(
    sid: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Check the status of an SSO scan session."""
    if not _has_sso_browser_binding(request, sid):
        raise HTTPException(status_code=403, detail="SSO session does not belong to this browser")
    result = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    if session.expires_at < datetime.now(timezone.utc):
        session.status = "expired"
        await db.commit()

    response_payload = {
        "status": session.status,
        "provider_type": session.provider_type,
        "error_msg": session.error_msg
    }
    
    if session.status == "authorized" and session.access_token:
        # Include token and user data once.
        # Must eagerly load the identity relationship because UserOut reads
        # hybrid properties (username, email, etc.) that proxy to Identity.
        from app.models.user import User
        from sqlalchemy.orm import selectinload
        user_result = await db.execute(
            select(User)
            .where(User.id == session.user_id)
            .options(selectinload(User.identity))
        )
        user = user_result.scalar_one_or_none()
        
        response_payload["access_token"] = session.access_token
        if user:
            response_payload["user"] = UserOut.model_validate(user).model_dump()
            
        # Mark as completed so it can't be reused
        session.status = "completed"
        await db.commit()
        from app.services.sso_login_state import sso_browser_cookie_name
        response.delete_cookie(sso_browser_cookie_name(sid), path="/", samesite="lax")
        
    return response_payload

@router.put("/sso/session/{sid}/scan")
async def mark_sso_session_scanned(sid: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Optional: Mark session as 'scanned' when the landing page loads on mobile."""
    if not _has_sso_browser_binding(request, sid):
        raise HTTPException(status_code=403, detail="SSO session does not belong to this browser")
    result = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
    session = result.scalar_one_or_none()
    if session and session.status == "pending":
        session.status = "scanned"
        await db.commit()
    return {"status": "ok"}

@router.get("/sso/config")
async def get_sso_config(sid: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """List active SSO providers with their redirect URLs for the specified session ID."""
    if not _has_sso_browser_binding(request, sid):
        raise HTTPException(status_code=403, detail="SSO session does not belong to this browser")
    # 1. Resolve session to get tenant context
    res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
    session = res.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
        
    providers = await _enabled_providers(db, session.tenant_id)
    auth_urls = []
    for p in providers:
        url = await _provider_auth_url(p, session, request, db)
        if url:
            auth_urls.append({"provider_type": p.provider_type, "name": p.name, "url": url})

    return auth_urls

@router.get("/auth/oauth2/callback")
async def oauth2_callback(
    code: str,
    request: Request,
    state: str = None,
    db: AsyncSession = Depends(get_db),
):
    """Callback for Generic OAuth2 SSO login."""
    from app.core.security import create_access_token
    from app.services.auth_provider import OAuth2AuthProvider
    from app.services.oauth_identity import oauth_identity_service, parse_oauth2_sso_state
    from app.services.sso_login_state import (
        get_enabled_sso_provider,
        parse_sso_login_state,
        sso_completion_url,
        sso_error_url,
    )

    # The signed state binds this callback to both the short-lived scan session
    # and the exact provider that issued the authorization code.
    carried_state = parse_sso_login_state(state)
    parsed_state = (carried_state[0], carried_state[1]) if carried_state else parse_oauth2_sso_state(state)
    if parsed_state is None:
        return RedirectResponse(sso_error_url("invalid_state"), status_code=302)
    sid, provider_id = parsed_state
    login_query = carried_state[2] if carried_state else ""
    if not _has_sso_browser_binding(request, sid):
        return RedirectResponse(sso_error_url("browser_mismatch", login_query), status_code=302)
    s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
    session = s_res.scalar_one_or_none()
    if (
        session is None
        or session.expires_at < datetime.now(timezone.utc)
        or session.status not in {"pending", "scanned"}
        or session.tenant_id is None
    ):
        return RedirectResponse(sso_error_url("invalid_session", login_query), status_code=302)

    provider_model = await get_enabled_sso_provider(db, provider_id, "oauth2", session.tenant_id)
    if provider_model is None:
        return RedirectResponse(sso_error_url("provider_unavailable", login_query), status_code=302)
    try:
        oauth_identity_service.validate_enterprise_provider(provider_model, session.tenant_id)
    except Exception:
        return RedirectResponse(sso_error_url("provider_unavailable", login_query), status_code=302)
    auth_provider = OAuth2AuthProvider(provider=provider_model)
    tenant_id = session.tenant_id

    # 3. 换 token → 获取用户信息 → 查找/创建用户
    try:
        login_result = await exchange_oauth_code_for_user(
            db,
            auth_provider,
            code,
            tenant_id=str(tenant_id) if tenant_id else None,
        )
        user = login_result.user
    except OAuthCodeLoginError as e:
        await db.rollback()
        logger.warning(
            "OAuth2 login rejected: reason={} status_code={}",
            e.reason,
            e.status_code,
        )
        return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)
    except Exception as e:
        await db.rollback()
        logger.error("OAuth2 login error: error_type={}", type(e).__name__)
        return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)

    # 4. 生成 JWT，更新 SSO session
    token = create_access_token(str(user.id), user.role)

    try:
        session.status = "authorized"
        session.provider_type = "oauth2"
        session.user_id = user.id
        session.access_token = token
        session.error_msg = None
        await db.commit()
        return RedirectResponse(sso_completion_url(sid, login_query), status_code=302)
    except Exception as e:
        logger.exception("Failed to update SSO session (oauth2) %s", e)

    return RedirectResponse(sso_error_url("session_update_failed", login_query), status_code=302)
