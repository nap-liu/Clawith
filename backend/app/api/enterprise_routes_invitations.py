"""Mechanically separated enterprise API route group."""

from app.api.enterprise_api_shared import *  # noqa: F401,F403


# ─── Invitation Codes ───────────────────────────────────

from app.models.invitation_code import InvitationCode


class InvitationCodeCreate(BaseModel):
    count: int = 1       # how many codes to generate
    max_uses: int = 1    # max registrations per code


def _require_tenant_admin(current_user: User) -> None:
    """Check that the user is org_admin or platform_admin with a tenant."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Requires admin privileges")
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No company assigned")


async def _ensure_invitation_email_enabled(db: AsyncSession) -> None:
    """Require enabled system email before accepting email invitations."""
    from app.services.system_email_service import resolve_email_config_async

    if await resolve_email_config_async(db):
        return
    if await resolve_email_config_async(db, include_disabled=True):
        raise HTTPException(
            status_code=400,
            detail="System email SMTP is configured but disabled. Enable system email before sending invitations.",
        )
    raise HTTPException(
        status_code=400,
        detail="System email SMTP settings are not configured. Configure system email before sending invitations.",
    )


@router.post("/invitation-codes")
async def create_invitation_codes(
    data: InvitationCodeCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Batch-create invitation codes for the current user's company."""
    _require_tenant_admin(current_user)
    import random
    import string

    codes_created = []
    for _ in range(min(data.count, 100)):  # cap at 100 per batch
        code_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        code = InvitationCode(
            code=code_str,
            tenant_id=current_user.tenant_id,
            max_uses=data.max_uses,
            created_by=current_user.id,
        )
        db.add(code)
        codes_created.append(code_str)

    await db.commit()
    return {"created": len(codes_created), "codes": codes_created}


@router.post("/invite-users")
async def invite_users(
    request: Request,
    data: UserInviteRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Batch-invite users via email to the current user's company."""
    _require_tenant_admin(current_user)
    if not data.emails:
        raise HTTPException(status_code=400, detail="No emails provided")
        
    import random
    import string

    from app.models.tenant import Tenant
    from app.services.platform_service import platform_service
    from app.services.system_email_service import send_company_invitation_email
    
    tenant_result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
    tenant = tenant_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")

    # Validate the invitation email channel is configured (upstream #613) before
    # building links — keep OUR three-tier-domain base URL resolver.
    await _ensure_invitation_email_enabled(db)

    base_url = await resolve_base_url(db, request=request)
    
    invited_count = 0
    codes = []
    
    for email in data.emails:
        email = email.lower().strip()
        if not email:
            continue
            
        code_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        code = InvitationCode(
            code=code_str,
            tenant_id=current_user.tenant_id,
            max_uses=1,
            created_by=current_user.id,
        )
        db.add(code)
        codes.append(code)
        
        invite_url = f"{base_url}/login?code={code_str}&email={email}"
        
        inviter_name = current_user.display_name or current_user.username
        
        # Use background task to send email
        background_tasks.add_task(
            send_company_invitation_email,
            to=email,
            inviter_name=inviter_name,
            company_name=tenant.name,
            invite_url=invite_url,
        )
        invited_count += 1

    if invited_count > 0:
        await db.commit()
        
    return {"invited": invited_count, "message": "Invitations sent successfully"}


@router.get("/invitation-codes")
async def list_invitation_codes(
    page: int = 1,
    page_size: int = 20,
    search: str = "",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List invitation codes for the current user's company."""
    _require_tenant_admin(current_user)
    from sqlalchemy import func as sqla_func

    base_filter = InvitationCode.tenant_id == current_user.tenant_id
    stmt = select(InvitationCode).where(base_filter)
    count_stmt = select(sqla_func.count()).select_from(InvitationCode).where(base_filter)

    if search:
        stmt = stmt.where(InvitationCode.code.ilike(f"%{search}%"))
        count_stmt = count_stmt.where(InvitationCode.code.ilike(f"%{search}%"))

    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    offset = (max(page, 1) - 1) * page_size
    result = await db.execute(
        stmt.order_by(InvitationCode.created_at.desc()).offset(offset).limit(page_size)
    )
    codes = result.scalars().all()
    return {
        "items": [
            {
                "id": str(c.id),
                "code": c.code,
                "max_uses": c.max_uses,
                "used_count": c.used_count,
                "is_active": c.is_active,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in codes
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }



@router.get("/invitation-codes/export")
async def export_invitation_codes_csv(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export invitation codes for the current user's company as CSV."""
    _require_tenant_admin(current_user)
    import csv
    import io

    from fastapi.responses import StreamingResponse

    result = await db.execute(
        select(InvitationCode)
        .where(InvitationCode.tenant_id == current_user.tenant_id)
        .order_by(InvitationCode.created_at.asc())
    )
    codes = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Code", "Max Uses", "Used Count", "Active", "Created At"])
    for c in codes:
        writer.writerow([
            c.code,
            c.max_uses,
            c.used_count,
            "Yes" if c.is_active else "No",
            c.created_at.strftime("%Y-%m-%d %H:%M:%S") if c.created_at else "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=invitation_codes.csv"},
    )


@router.delete("/invitation-codes/{code_id}")
async def deactivate_invitation_code(
    code_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate an invitation code (must belong to current user's company)."""
    _require_tenant_admin(current_user)
    import uuid as _uuid
    result = await db.execute(
        select(InvitationCode).where(
            InvitationCode.id == _uuid.UUID(code_id),
            InvitationCode.tenant_id == current_user.tenant_id,
        )
    )
    code = result.scalar_one_or_none()
    if not code:
        raise HTTPException(status_code=404, detail="Code not found")
    code.is_active = False
    await db.commit()
    return {"status": "deactivated"}

__all__ = [name for name in globals() if not name.startswith("__")]
