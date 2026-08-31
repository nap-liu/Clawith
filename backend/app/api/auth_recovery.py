"""Authentication recovery routes."""

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password_async
from app.database import get_db
from app.models.user import Identity
from app.schemas.schemas import ForgotPasswordRequest, ResetPasswordRequest

router = APIRouter()


@router.get("/email-hint")
async def get_email_hint(username: str, db: AsyncSession = Depends(get_db)):
    """Return a hinted email address for a given username."""
    from app.models.user import Identity
    result = await db.execute(select(Identity).where(Identity.username == username))
    identity = result.scalar_one_or_none()
    
    if not identity or not identity.email:
        raise HTTPException(status_code=404, detail="Account not found.")
        
    email = identity.email
    parts = email.split("@")
    if len(parts) == 2:
        name, domain = parts
        
        # Obfuscate name
        if len(name) <= 2:
            obs_name = name[0] + "***"
        else:
            obs_name = name[:2] + "***" + name[-1]
            
        # Obfuscate domain
        domain_parts = domain.split(".")
        if len(domain_parts) >= 2:
            d_name = domain_parts[0]
            d_ext = ".".join(domain_parts[1:])
            if len(d_name) <= 2:
                obs_domain = d_name[0] + "***." + d_ext
            else:
                obs_domain = d_name[0] + "***" + d_name[-1] + "." + d_ext
            hint = f"{obs_name}@{obs_domain}"
        else:
            hint = f"{obs_name}@{domain}"
    else:
        hint = email[:3] + "***"
        
    return {"hint": hint}


@router.post("/forgot-password")
async def forgot_password(
    data: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Request a password reset link for a global Identity."""
    from app.services.system_email_service import resolve_email_config_async
    email_config = await resolve_email_config_async(db)

    if not email_config:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password reset is currently unavailable (no mail server configured)."
        )

    generic_response = {
        "ok": True,
        "message": "If an account with that email exists, a password reset email has been sent.",
    }

    # Find Identity by email
    identity_query = select(Identity).where(Identity.email == data.email)
    identity_result = await db.execute(identity_query)
    identity = identity_result.scalar_one_or_none()
    
    if not identity or not identity.is_active:
        return generic_response

    try:
        from app.services.password_reset_service import build_password_reset_url, create_password_reset_token
        from app.services.system_email_service import (
            send_password_reset_email,
        )

        raw_token, expires_at = await create_password_reset_token(identity.id)

        reset_url = await build_password_reset_url(db, raw_token)
        expiry_minutes = int((expires_at - datetime.now(timezone.utc)).total_seconds() // 60)
        background_tasks.add_task(
            send_password_reset_email,
            identity.email,
            identity.username or "User",
            reset_url,
            expiry_minutes,
        )
    except Exception as exc:
        logger.warning(f"Failed to process password reset email for {data.email}: {exc}")

    return generic_response


@router.post("/reset-password")
async def reset_password(data: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Reset a password using a valid single-use token."""
    from app.services.password_reset_service import consume_password_reset_token

    token_data = await consume_password_reset_token(data.token)
    if not token_data:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    identity_id = token_data["identity_id"]
    result = await db.execute(select(Identity).where(Identity.id == identity_id))
    identity = result.scalar_one_or_none()
    
    if not identity or not identity.is_active:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    new_hash = await hash_password_async(data.new_password)
    identity.password_hash = new_hash

    await db.flush()
    await db.commit()
    return {"ok": True}
