"""Page-independent temporary login links backed by the existing login owner."""
import secrets
from datetime import timedelta
from urllib.parse import unquote, urlsplit

from jose import JWTError, jwt

from app.config import get_settings
from app.models.openapi_application import OpenAPICredential
from app.services.openapi_applications import digest, fail, now

AUDIENCE = "openapi-temporary-login"


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
    settings = get_settings()
    payload = {"aud": AUDIENCE, "jti": secrets.token_urlsafe(24),
               "app_id": str(app.id), "generation": app.generation,
               "redirect_uri": redirect_uri, "exp": expires_at}
    code = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    db.add(OpenAPICredential(token_hash=digest(code), application_id=app.id,
                            generation=app.generation, kind="login", user_id=user.id,
                            redirect_uri=redirect_uri, embed_origin=embed_origin,
                            launcher=launcher, expires_at=expires_at))
    await db.flush()
    return code


def verify_login_code(code):
    settings = get_settings()
    try:
        return jwt.decode(code, settings.JWT_SECRET_KEY,
                          algorithms=[settings.JWT_ALGORITHM], audience=AUDIENCE,
                          options={"require_aud": True, "require_exp": True, "require_jti": True})
    except JWTError:
        fail("invalid_login_code", 401)
