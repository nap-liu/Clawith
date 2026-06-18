"""MCP server authentication helpers.

Extracts a Bearer PAT from the MCP request context and resolves it to a
(User, tenant_id) pair via the PAT service.  Returns (None, None) when the
token is absent, malformed, revoked, or expired.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.pat_service import verify_pat, verify_pat_with_scope


def _headers_from_ctx(ctx):
    """Locate the HTTP request headers on an MCP tool Context.

    Primary path (verified against the mcp SDK's StreamableHTTP transport):
    ``ctx.request_context.request`` is the Starlette ``Request`` whose
    ``.headers`` carry the inbound HTTP headers. Two defensive fallbacks cover
    SDK-shape differences (``request_context.transport.headers`` and a direct
    ``ctx.request``); the unit tests exercise the primary path.
    """
    rc = getattr(ctx, "request_context", None)
    # Primary: request_context.request (Starlette Request) -> .headers
    req = getattr(rc, "request", None) if rc else None
    headers = getattr(req, "headers", None)
    # Fallback 1: request_context.transport.headers
    if headers is None and rc is not None:
        transport = getattr(rc, "transport", None)
        headers = getattr(transport, "headers", None)
    # Fallback 2: a direct ctx.request
    if headers is None:
        direct = getattr(ctx, "request", None)
        headers = getattr(direct, "headers", None)
    return headers


def _bearer_from_ctx(ctx) -> str | None:
    """Extract the raw Bearer token string from an MCP tool Context."""
    headers = _headers_from_ctx(ctx)
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


@dataclass
class PatContext:
    user: object          # app.models.user.User
    tenant_id: object     # uuid.UUID
    scope: str            # "read" | "write"


async def resolve_pat_context(ctx, db) -> "PatContext | None":
    """Return a PatContext for a valid PAT, or None when unauthenticated."""
    user, tid, scope = await verify_pat_with_scope(db, _bearer_from_ctx(ctx))
    if user is None:
        return None
    return PatContext(user=user, tenant_id=tid, scope=scope or "read")


def require_write(pc: "PatContext") -> bool:
    """True iff the PAT scope permits mutations."""
    return getattr(pc, "scope", "read") == "write"
