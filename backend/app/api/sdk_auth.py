"""SDK OAuth identity endpoints — fetch user info via company OAuth2, without creating a clawith User."""

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from urllib.parse import urlparse, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import get_settings
from app.database import get_db
from app.models.identity import IdentityProvider
from app.services.auth_provider import OAuth2AuthProvider

settings = get_settings()

router = APIRouter(prefix="/sdk/auth", tags=["sdk_auth"])


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def sign_sdk_state(return_to: str, ttl_seconds: int = 600) -> str:
    payload = {
        "return_to": return_to,
        "nonce": uuid.uuid4().hex,
        "exp": int(time.time()) + ttl_seconds,
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(settings.SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def verify_sdk_state(state: str) -> dict | None:
    try:
        raw, sig = state.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(settings.SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        padding = "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw + padding).decode())
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


# ---------------------------------------------------------------------------
# Provider loader
# ---------------------------------------------------------------------------

async def _load_oauth2_provider(db: AsyncSession) -> OAuth2AuthProvider:
    # 单租户/单 oauth2 provider 假设：报告读者匿名、无 tenant 上下文，故取唯一启用的 provider。
    # 多租户部署需改为按 tenant_id 过滤（reader 的 tenant 可由 short_id 反查）。
    result = await db.execute(
        select(IdentityProvider).where(
            IdentityProvider.provider_type == "oauth2",
            IdentityProvider.is_active == True,  # noqa: E712
            IdentityProvider.sso_login_enabled == True,  # noqa: E712
        ).limit(1)
    )
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=503, detail="OAuth2 provider not configured")
    return OAuth2AuthProvider(provider=model)


# ---------------------------------------------------------------------------
# return_to validation
# ---------------------------------------------------------------------------

_RETURN_PATH = re.compile(r"^/p/[A-Za-z0-9_-]{1,32}$")


def _validate_return_to(return_to: str) -> str:
    u = urlparse(return_to)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise HTTPException(status_code=400, detail="return_to must be an absolute http(s) URL")
    if "@" in u.netloc:
        raise HTTPException(status_code=400, detail="return_to host must not contain userinfo")
    if not _RETURN_PATH.match(u.path):
        raise HTTPException(status_code=400, detail="return_to must point to a /p/<short_id> page")
    allowed = settings.SDK_ALLOWED_RETURN_HOSTS
    if allowed and u.hostname not in allowed:
        raise HTTPException(status_code=400, detail="return_to host not allowed")
    return f"{u.scheme}://{u.netloc}{u.path}"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/start")
async def sdk_auth_start(return_to: str, db: AsyncSession = Depends(get_db)):
    clean_return_to = _validate_return_to(return_to)
    provider = await _load_oauth2_provider(db)
    state = sign_sdk_state(clean_return_to)
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": clean_return_to,
        "scope": provider.scope or "openid",
        "state": state,
    }
    authorize_url = f"{provider.authorize_url}?{urlencode(params)}"
    return RedirectResponse(url=authorize_url, status_code=302)


class ExchangeRequest(BaseModel):
    code: str
    state: str


@router.post("/exchange")
async def sdk_auth_exchange(req: ExchangeRequest, db: AsyncSession = Depends(get_db)):
    data = verify_sdk_state(req.state)
    if not data:
        raise HTTPException(status_code=400, detail="invalid or expired state")
    redirect_uri = data["return_to"]

    provider = await _load_oauth2_provider(db)

    creds = base64.b64encode(f"{provider.client_id}:{provider.client_secret}".encode()).decode()
    async with httpx.AsyncClient(timeout=20) as client:
        token_resp = await client.post(
            provider.token_url,
            headers={"Authorization": f"Basic {creds}"},
            data={"grant_type": "authorization_code", "code": req.code, "redirect_uri": redirect_uri},
        )
    if token_resp.status_code != 200:
        logger.error(f"SDK OAuth token exchange failed HTTP {token_resp.status_code}: {token_resp.text}")
        raise HTTPException(status_code=502, detail="token exchange failed")
    try:
        token_json = token_resp.json() or {}
    except Exception:
        raise HTTPException(status_code=502, detail="token exchange returned non-JSON response")
    access_token = token_json.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="no access_token returned")

    user_info = await provider.get_user_info(access_token)
    if not user_info.provider_user_id:
        raise HTTPException(status_code=502, detail="userinfo response missing user ID field")

    return {
        "userId": user_info.provider_user_id,
        "userName": user_info.name,
        "mobile": user_info.mobile,
    }
