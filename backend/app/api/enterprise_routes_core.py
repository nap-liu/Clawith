"""Mechanically separated enterprise API route group."""

from app.api.enterprise_api_shared import *  # noqa: F401,F403


# ─── Public: Check Email Exists ────────────────────────

class CheckEmailRequest(BaseModel):
    email: str


@router.post("/check-email-exists")
async def check_email_exists(
    data: CheckEmailRequest,
    db: AsyncSession = Depends(get_db),
):
    """Public endpoint — check if an email address is already registered on this platform.

    Used by the invitation flow to decide whether to show the login or register form.
    Only returns a boolean; does not expose any user data.
    """
    from app.models.user import Identity
    result = await db.execute(
        select(Identity).where(Identity.email == data.email.strip().lower())
    )
    exists = result.scalar_one_or_none() is not None
    return {"exists": exists}



# ─── Enterprise Info ────────────────────────────────────

@router.get("/info", response_model=list[EnterpriseInfoOut])
async def list_enterprise_info(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all enterprise information entries."""
    result = await db.execute(select(EnterpriseInfo).order_by(EnterpriseInfo.info_type))
    return [EnterpriseInfoOut.model_validate(e) for e in result.scalars().all()]


@router.put("/info/{info_type}", response_model=EnterpriseInfoOut)
async def update_enterprise_info(
    info_type: str,
    data: EnterpriseInfoUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create or update enterprise information. Triggers sync to agents."""
    info = await enterprise_sync_service.update_enterprise_info(
        db, info_type, data.content, data.visible_roles, current_user.id
    )
    # Sync to all running agents
    await enterprise_sync_service.sync_to_all_agents(db)
    return EnterpriseInfoOut.model_validate(info)


# ─── Approvals ──────────────────────────────────────────

@router.get("/approvals", response_model=list[ApprovalRequestOut])
async def list_approvals(
    tenant_id: str | None = None,
    status_filter: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List approval requests scoped to a tenant."""
    query = select(ApprovalRequest)
    # Scope by tenant: only show approvals for agents belonging to this tenant
    tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)
    if tid:
        tenant_agent_ids = select(Agent.id).where(Agent.tenant_id == tid)
        query = query.where(ApprovalRequest.agent_id.in_(tenant_agent_ids))
    # Non-admins further restricted to their own agents
    if current_user.role != "platform_admin":
        query = query.where(ApprovalRequest.agent_id.in_(
            select(Agent.id).where(Agent.creator_id == current_user.id)
        ))
    if status_filter:
        query = query.where(ApprovalRequest.status == status_filter)
    query = query.order_by(ApprovalRequest.created_at.desc())

    result = await db.execute(query)
    approvals = result.scalars().all()

    # Batch-load agent names
    agent_ids_set = {a.agent_id for a in approvals}
    agent_names: dict[uuid.UUID, str] = {}
    if agent_ids_set:
        agents_r = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids_set)))
        agent_names = {row.id: row.name for row in agents_r.all()}

    out = []
    for a in approvals:
        d = ApprovalRequestOut.model_validate(a)
        d.agent_name = agent_names.get(a.agent_id)
        out.append(d)
    return out


@router.post("/approvals/{approval_id}/resolve", response_model=ApprovalRequestOut)
async def resolve_approval(
    approval_id: uuid.UUID,
    data: ApprovalAction,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve or reject a pending approval request."""
    try:
        approval = await autonomy_service.resolve_approval(
            db, approval_id, current_user, data.action
        )
        return ApprovalRequestOut.model_validate(approval)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── Audit Logs ─────────────────────────────────────────

@router.get("/audit-logs", response_model=list[AuditLogOut])
async def list_audit_logs(
    agent_id: uuid.UUID | None = None,
    tenant_id: str | None = None,
    limit: int = 50,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List audit logs scoped to a tenant (admin only)."""
    query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    # Scope by tenant: only show logs for agents belonging to this tenant
    tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)
    if tid:
        tenant_agent_ids = select(Agent.id).where(Agent.tenant_id == tid)
        query = query.where(AuditLog.agent_id.in_(tenant_agent_ids))
    if agent_id:
        query = query.where(AuditLog.agent_id == agent_id)
    result = await db.execute(query)
    return [AuditLogOut.model_validate(log) for log in result.scalars().all()]


# ─── Dashboard Stats ────────────────────────────────────

@router.get("/stats")
async def get_enterprise_stats(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get enterprise dashboard statistics, optionally scoped to a tenant."""
    # Determine which tenant to filter by
    tid = tenant_id
    if tid and isinstance(tid, str):
        tid = uuid.UUID(tid)
    elif not tid:
        tid = current_user.tenant_id

    # Base queries
    agent_q = select(func.count(Agent.id))
    user_q = select(func.count(User.id)).where(User.is_active == True)
    approval_q = select(func.count(ApprovalRequest.id))

    if tid:
        agent_q = agent_q.where(Agent.tenant_id == tid)
        user_q = user_q.where(User.tenant_id == tid)
        # For approvals, we only see requests for agents in this tenant
        approval_q = approval_q.where(ApprovalRequest.agent_id.in_(
            select(Agent.id).where(Agent.tenant_id == tid)
        ))

    total_agents = await db.execute(agent_q)
    running_agents = await db.execute(
        agent_q.where(Agent.status == "running")
    )
    total_users = await db.execute(user_q)
    pending_approvals = await db.execute(
        approval_q.where(ApprovalRequest.status == "pending")
    )

    return {
        "total_agents": total_agents.scalar() or 0,
        "running_agents": running_agents.scalar() or 0,
        "total_users": total_users.scalar() or 0,
        "pending_approvals": pending_approvals.scalar() or 0,
    }


# ─── Tenant Quota Settings ──────────────────────────────

from app.models.tenant import Tenant


class TenantQuotaUpdate(BaseModel):
    default_message_limit: int | None = None
    default_message_period: str | None = None
    default_max_agents: int | None = None
    default_agent_ttl_hours: int | None = None
    default_max_llm_calls_per_day: int | None = None
    min_heartbeat_interval_minutes: int | None = None
    default_max_triggers: int | None = None
    min_poll_interval_floor: int | None = None
    max_webhook_rate_ceiling: int | None = None


@router.get("/tenant-quotas")
async def get_tenant_quotas(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tenant quota defaults and heartbeat settings."""
    if not current_user.tenant_id:
        return {}
    result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        return {}
    return {
        "default_message_limit": tenant.default_message_limit,
        "default_message_period": tenant.default_message_period,
        "default_max_agents": tenant.default_max_agents,
        "default_agent_ttl_hours": tenant.default_agent_ttl_hours,
        "default_max_llm_calls_per_day": tenant.default_max_llm_calls_per_day,
        "min_heartbeat_interval_minutes": tenant.min_heartbeat_interval_minutes,
        "default_max_triggers": tenant.default_max_triggers,
        "min_poll_interval_floor": tenant.min_poll_interval_floor,
        "max_webhook_rate_ceiling": tenant.max_webhook_rate_ceiling,
    }


@router.patch("/tenant-quotas")
async def update_tenant_quotas(
    data: TenantQuotaUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update tenant quota defaults (admin only). Enforces heartbeat floor on existing agents."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")

    result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if data.default_message_limit is not None:
        tenant.default_message_limit = data.default_message_limit
    if data.default_message_period is not None:
        tenant.default_message_period = data.default_message_period
    if data.default_max_agents is not None:
        tenant.default_max_agents = data.default_max_agents
    if data.default_agent_ttl_hours is not None:
        tenant.default_agent_ttl_hours = data.default_agent_ttl_hours
    if data.default_max_llm_calls_per_day is not None:
        tenant.default_max_llm_calls_per_day = data.default_max_llm_calls_per_day

    # Handle heartbeat floor — enforce on existing agents
    adjusted_count = 0
    if data.min_heartbeat_interval_minutes is not None:
        tenant.min_heartbeat_interval_minutes = data.min_heartbeat_interval_minutes
        from app.services.quota_guard import enforce_heartbeat_floor
        adjusted_count = await enforce_heartbeat_floor(
            tenant.id, floor=data.min_heartbeat_interval_minutes, db=db
        )

    # Handle trigger limit fields
    if data.default_max_triggers is not None:
        tenant.default_max_triggers = data.default_max_triggers
    if data.min_poll_interval_floor is not None:
        tenant.min_poll_interval_floor = data.min_poll_interval_floor
    if data.max_webhook_rate_ceiling is not None:
        tenant.max_webhook_rate_ceiling = data.max_webhook_rate_ceiling

    await db.commit()
    return {
        "message": "Tenant quotas updated",
        "heartbeat_agents_adjusted": adjusted_count,
    }


# ── System Email: Test & Templates ──────────────────────


class TestEmailRequest(BaseModel):
    email: str


@router.post("/system-email/test")
async def send_test_email_endpoint(
    data: TestEmailRequest,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Send a test email to verify SMTP configuration (admin only)."""
    import smtplib
    import socket
    import ssl

    from app.services.system_email_service import send_test_email

    try:
        await send_test_email(data.email, db=db)
        return {"success": True, "message": f"Test email sent to {data.email}"}
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(
            status_code=400,
            detail=(
                "SMTP authentication failed. Please check that the SMTP username is the full email address "
                "and that the password/app password is valid for this mailbox."
            ),
        )
    except (TimeoutError, socket.timeout, ssl.SSLError) as e:
        raise HTTPException(
            status_code=400,
            detail=(
                f"SMTP TLS/connect timed out: {e}. Please verify the SMTP host, port, and SSL/TLS mode. "
                "For Zoho, the SMTP host depends on the account data center, for example smtp.zoho.com "
                "or smtp.zoho.com.cn."
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/email-templates")
async def get_email_templates_endpoint(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get email templates (current values + available variables per scenario)."""
    from app.services.system_email_service import (
        DEFAULT_EMAIL_TEMPLATES,
        EMAIL_TEMPLATE_VARIABLES,
        get_email_templates,
    )

    templates = await get_email_templates(db=db)
    return {
        "templates": templates,
        "variables": EMAIL_TEMPLATE_VARIABLES,
        "defaults": DEFAULT_EMAIL_TEMPLATES,
    }


class EmailTemplatesUpdate(BaseModel):
    templates: dict


@router.put("/email-templates")
async def update_email_templates_endpoint(
    data: EmailTemplatesUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Save email templates (admin only)."""
    from app.services.system_email_service import EMAIL_TEMPLATE_VARIABLES

    # Validate that only known scenario keys are provided
    for key in data.templates:
        if key not in EMAIL_TEMPLATE_VARIABLES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown email template scenario: {key}"
            )

    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "email_templates")
    )
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = data.templates
    else:
        setting = SystemSetting(key="email_templates", value=data.templates)
        db.add(setting)
    await db.commit()
    return {"success": True, "message": "Email templates saved"}


# ─── System Settings ───────────────────────────────────

from app.models.system_settings import SystemSetting


class SettingUpdate(BaseModel):
    value: dict


@router.get("/system-settings/notification_bar/public")
async def get_notification_bar_public(
    db: AsyncSession = Depends(get_db),
):
    """Public (no auth) endpoint to read the notification bar config."""
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "notification_bar")
    )
    setting = result.scalar_one_or_none()
    if not setting or not setting.value:
        return {"enabled": False, "text": "", "updated_at": None}
    return {
        "enabled": setting.value.get("enabled", False),
        "text": setting.value.get("text", ""),
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    }


@router.get("/system-settings/{key}")
async def get_system_setting(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a system setting by key."""
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = result.scalar_one_or_none()
    if not setting:
        return {"key": key, "value": {}}
    return {"key": setting.key, "value": setting.value, "updated_at": setting.updated_at.isoformat() if setting.updated_at else None}


@router.put("/system-settings/{key}")
async def update_system_setting(
    key: str,
    data: SettingUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create or update a system setting."""
    # Platform-level settings (e.g. PUBLIC_BASE_URL) require platform_admin
    if key == "platform" and not _is_platform_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only platform admin can modify platform settings")
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = data.value
    else:
        setting = SystemSetting(key=key, value=data.value)
        db.add(setting)
    await db.commit()

    # When public_base_url changes, regenerate sso_domain for all SSO-enabled tenants
    if key == "platform" and data.value.get("public_base_url"):
        await _regenerate_all_sso_domains(db)

    await db.refresh(setting)
    return {
        "key": setting.key,
        "value": setting.value,
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    }


__all__ = [name for name in globals() if not name.startswith("__")]
