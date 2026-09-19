"""Authentication email-verification routes."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_registration import _send_verification_email_task
from app.core.security import create_access_token, set_access_token_cookie
from app.database import get_db
from app.models.user import Identity, User
from app.schemas.schemas import IdentityOut, ResendVerificationRequest, TokenResponse, UserOut, VerifyEmailRequest

router = APIRouter()


@router.post("/verify-email")
async def verify_email(
    data: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
    response: Response = None,
    request: Request = None,
):
    """Verify email address using a token from the verification email.

    On success, returns user info and access token to allow immediate login.
    """
    from app.services.email_verification_service import email_verification_service

    token_data = await email_verification_service.consume_email_verification_token(data.token)
    if not token_data:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")

    identity_id = token_data.get("identity_id")
    if not identity_id:
        raise HTTPException(status_code=400, detail="Token does not contain identity information")

    # 1. Update Identity
    identity_result = await db.execute(select(Identity).where(Identity.id == identity_id))
    identity = identity_result.scalar_one_or_none()
    if not identity:
        raise HTTPException(status_code=400, detail="Identity not found")

    identity.email_verified = True

    await db.flush()
    token_user_id = token_data.get("user_id")
    user = await db.get(User, token_user_id) if token_user_id else None
    if user is None and token_user_id is None:
        legacy_users = list(
            (
                await db.scalars(
                    select(User).where(User.identity_id == identity.id).limit(2)
                )
            ).all()
        )
        user = legacy_users[0] if len(legacy_users) == 1 else None
    if user is None or user.identity_id != identity.id:
        raise HTTPException(status_code=400, detail="Verification token has no login membership")
    from app.services.authentication_principal import activate_platform_email_source

    user = await activate_platform_email_source(db, user=user)
    await db.commit()
    await db.refresh(identity)

    # 4. Generate token and return full response for Auto Login (TokenResponse)
    effective_id = str(user.id)
    effective_role = user.role
    token = create_access_token(effective_id, effective_role)
    set_access_token_cookie(response, request, token)

    return TokenResponse(
        access_token=token,
        user=UserOut.model_validate(user),
        identity=IdentityOut.model_validate(identity),
        needs_company_setup=user.tenant_id is None,
    )


@router.post("/resend-verification")
async def resend_verification(
    data: ResendVerificationRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Resend email verification link."""
    from app.config import get_settings
    from app.services.system_email_service import resolve_email_config_async

    # Always return success to prevent email enumeration
    generic_response = {
        "ok": True,
        "message": "If an account with that email exists, a verification email has been sent.",
    }
    settings = get_settings()

    # Check if email is configured (DB-only, no env fallback)
    email_config = await resolve_email_config_async(db)
    if not email_config:
        return generic_response

    # Find Identity by email
    id_result = await db.execute(select(Identity).where(Identity.email == data.email))
    identity = id_result.scalar_one_or_none()

    # Don't reveal if user exists or already verified
    if not identity or identity.email_verified:
        return generic_response

    # Pick a representative user context (e.g. latest one)
    u_result = await db.execute(
        select(User).where(User.identity_id == identity.id).order_by(User.created_at.desc()).limit(1)
    )
    user = u_result.scalar_one_or_none()

    if user:
        await _send_verification_email_task(user, background_tasks, settings, db)

    return generic_response
