"""Authentication email-verification routes."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_registration import _send_verification_email_task
from app.core.security import create_access_token
from app.database import get_db
from app.models.user import Identity, User
from app.schemas.schemas import IdentityOut, ResendVerificationRequest, TokenResponse, UserOut, VerifyEmailRequest

router = APIRouter()


@router.post("/verify-email")
async def verify_email(data: VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
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
    identity.is_active = True
    
    # 2. Activate all linked User accounts
    # email_verified is a proxy to Identity, so only update physical is_active column
    from sqlalchemy import update
    await db.execute(
        update(User)
        .where(User.identity_id == identity.id)
        .values(is_active=True)
    )
    
    await db.flush()
    await db.commit()

    # Refresh after commit to avoid MissingGreenlet during Pydantic validation
    await db.refresh(identity)

    # 3. Find a representative user for the token (for immediate login)
    user_result = await db.execute(
        select(User)
        .where(User.identity_id == identity.id)
        .order_by(User.created_at.desc())
        .limit(1)
    )
    user = user_result.scalar_one_or_none()

    # 4. Generate token and return full response for Auto Login (TokenResponse)
    effective_id = str(user.id) if user else str(identity.id)
    effective_role = user.role if user else "user"
    token = create_access_token(effective_id, effective_role)

    return TokenResponse(
        access_token=token,
        user=UserOut.model_validate(user) if user else None,
        identity=IdentityOut.model_validate(identity),
        needs_company_setup=user.tenant_id is None if user else True,
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
