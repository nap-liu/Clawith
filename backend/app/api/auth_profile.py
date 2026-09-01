"""Authentication profile routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import create_access_token, get_authenticated_user, get_current_user, hash_password_async, verify_password_async
from app.database import get_db
from app.models.user import Identity, User
from app.schemas.schemas import TenantChoice, TenantSwitchRequest, TenantSwitchResponse, UserOut, UserUpdate

router = APIRouter()


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_authenticated_user)):
    """Get current user profile."""
    data = UserOut.model_validate(current_user)
    data.is_platform_admin = bool(getattr(getattr(current_user, "identity", None), "is_platform_admin", False))
    return data


@router.patch("/me", response_model=UserOut)
async def update_me(
    data: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update current user profile."""
    update_data = data.model_dump(exclude_unset=True)

    # Validate username uniqueness if changing
    if "username" in update_data and update_data["username"] != current_user.username:
        existing = await db.execute(
            select(User)
            .join(Identity, User.identity_id == Identity.id)
            .where(Identity.username == update_data["username"])
        )
        if existing.scalars().first():
            raise HTTPException(status_code=409, detail="Username already taken")

    # Validate email uniqueness within tenant if changing
    if "email" in update_data and update_data["email"] != current_user.email:
        existing = await db.execute(
            select(User)
            .join(Identity, User.identity_id == Identity.id)
            .where(
                Identity.email == update_data["email"],
                User.tenant_id == current_user.tenant_id,
                User.id != current_user.id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already registered")

    # Validate mobile uniqueness within tenant if changing
    if "primary_mobile" in update_data and update_data["primary_mobile"] != current_user.primary_mobile:
        existing = await db.execute(
            select(User)
            .join(Identity, User.identity_id == Identity.id)
            .where(
                Identity.phone == update_data["primary_mobile"],
                User.tenant_id == current_user.tenant_id,
                User.id != current_user.id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Mobile already registered")

    for field, value in update_data.items():
        setattr(current_user, field, value)
    await db.commit()
    await db.refresh(current_user)

    # Sync email/phone to OrgMember if changed
    if "email" in update_data or "primary_mobile" in update_data:
        from app.services.registration_service import registration_service
        await registration_service.sync_org_member_contact_from_user(
            db,
            current_user,
            sync_email="email" in update_data,
            sync_phone="primary_mobile" in update_data,
        )

    return UserOut.model_validate(current_user)


@router.get("/my-tenants", response_model=list[TenantChoice])
async def get_my_tenants(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all tenants associated with the current user's identity."""
    from app.models.tenant import Tenant

    # 1. Get all user records for this identity
    result = await db.execute(
        select(User).where(User.identity_id == current_user.identity_id)
    )
    users = result.scalars().all()

    # 2. Extract tenant IDs
    tenant_ids = [u.tenant_id for u in users if u.tenant_id]
    if not tenant_ids:
        return []

    # 3. Get tenant details
    result = await db.execute(
        select(Tenant).where(Tenant.id.in_(tenant_ids))
    )
    tenants = result.scalars().all()

    return [
        TenantChoice(
            tenant_id=t.id,
            tenant_name=t.name,
            tenant_slug=t.slug,
            logo_url=t.logo_url,
        ) for t in tenants
    ]


@router.post("/switch-tenant", response_model=TenantSwitchResponse)
async def switch_tenant(
    data: TenantSwitchRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Switch to a different tenant and return a new token and redirect URL."""
    from app.models.tenant import Tenant
    from app.models.system_settings import SystemSetting

    # 1. Verify membership
    result = await db.execute(
        select(User).where(
            User.identity_id == current_user.identity_id,
            User.tenant_id == data.tenant_id
        )
    )
    target_user = result.scalar_one_or_none()

    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this organization."
        )

    # 2. Get tenant details
    result = await db.execute(select(Tenant).where(Tenant.id == data.tenant_id))
    tenant = result.scalar_one_or_none()

    if not tenant or not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This organization is currently unavailable."
        )

    # 3. Generate new token
    token = create_access_token(str(target_user.id), target_user.role)

    # 4. Determine redirect URL
    # Determine redirect URL (Priority: sso_domain > ENV > Request > Fallback)
    from app.models.system_settings import SystemSetting

    # Global toggle (CP6 #591/#592): admins can disable custom-domain SSO redirect
    # platform-wide. When enabled (default), resolve via OUR domain-management chain
    # (resolve_base_url: tenant sso_domain → global → request) rather than upstream's
    # platform_service, so subdomain_prefix / three-tier fallback keep working.
    setting_result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "sso_custom_domain_redirect_enabled")
    )
    setting_s = setting_result.scalar_one_or_none()
    sso_redirect_enabled = setting_s.value.get("enabled", True) if setting_s else True

    if not sso_redirect_enabled:
        redirect_url = None
    else:
        from app.core.domain import resolve_base_url
        redirect_url = await resolve_base_url(db, request=request, tenant_id=str(tenant.id) if tenant else None)


    # Include token in redirect URL for cross-domain switching if needed
    if redirect_url:
        separator = "&" if "?" in redirect_url else "?"
        redirect_url = f"{redirect_url}{separator}token={token}"

    return TenantSwitchResponse(
        access_token=token,
        redirect_url=redirect_url,
        message="Switching organization..."
    )


@router.put("/me/password")
async def change_password(
    data: dict,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Change current user's password. Updates the global identity password."""
    old_password = data.get("old_password", "")
    new_password = data.get("new_password", "")

    if not old_password or not new_password:
        raise HTTPException(status_code=400, detail="Both old_password and new_password are required")

    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    # Access identity through current_user (TenantUser)
    res = await db.execute(select(User).where(User.id == current_user.id).options(selectinload(User.identity)))
    user = res.scalar_one()
    identity = user.identity

    if not identity or not identity.password_hash or not await verify_password_async(old_password, identity.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    new_hash = await hash_password_async(new_password)
    identity.password_hash = new_hash

    await db.flush()
    await db.commit()
    return {"ok": True}
