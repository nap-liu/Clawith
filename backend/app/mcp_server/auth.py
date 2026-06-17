"""MCP server authentication helpers.

Extracts a Bearer PAT from the MCP request context and resolves it to a
(User, tenant_id) pair via the PAT service.  Returns (None, None) when the
token is absent, malformed, revoked, or expired.
"""

from __future__ import annotations

from app.services.pat_service import verify_pat


def _bearer_from_ctx(ctx) -> str | None:
    """Extract the raw Bearer token string from an MCP tool Context.

    Tries ctx.request_context.transport.headers first (StreamableHTTP
    transport), then falls back to ctx.request.headers (older path).
    Both Starlette Headers and plain dicts are supported via .get().
    """
    rc = getattr(ctx, "request_context", None)
    transport = getattr(rc, "transport", None) if rc else None
    headers = getattr(transport, "headers", None)
    if headers is None:
        req = getattr(ctx, "request", None)
        headers = getattr(req, "headers", None)
    if not headers:
        return None
    # Starlette Headers are case-insensitive; plain dicts are not — try both.
    auth = headers.get("authorization") or headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    return auth.split(" ", 1)[1].strip()


async def resolve_pat_user(ctx, db):
    """Return (User, tenant_id) for a valid PAT, or (None, None).

    The MCP viewer is always the human who owns the PAT.
    """
    return await verify_pat(db, _bearer_from_ctx(ctx))
