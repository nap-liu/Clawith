"""Shared active-state gate for platform login principals."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.models.user import Identity, User


async def require_active_authentication_principal(
    db: AsyncSession,
    user: User | None,
    *,
    status_code: int = status.HTTP_403_FORBIDDEN,
) -> User:
    """Require the global Identity, tenant membership, and tenant to be active."""
    if user is None or not user.is_active or user.is_login_suspended:
        raise HTTPException(status_code=status_code, detail="Account is disabled")

    identity_id = user.identity_id
    identity = await db.get(Identity, identity_id) if identity_id else None
    if identity is None or not identity.is_active:
        raise HTTPException(status_code=status_code, detail="Account is disabled")

    if user.tenant_id is not None:
        tenant = await db.get(Tenant, user.tenant_id)
        if tenant is None or not tenant.is_active:
            raise HTTPException(
                status_code=status_code,
                detail="Organization is disabled",
            )
    return user


async def require_reactivatable_authentication_principal(
    db: AsyncSession,
    user: User | None,
    *,
    status_code: int = status.HTTP_403_FORBIDDEN,
) -> User:
    """Validate immutable/admin boundaries before fresh auth mutates a source.

    Provider-derived ``User.is_active`` is deliberately not checked here.  A
    fresh trusted login may make that projection active again.  Global,
    tenant, and explicit tenant-login suspension boundaries remain fail-closed.
    """
    if user is None or user.is_login_suspended:
        raise HTTPException(status_code=status_code, detail="Account is disabled")
    identity = await db.get(Identity, user.identity_id) if user.identity_id else None
    if identity is None or not identity.is_active:
        raise HTTPException(status_code=status_code, detail="Account is disabled")
    if user.tenant_id is not None:
        tenant = await db.get(Tenant, user.tenant_id)
        if tenant is None or not tenant.is_active:
            raise HTTPException(status_code=status_code, detail="Organization is disabled")
    return user
