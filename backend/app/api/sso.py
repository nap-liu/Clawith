import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from loguru import logger
from app.models.identity import SSOScanSession, IdentityProvider
from app.schemas.schemas import TokenResponse, UserOut
from app.services.oauth_login import OAuthCodeLoginError, exchange_oauth_code_for_user

router = APIRouter(tags=["sso"])

@router.post("/sso/session")
async def create_sso_session(
    tenant_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db)
):
    """Create a new SSO scan session for QR code login."""
    session = SSOScanSession(
        id=uuid.uuid4(),
        status="pending",
        tenant_id=tenant_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)
    )
    db.add(session)
    await db.commit()
    return {"session_id": str(session.id), "expires_at": session.expires_at}

@router.get("/sso/session/{sid}/status")
async def get_sso_session_status(sid: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Check the status of an SSO scan session."""
    result = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    if session.expires_at < datetime.now(timezone.utc):
        session.status = "expired"
        await db.commit()

    response = {
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
        
        response["access_token"] = session.access_token
        if user:
            response["user"] = UserOut.model_validate(user).model_dump()
            
        # Mark as completed so it can't be reused
        session.status = "completed"
        await db.commit()
        
    return response

@router.put("/sso/session/{sid}/scan")
async def mark_sso_session_scanned(sid: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Optional: Mark session as 'scanned' when the landing page loads on mobile."""
    result = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
    session = result.scalar_one_or_none()
    if session and session.status == "pending":
        session.status = "scanned"
        await db.commit()
    return {"status": "ok"}

@router.get("/sso/config")
async def get_sso_config(sid: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """List active SSO providers with their redirect URLs for the specified session ID."""
    # 1. Resolve session to get tenant context
    res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
    session = res.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
        
    # 2. Query IdentityProviders for this tenant (only those that are active AND SSO-enabled)
    query = select(IdentityProvider).where(
        IdentityProvider.is_active == True,
        IdentityProvider.sso_login_enabled == True,
    )
    if session.tenant_id:
        query = query.where(IdentityProvider.tenant_id == session.tenant_id)
    else:
        # Fallback to global/unscoped if session has no tenant_id
        # In a fully isolated system, this might return empty results
        query = query.where(IdentityProvider.tenant_id.is_(None))

    result = await db.execute(query)
    providers = result.scalars().all()
    
    # Determine the base URL for OAuth callbacks:
    # Use resolve_base_url which has the correct 5-level fallback chain
    from app.core.domain import resolve_base_url
    public_base = await resolve_base_url(
        db, request=request,
        tenant_id=str(session.tenant_id) if session.tenant_id else None
    )
    
    auth_urls = []
    for p in providers:
        if p.provider_type == "feishu":
            app_id = p.config.get("app_id")
            if app_id:
                redir = f"{public_base}/api/auth/feishu/callback"
                url = f"https://open.feishu.cn/open-apis/authen/v1/index?app_id={app_id}&redirect_uri={quote(redir)}&state={sid}"
                auth_urls.append({"provider_type": "feishu", "name": p.name, "url": url})
        
        elif p.provider_type == "dingtalk":
            from app.services.auth_registry import auth_provider_registry
            auth_provider = await auth_provider_registry.get_provider(db, "dingtalk", str(session.tenant_id) if session.tenant_id else None)
            if auth_provider:
                redir = f"{public_base}/api/auth/dingtalk/callback"
                # Use provider's standardized authorization URL
                url = await auth_provider.get_authorization_url(redir, str(sid))
                auth_urls.append({"provider_type": "dingtalk", "name": p.name, "url": url})
                
        elif p.provider_type == "wecom":
            corp_id = p.config.get("corp_id")
            agent_id = p.config.get("agent_id")
            if corp_id and agent_id:
                # Callback implemented in app/api/wecom.py
                redir = f"{public_base}/api/auth/wecom/callback"
                url = f"https://open.work.weixin.qq.com/wwopen/sso/qrConnect?appid={corp_id}&agentid={agent_id}&redirect_uri={quote(redir)}&state={sid}"
                auth_urls.append({"provider_type": "wecom", "name": p.name, "url": url})
        elif p.provider_type == "google_workspace":
            from app.services.auth_registry import auth_provider_registry
            from app.services.google_workspace_oauth import (
                get_google_redirect_uri,
                sign_google_sso_state,
            )
            auth_provider = await auth_provider_registry.get_provider(
                db, "google_workspace", str(session.tenant_id) if session.tenant_id else None
            )
            if auth_provider:
                redir = await get_google_redirect_uri(db, p, request)
                auth_provider.config["redirect_uri"] = redir
                state = sign_google_sso_state(sid, p.id)
                url = await auth_provider.get_authorization_url(redir, state)
                auth_urls.append({"provider_type": "google_workspace", "name": p.name, "url": url})

        elif p.provider_type == "oauth2":
            from app.services.auth_registry import auth_provider_registry
            auth_provider = await auth_provider_registry.get_provider(
                db, "oauth2", str(session.tenant_id) if session.tenant_id else None
            )
            if auth_provider:
                redir = f"{public_base}/api/auth/oauth2/callback"
                url = await auth_provider.get_authorization_url(redir, str(sid))
                auth_urls.append({"provider_type": "oauth2", "name": p.name, "url": url})

    return auth_urls

@router.get("/auth/oauth2/callback")
async def oauth2_callback(
    code: str,
    state: str = None,
    db: AsyncSession = Depends(get_db),
):
    """Callback for Generic OAuth2 SSO login."""
    from app.core.security import create_access_token
    from fastapi.responses import HTMLResponse
    from app.services.auth_registry import auth_provider_registry

    # 1. 从 state (=sid) 获取 tenant 上下文
    tenant_id = None
    sid = None
    if state:
        try:
            sid = uuid.UUID(state)
            s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
            session = s_res.scalar_one_or_none()
            if session:
                tenant_id = session.tenant_id
        except (ValueError, AttributeError):
            pass

    # 2. 获取 OAuth2 provider
    auth_provider = await auth_provider_registry.get_provider(
        db, "oauth2", str(tenant_id) if tenant_id else None
    )
    if not auth_provider:
        return HTMLResponse("Auth failed: OAuth2 provider not configured")

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
        logger.warning(
            "OAuth2 login rejected: reason={} status_code={}",
            e.reason,
            e.status_code,
        )
        return HTMLResponse(f"Auth failed: {e.public_message}")
    except Exception as e:
        logger.error("OAuth2 login error: error_type={}", type(e).__name__)
        return HTMLResponse("Auth failed: OAuth authentication failed")

    # 4. 生成 JWT，更新 SSO session
    token = create_access_token(str(user.id), user.role)

    if sid:
        try:
            s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
            session = s_res.scalar_one_or_none()
            if session:
                session.status = "authorized"
                session.provider_type = "oauth2"
                session.user_id = user.id
                session.access_token = token
                session.error_msg = None
                await db.commit()
                return HTMLResponse(
                    f'<html><head><meta charset="utf-8" /></head>'
                    f'<body><div>SSO login successful. Redirecting...</div>'
                    f'<script>window.location.href = "/sso/entry?sid={sid}&complete=1";</script>'
                    f'</body></html>'
                )
        except Exception as e:
            logger.exception("Failed to update SSO session (oauth2) %s", e)

    return HTMLResponse(f"Logged in successfully.")
