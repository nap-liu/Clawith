"""RFC 6749 client-credentials and RFC 6750 bearer protocol helpers."""
import base64
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote_plus

from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2
from fastapi.security.http import HTTPBase

from app.models.openapi_application import OpenAPIApplication

SCOPES = {"employees:read": "Read delegated user's visible digital employees",
          "auth:login": "Create temporary login links for delegated users"}
oauth2 = OAuth2(flows={"clientCredentials": {
    "tokenUrl": "/api/openapi/v1/auth/token", "scopes": SCOPES,
}}, scheme_name="SystemOAuth2", auto_error=False)
client_basic = HTTPBase(scheme="basic", scheme_name="ClientSecretBasic", auto_error=False)


@dataclass
class SystemContext:
    application: OpenAPIApplication
    scopes: set[str]


class OAuthFailure(Exception):
    def __init__(self, error: str | None, status: int = 400, *, basic=False, scope=None):
        self.error, self.status, self.basic, self.scope = error, status, basic, scope

    def response(self):
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
        if self.basic:
            headers["WWW-Authenticate"] = 'Basic realm="OpenAPI"'
        elif self.status in {401, 403}:
            headers["WWW-Authenticate"] = f'Bearer error="{self.error}"' if self.error else "Bearer"
            if self.scope:
                headers["WWW-Authenticate"] += f', scope="{self.scope}"'
        body = {"error": self.error, "error_description": self.error.replace("_", " ")} if self.error else {}
        return JSONResponse(body,
                            status_code=self.status, headers=headers)


async def client_request(request: Request, *, revoke=False):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise OAuthFailure("invalid_request")
    raw = await request.body()
    if len(raw) > 4096:
        raise OAuthFailure("invalid_request")
    try:
        form = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    except (ValueError, UnicodeError):
        raise OAuthFailure("invalid_request")
    # RFC 6749 section 3.2: unknown extensions are ignored. A request must not
    # repeat parameters or combine Basic with a second client auth mechanism.
    if any(len(values) != 1 for values in form.values()) or {"client_id", "client_secret"} & set(form):
        raise OAuthFailure("invalid_request")
    if ("token" if revoke else "grant_type") not in form:
        raise OAuthFailure("invalid_request")
    if not revoke and form["grant_type"][0] != "client_credentials":
        raise OAuthFailure("unsupported_grant_type")
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        raise OAuthFailure("invalid_client", 401, basic=True)
    try:
        raw_client = base64.b64decode(auth[6:], validate=True).decode("utf-8")
        client_id, secret = raw_client.split(":", 1)
        client_id, secret = unquote_plus(client_id), unquote_plus(secret)
        if not client_id or not secret or len(secret) > 256:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise OAuthFailure("invalid_client", 401, basic=True)
    return client_id, secret, form


async def client_credentials_request(request):
    client_id, secret, form = await client_request(request)
    if "scope" in form and not re.fullmatch(r'[\x21\x23-\x5b\x5d-\x7e]+(?: [\x21\x23-\x5b\x5d-\x7e]+)*', form["scope"][0]):
        raise OAuthFailure("invalid_scope")
    requested_scope = set(form["scope"][0].split(" ")) if "scope" in form else None
    return client_id, secret, requested_scope


def require_scope(context, scope):
    if scope not in context.scopes:
        raise OAuthFailure("insufficient_scope", 403, scope=scope)
