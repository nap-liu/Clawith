"""Authentication login routes."""

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth_registration import _send_verification_email_task
from app.core.security import create_access_token, set_access_token_cookie, verify_password_async
from app.database import get_db
from app.models.user import Identity, User
from app.schemas.schemas import IdentityOut, MultiTenantResponse, TenantChoice, TokenResponse, UserLogin, UserOut
from app.services.authentication_state import require_active_authentication_principal

router = APIRouter()


@router.post("/login", response_model=Any)
async def login(
    data: UserLogin,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    response: Response = None,
    request: Request = None,
):
    """Login with email/phone/username and password. Supports multi-tenant selection."""
    from app.services.platform_auth_policy import get_platform_auth_policy

    auth_policy = await get_platform_auth_policy(db)
    if not auth_policy.password_login_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password login is disabled. Use an enabled SSO provider.",
        )
    from app.models.tenant import Tenant
    from app.models.user import Identity, User

    # 1. Query Identity
    query = select(Identity).where(
        (Identity.email == data.login_identifier) |
        (Identity.phone == data.login_identifier) |
        (Identity.username == data.login_identifier)
    )
    result = await db.execute(query)
    identity = result.scalar_one_or_none()

    if not identity or not identity.password_hash or not await verify_password_async(data.password, identity.password_hash):
        logger.warning(f"[LOGIN] Invalid credentials for {data.login_identifier} identity_id={identity.id if identity else 'None'}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # 2. Check Global Activity & Verification
    if not identity.is_active:
         raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your account has been disabled.")

    if not identity.email_verified:
        from app.config import get_settings
        from sqlalchemy import update
        from app.services.system_email_service import resolve_email_config_async

        email_config = await resolve_email_config_async(db)
        if not email_config:
            identity.email_verified = True
            identity.is_active = True
            await db.execute(
                update(User)
                .where(User.identity_id == identity.id)
                .values(is_active=True)
            )
            await db.flush()
        else:
            # Find any user record (just for the task)
            user_res = await db.execute(select(User).where(User.identity_id == identity.id).limit(1))
            user = user_res.scalar_one_or_none()

            # Trigger email delivery in background
            if user:
                await _send_verification_email_task(user, background_tasks, get_settings(), db)

            # Consistent with identity-first flow: Return 403 Forbidden with verification intent
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "needs_verification": True,
                    "email": identity.email,
                    "message": "Please verify your email to continue."
                }
            )

    # 3. Find all User records (tenants)
    result = await db.execute(
        select(User)
        .outerjoin(Tenant, Tenant.id == User.tenant_id)
        .where(
            User.identity_id == identity.id,
            User.is_active.is_(True),
            or_(User.tenant_id.is_(None), Tenant.is_active.is_(True)),
        )
        .options(selectinload(User.identity))
    )
    valid_users = list(result.scalars().all())

    if not valid_users:
        # User has an identity but no tenant records? Should they create one?
        # Create a "tenant-less" user if needed, or redirect to company setup
        # For now, if no users, they need company setup.
        # But wait, register_init should have created one.
        if data.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account cannot access the selected organization.",
            )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No organization associated with this account.")

    # 4. Handle Tenant Selection
    if not data.tenant_id:
        # If multiple tenants, return choice
        if len(valid_users) > 1:
            tenant_ids = [u.tenant_id for u in valid_users if u.tenant_id]
            tenants_map = {}
            if tenant_ids:
                tenants_result = await db.execute(
                    select(Tenant).where(Tenant.id.in_(tenant_ids))
                )
                tenants_map = {str(t.id): t for t in tenants_result.scalars().all()}

            tenant_choices = []
            for u in valid_users:
                tenant = tenants_map.get(str(u.tenant_id)) if u.tenant_id else None
                tenant_choices.append(TenantChoice(
                    tenant_id=u.tenant_id,
                    tenant_name=tenant.name if tenant else "Create or Join Organization",
                    tenant_slug=tenant.slug if tenant else "",
                    logo_url=tenant.logo_url if tenant else None,
                ))

            return MultiTenantResponse(
                requires_tenant_selection=True,
                login_identifier=data.login_identifier,
                tenants=tenant_choices,
            )

        # Only one tenant
        user = valid_users[0]
    else:
        # Specific tenant requested (Dedicated Link flow)
        # Search for the user record in that tenant
        user = next((u for u in valid_users if u.tenant_id == data.tenant_id), None)

        # Cross-tenant access check
        if not user:
             # Even platform admins must have a valid record in the targeted tenant
             # when logging in via a dedicated tenant URL / tenant_id.
             raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account does not belong to the selected organization.",
            )


    if user.tenant_id:
        t_result = await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant and not tenant.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your organization has been disabled.",
            )

    # Tenant-scoped login: when accessing from a company-specific domain,
    # only allow users who belong to that company (platform_admin exempt)
    if data.tenant_id and user.role != "platform_admin" and user.tenant_id is not None:
        if str(user.tenant_id) != str(data.tenant_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account does not belong to this organization.",
            )

    await require_active_authentication_principal(db, user)
    needs_setup = user.tenant_id is None
    token = create_access_token(str(user.id), user.role)
    set_access_token_cookie(response, request, token)
    return TokenResponse(
        access_token=token,
        user=UserOut.model_validate(user),
        identity=IdentityOut.model_validate(identity),
        needs_company_setup=user.tenant_id is None,
    )
