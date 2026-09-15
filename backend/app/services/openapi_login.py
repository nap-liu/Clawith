"""Page-independent temporary login links backed by the existing login owner."""
import secrets
from datetime import timedelta
from urllib.parse import unquote, urlsplit

from jose import JWTError, jwt

from app.config import get_settings
from app.models.openapi_application import OpenAPICredential
from app.services.openapi_applications import digest, fail, now

AUDIENCE = "openapi-temporary-login"
AGENT_AUDIENCE = "agent-temporary-login"


def sign_login_code(payload: dict) -> str:
    settings = get_settings()
    return jwt.encode({"jti": secrets.token_urlsafe(24), **payload},
                      settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def redirect_target(app, value: str, public_base_url="") -> str:
    # Reject encoded network-path/CRLF/backslash ambiguities before URL parsing.
    decoded = unquote(value)
    if ("\\" in decoded or any(ord(char) < 32 for char in decoded)
            or decoded.startswith("//") or value != value.strip()):
        fail("invalid_redirect_uri", 400)
    parsed = urlsplit(value)
    if not parsed.scheme and not parsed.netloc and value.startswith("/"):
        return value
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        fail("invalid_redirect_uri", 400)
    allowed = set(app.redirect_origins)
    public = urlsplit(public_base_url)
    if public.scheme and public.netloc:
        allowed.add(f"{public.scheme}://{public.netloc}")
    if f"{parsed.scheme}://{parsed.netloc}" not in allowed:
        fail("redirect_origin_denied", 400)
    return value


async def issue_login_code(db, app, user, redirect_uri, embed_origin, *, launcher=None):
    expires_at = now() + timedelta(seconds=60)
    payload = {"aud": AUDIENCE,
               "app_id": str(app.id), "generation": app.generation,
               "redirect_uri": redirect_uri, "exp": expires_at}
    code = sign_login_code(payload)
    db.add(OpenAPICredential(token_hash=digest(code), application_id=app.id,
                            generation=app.generation, kind="login", user_id=user.id,
                            redirect_uri=redirect_uri, embed_origin=embed_origin,
                            launcher=launcher, expires_at=expires_at))
    await db.flush()
    return code


def verify_login_code(code, *, allow_agent=False, allow_expired_agent=False):
    settings = get_settings()
    try:
        payload = jwt.decode(code, settings.JWT_SECRET_KEY,
                             algorithms=[settings.JWT_ALGORITHM],
                             options={"verify_aud": False,
                                      "verify_exp": not allow_expired_agent,
                                      "require_jti": True})
        allowed = {AUDIENCE, AGENT_AUDIENCE} if allow_agent else {AUDIENCE}
        if payload.get("aud") not in allowed or not isinstance(payload.get("exp"), (int, float)):
            fail("invalid_login_code", 401)
        if allow_expired_agent and payload.get("aud") != AGENT_AUDIENCE and payload["exp"] <= now().timestamp():
            fail("invalid_login_code", 401)
        return payload
    except JWTError:
        fail("invalid_login_code", 401)
