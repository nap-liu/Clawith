"""RFC 8414 server metadata, built only from the configured public issuer."""
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.services.platform_service import platform_service
from app.services.openapi_oauth import SCOPES

router = APIRouter(tags=["OAuth server metadata"])


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(db: AsyncSession = Depends(get_db)):
    base = await platform_service.get_configured_public_base_url(db)
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        return JSONResponse({"error": "issuer_not_configured"}, status_code=503,
                            headers={"Cache-Control": "no-store"})
    return {"issuer": base, "token_endpoint": f"{base}/api/openapi/v1/auth/token",
            "revocation_endpoint": f"{base}/api/openapi/v1/auth/revoke",
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            "revocation_endpoint_auth_methods_supported": ["client_secret_basic"],
            "grant_types_supported": ["client_credentials"],
            "response_types_supported": [], "scopes_supported": sorted(SCOPES)}
