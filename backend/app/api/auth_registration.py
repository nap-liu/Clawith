"""Authentication registration routes."""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, verify_password_async
from app.database import get_db
from app.models.user import Identity, User
from app.schemas.schemas import (
    RegisterInitRequest,
    RegisterInitResponse,
    SSORegisterRequest,
    TokenResponse,
    UserOut,
    UserRegister,
)

router = APIRouter()


@router.get("/registration-config")
async def get_registration_config(db: AsyncSession = Depends(get_db)):
    """Public endpoint — returns registration requirements (no auth needed)."""
    from app.models.system_settings import SystemSetting
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == "invitation_code_enabled"))
    setting = result.scalar_one_or_none()
    enabled = setting.value.get("enabled", False) if setting else False
    return {"invitation_code_required": enabled}


@router.get("/check-duplicate")
async def check_duplicate(
    email: str | None = Query(None, description="Email to check"),
    username: str | None = Query(None, description="Username to check"),
    db: AsyncSession = Depends(get_db),
):
    """Check if email or username already exists."""
    from app.models.user import Identity, User
    result = {"email_exists": False, "username_exists": False, "conflicts": []}

    if email:
        # Check Identity email
        existing = await db.execute(
            select(Identity).where(Identity.email == email)
        )
        if existing.scalar_one_or_none():
            result["email_exists"] = True
            result["conflicts"].append({"type": "email", "scope": "global", "message": "Email already registered"})

    if username:
        existing = await db.execute(select(Identity).where(Identity.username == username))
        if existing.scalar_one_or_none():
            result["username_exists"] = True
            result["conflicts"].append({"type": "username", "scope": "global", "message": "Username already taken"})

    result["has_conflict"] = result["email_exists"] or result["username_exists"]
    return result


async def _send_verification_email_task(
    user: User,
    background_tasks: BackgroundTasks,
    settings: Any,
    db: AsyncSession,
) -> None:
    """Helper to create verification token and add email task to background tasks."""
    # Check if email is configured — either via DB (platform settings UI) or env vars.
    # We must check the DB config too, since most users configure SMTP via the UI.
    from app.services.system_email_service import resolve_email_config_async
    email_config = await resolve_email_config_async(db)
    if not email_config:
        logger.debug("No email config found (env or DB), skipping verification email")
        return

    from app.services.email_verification_service import email_verification_service

    try:
        # Get identity for this user
        res = await db.execute(select(Identity).where(Identity.id == user.identity_id))
        identity = res.scalar_one_or_none()

        if not identity:
            logger.warning(f"No identity found for user {user.id} ({user.email}). Cannot send verification.")
            return

        raw_code, expires_at = await email_verification_service.create_email_verification_token(identity.id, identity.email)
        expiry_minutes = int((expires_at - datetime.now(timezone.utc)).total_seconds() // 60)

        background_tasks.add_task(
            email_verification_service.send_verification_email,
            identity.email,
            user.display_name or identity.username or "User",
            raw_code,
            expiry_minutes,
        )
    except Exception as exc:
        logger.error(f"Failed to create verification token for {user.email}: {exc}")
        logger.warning(f"Failed to send verification email for {user.email}: {exc}")


@router.post("/register", response_model=Any, status_code=status.HTTP_201_CREATED)
async def register(
    data: UserRegister,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Legacy registration endpoint - kept for backward compatibility.

    For new implementations, use:
    - /register/init - Step 1: Initialize registration
    - /register/sso - SSO registration
    - /verify-email - Step 3: Verify email
    """
    from app.config import get_settings
    settings = get_settings()

    # Handle SSO registration if provider info provided
    if data.provider and data.provider_code:
        return await _handle_sso_register(data, db)

    # Regular username/password registration - delegate to new flow
    return await _handle_normal_register(data, background_tasks, db, settings)


@router.post("/register/init", response_model=RegisterInitResponse, status_code=status.HTTP_201_CREATED)
async def register_init(
    data: RegisterInitRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Step 1: Initialize registration with account credentials.

    Creates/finds a global Identity and a tenant-scoped User.
    """
    from app.config import get_settings
    settings = get_settings()
    from app.services.registration_service import registration_service
    from app.models.user import Identity, User

    logger.info(f"[REGISTER_INIT] Starting registration for email={data.email}")

    # Resolve email config once
    from app.services.system_email_service import resolve_email_config_async
    email_config = await resolve_email_config_async(db)

    # Check if this is the first user (platform admin setup) - Optimize with EXISTS
    is_first_user = (await db.execute(select(Identity.id).limit(1))).scalar() is None

    # Find or Create Identity
    identity = await registration_service.find_or_create_identity(
        db,
        email=data.email,
        username=data.username,
        password=data.password,
        is_platform_admin=is_first_user,
        email_config=email_config,
    )
    # Defense-in-depth: verify the returned identity actually belongs to the
    # submitted email. Under normal circumstances this should never trigger
    # (find_or_create_identity no longer uses username as a lookup key), but
    # this guard protects against future regressions.
    if identity.email and identity.email != data.email:
        logger.warning(
            "[REGISTER_INIT] Identity email mismatch: submitted=%s returned=%s — rejecting",
            data.email,
            identity.email,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken. Please choose a different username.",
        )

    # If identity existed, verify password
    if identity.password_hash and not await verify_password_async(data.password, identity.password_hash):
         raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email already registered. Incorrect password."
        )

    # For first user: auto-create/get default tenant
    tenant_uuid = None
    if is_first_user:
        from app.models.tenant import Tenant
        default = await db.execute(select(Tenant).where(Tenant.slug == "default"))
        tenant = default.scalar_one_or_none()
        if not tenant:
            tenant = Tenant(name="Default", slug="default", im_provider="web_only")
            db.add(tenant)
            await db.flush()
        tenant_uuid = tenant.id

    # Create User (tenant-scoped)
    # Check if user already exists in this tenant (if tenant_uuid is set)
    if tenant_uuid:
        existing_user_res = await db.execute(
            select(User).where(User.identity_id == identity.id, User.tenant_id == tenant_uuid)
        )
        user = existing_user_res.scalar_one_or_none()
    else:
        # Check for a "tenant-less" user (pending company setup)
        existing_user_res = await db.execute(
            select(User).where(User.identity_id == identity.id, User.tenant_id == None)
        )
        user = existing_user_res.scalar_one_or_none()

    if not user:
        user = await registration_service.create_user_with_identity(
            db,
            identity=identity,
            display_name=data.display_name or data.username,
            role="platform_admin" if is_first_user else "member",
            tenant_id=tenant_uuid,
        )
        # Set initial status
        user.is_active = is_first_user # Active immediately if first user
        user.email_verified = identity.email_verified
        await db.flush()

    # Generate token
    token = create_access_token(str(user.id), user.role)

    # Send verification email if not verified
    if not identity.email_verified:
        await _send_verification_email_task(user, background_tasks, settings, db)

    return RegisterInitResponse(
        user_id=user.id,
        email=identity.email,
        access_token=token,
        user=UserOut.model_validate(user),
        message="Registration initiated. Please verify your email." if not identity.email_verified else "Registration successful.",
        needs_company_setup=user.tenant_id is None,
        target_tenant_id=data.target_tenant_id,
    )


@router.post("/register/sso", response_model=TokenResponse)
async def register_sso(
    data: SSORegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """SSO registration - completely separate from normal registration flow.

    This endpoint handles OAuth-based registration/login via external providers.
    """
    from app.services.auth_registry import auth_provider_registry
    from app.services.registration_service import registration_service

    logger.info(f"[REGISTER_SSO] Starting SSO registration: provider={data.provider}")

    # Get provider
    auth_provider = await auth_provider_registry.get_provider(db, data.provider)
    if not auth_provider:
        raise HTTPException(status_code=400, detail=f"Provider '{data.provider}' not supported")

    # Perform SSO registration
    user, is_new, error = await registration_service.register_with_sso(
        db, data.provider, data.code, auth_provider
    )

    if error:
        raise HTTPException(status_code=400, detail=error)

    # If no tenant, check for email domain match
    if not user.tenant_id and user.email:
        tenant, _ = await registration_service.get_tenant_for_registration(
            db, email=user.email, invitation_code=data.invitation_code
        )
        if tenant:
            user.tenant_id = tenant.id
            await db.flush()

    # Generate token
    token = create_access_token(str(user.id), user.role)

    logger.info(f"[REGISTER_SSO] SSO successful: user_id={user.id}, is_new={is_new}")

    return TokenResponse(
        access_token=token,
        user=UserOut.model_validate(user),
        needs_company_setup=user.tenant_id is None,
    )


async def _handle_normal_register(data: UserRegister, background_tasks: BackgroundTasks, db: AsyncSession, settings):
    """Legacy normal registration handler."""
    logger.info(f"[REGISTER_LEGACY] email={data.email}")

    from app.services.registration_service import registration_service
    from sqlalchemy import func

    # Resolve email config once
    from app.services.system_email_service import resolve_email_config_async
    email_config = await resolve_email_config_async(db)

    # Check if first user - Optimize with EXISTS
    is_first_user = (await db.execute(select(User.id).limit(1))).scalar() is None

    # Resolve tenant
    tenant_uuid = None
    if is_first_user:
        from app.models.tenant import Tenant
        default = await db.execute(select(Tenant).where(Tenant.slug == "default"))
        tenant = default.scalar_one_or_none()
        if not tenant:
            tenant = Tenant(name="Default", slug="default", im_provider="web_only")
            db.add(tenant)
            await db.flush()
        tenant_uuid = tenant.id
        role = "platform_admin"
    else:
        tenant, _ = await registration_service.get_tenant_for_registration(
            db, email=data.email, invitation_code=data.invitation_code
        )
        if tenant:
            tenant_uuid = tenant.id
        role = "member"

    # 1. Check for existing Identity/Tenant-User
    from app.services.registration_service import registration_service

    # Check if this email is already registered globally
    identity_query = select(Identity).where(Identity.email == data.email)
    ident_res = await db.execute(identity_query)
    identity = ident_res.scalar_one_or_none()

    if identity:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered, please login directly."
        )

    # 2. Uniqueness Check (Already handled by Identity lookup above, but let's be explicit for Phone if needed)
    # conflicts = await registration_service.check_duplicate_identity(db, email=data.email)
    # ...

    # 3. Resolve or create Identity
    # If it's the first user, we auto-verify (trusted admin)
    identity = await registration_service.find_or_create_identity(
        db,
        email=data.email,
        username=data.username,
        password=data.password,
        is_platform_admin=is_first_user,
        email_config=email_config,
    )

    # Defense-in-depth: verify the returned identity actually belongs to the
    # submitted email. Should be unreachable after the username-lookup fix, but
    # acts as a safety net against future regressions.
    if identity.email and identity.email != data.email:
        logger.warning(
            "[REGISTER_LEGACY] Identity email mismatch: submitted=%s returned=%s — rejecting",
            data.email,
            identity.email,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken. Please choose a different username.",
        )

    if is_first_user:
        identity.email_verified = True
        identity.is_active = True
        await db.flush()

    # 4. Create Tenant User (Handles OrgMember binding and Participant creation)
    user = await registration_service.create_user_with_identity(
        db,
        identity=identity,
        display_name=data.display_name or data.username,
        role=role,
        tenant_id=tenant_uuid,
        registration_source="web",
        email_config=email_config,
    )

    # Seed default agents for first user
    if is_first_user:
        await db.commit()
        try:
            from app.services.agent_seeder import seed_default_agents
            await seed_default_agents(tenant_id=user.tenant_id, creator_id=user.id)
        except Exception as e:
            logger.warning(f"Failed to seed default agents: {e}")

    # Send verification email only when the identity still needs it. If the
    # platform has no system email configured, registration_service auto-verifies
    # the identity so local/self-hosted installs are not blocked.
    if not identity.email_verified:
        await _send_verification_email_task(user, background_tasks, settings, db)

    return RegisterInitResponse(
        user_id=user.id,
        email=user.email,
        access_token=create_access_token(str(user.id), user.role),
        user=UserOut.model_validate(user),
        message="Registration successful. Please verify your email." if not identity.email_verified else "Registration successful.",
        needs_company_setup=user.tenant_id is None,
    )


async def _handle_sso_register(data: UserRegister, db: AsyncSession):
    """Legacy SSO registration handler - delegates to new SSO endpoint logic."""
    # Redirect to new SSO flow
    sso_data = SSORegisterRequest(
        provider=data.provider,
        code=data.provider_code,
        invitation_code=data.invitation_code
    )
    return await register_sso(sso_data, db)
