"""Shared fresh-authentication activation and final principal gating."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.user import User
from app.services.authentication_state import (
    require_active_authentication_principal,
    require_reactivatable_authentication_principal,
)
from app.services.directory_user_status import sync_tenant_user_statuses


async def activate_authenticated_source(
    db: AsyncSession,
    *,
    user: User,
    provider: IdentityProvider,
    member: OrgMember,
    method: str,
) -> User:
    """Project one freshly authenticated provider account into login state."""
    locked_member = await db.scalar(
        select(OrgMember)
        .where(OrgMember.id == member.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_member is None:
        raise ValueError("Authenticated source no longer exists")
    member = locked_member
    locked_user = await db.scalar(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_user is None:
        raise ValueError("Authenticated user no longer exists")
    user = locked_user
    await require_reactivatable_authentication_principal(db, user)
    if user.tenant_id is None:
        if provider.tenant_id is not None or member.tenant_id is not None or member.user_id != user.id:
            raise ValueError("Authenticated source is outside the principal scope")
        member.status = "active"
        user.is_active = True
        await db.flush()
        return await require_active_authentication_principal(db, user)
    if (
        (provider.tenant_id is not None and str(provider.tenant_id) != str(user.tenant_id))
        or str(member.tenant_id) != str(user.tenant_id)
        or member.provider_id != provider.id
        or member.user_id != user.id
        or not provider.is_active
    ):
        raise ValueError("Authenticated source is outside the principal scope")

    was_active = user.is_active
    member.status = "active"
    await db.flush()
    await sync_tenant_user_statuses(
        db,
        tenant_id=user.tenant_id,
        changed_provider_id=provider.id,
    )
    await db.refresh(user, attribute_names=["is_active"])
    if not was_active and user.is_active:
        db.add(
            AuditLog(
                user_id=user.id,
                action="authentication.source_reactivated",
                details={
                    "tenant_id": str(user.tenant_id),
                    "provider_id": str(provider.id),
                    "method": method,
                },
            )
        )
        await db.flush()
    return await require_active_authentication_principal(db, user)


async def activate_platform_password_source(
    db: AsyncSession,
    *,
    user: User,
) -> User:
    """Activate the selected tenant's Platform source after valid password auth."""
    return await _activate_platform_source(db, user=user, method="password")


async def activate_platform_admin_source(
    db: AsyncSession,
    *,
    user: User,
) -> User:
    """Persist an administrator's explicit enablement as a Platform source."""
    return await _activate_platform_source(db, user=user, method="admin_enable")


async def activate_platform_email_source(
    db: AsyncSession,
    *,
    user: User,
) -> User:
    """Activate the registration membership proven by its email token."""
    return await _activate_platform_source(db, user=user, method="email_verification")


async def _activate_platform_source(
    db: AsyncSession,
    *,
    user: User,
    method: str,
) -> User:
    await require_reactivatable_authentication_principal(db, user)
    if user.tenant_id is None:
        user.is_active = True
        await db.flush()
        return await require_active_authentication_principal(db, user)

    from app.services.registration_service import registration_service

    member = await registration_service.ensure_web_org_member(db, user)
    if member is None:
        raise ValueError("Platform login source could not be persisted")
    provider = await db.get(IdentityProvider, member.provider_id)
    if provider is None:
        raise ValueError("Platform login provider is unavailable")
    return await activate_authenticated_source(
        db,
        user=user,
        provider=provider,
        member=member,
        method=method,
    )
