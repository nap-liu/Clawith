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



@router.get("/llm-providers")
async def list_llm_providers(
    current_user: User = Depends(get_current_user),
):
    """List supported LLM providers and capabilities from registry."""
    return get_provider_manifest()


class LLMTestRequest(BaseModel):
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    model_id: str | None = None  # existing model ID to use stored API key
    reasoning_effort: str | None = None


def _llm_model_out(model: LLMModel) -> LLMModelOut:
    from app.services.llm.reasoning import capability_metadata

    out = LLMModelOut.model_validate(model)
    metadata = capability_metadata(
        provider=model.provider,
        model=model.model,
        base_url=model.base_url,
    )
    out.reasoning_profile = metadata["reasoning_profile"]
    out.reasoning_efforts = metadata["reasoning_efforts"]
    out.reasoning_can_disable = metadata["reasoning_can_disable"]
    return out


async def _load_llm_test_api_key(model_id: str | None, current_user: User) -> str | None:
    """Load the stored API key for llm-test using a short-lived independent session."""
    if not model_id:
        return None

    async with async_session() as session:
        result = await session.execute(select(LLMModel).where(LLMModel.id == model_id))
        existing = result.scalar_one_or_none()
        if existing is None:
            return None
        _assert_tenant_scope(current_user, existing.tenant_id)
        return get_model_api_key(existing) if existing else None


@router.post("/llm-test")
async def test_llm_model(
    data: LLMTestRequest,
    current_user: User = Depends(get_current_admin),
):
    """Test an LLM model configuration by making a simple API call."""
    import time

    # Resolve API key: use provided key, or look up from stored model
    api_key = data.api_key if data.api_key and not data.api_key.startswith('****') else None
    if not api_key and data.model_id:
        api_key = await _load_llm_test_api_key(data.model_id, current_user)
    if not api_key:
        return {"success": False, "latency_ms": 0, "error": "API Key is required"}

    start = time.time()
    try:
        client = create_llm_client(
            provider=data.provider,
            model=data.model,
            api_key=api_key,
            base_url=data.base_url or None,
        )
        # Simple test: ask model to say "ok"
        try:
            response = await client.complete(
            messages=[LLMMessage(role="user", content="Say 'ok' and nothing else.")],
            max_tokens=16,
            reasoning_effort=data.reasoning_effort,
            )
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                await close()
        latency_ms = int((time.time() - start) * 1000)
        reply = (response.content or "")[:100] if response else ""
        return {"success": True, "latency_ms": latency_ms, "reply": reply}
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        return {"success": False, "latency_ms": latency_ms, "error": str(e)[:500]}



@router.get("/llm-models", response_model=list[LLMModelOut])
async def list_llm_models(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List LLM models scoped to the selected tenant."""
    # Authorization: non-platform admins can only see their own tenant's models
    if tenant_id and not _is_platform_admin_user(current_user):
        if str(current_user.tenant_id) != tenant_id:
            raise HTTPException(status_code=403, detail="Cannot access other tenant's models")
    if not _is_platform_admin_user(current_user) and current_user.tenant_id is None:
        raise HTTPException(status_code=403, detail="Tenant scope is required")

    tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)
    query = select(LLMModel).order_by(LLMModel.created_at.desc())
    if tid:
        query = query.where(LLMModel.tenant_id == uuid.UUID(tid))
    result = await db.execute(query)
    models = []
    for m in result.scalars().all():
        out = _llm_model_out(m)
        # Mask API key: show last 4 chars
        key = get_model_api_key(m)
        out.api_key_masked = f"****{key[-4:]}" if len(key) > 4 else "****"
        models.append(out)
    return models


@router.post("/llm-models", response_model=LLMModelOut, status_code=status.HTTP_201_CREATED)
async def add_llm_model(
    data: LLMModelCreate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add a new LLM model to the tenant's pool (admin)."""
    tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)
    target_tenant_id = uuid.UUID(tid) if tid else None
    _assert_tenant_scope(current_user, target_tenant_id)
    model = LLMModel(
        provider=data.provider,
        model=data.model,
        api_key_encrypted=encrypt_data(data.api_key, settings.SECRET_KEY),
        base_url=data.base_url,
        label=data.label,
        temperature=data.temperature,
        reasoning_effort=data.reasoning_effort,
        max_tokens_per_day=data.max_tokens_per_day,
        enabled=data.enabled,
        supports_vision=data.supports_vision,
        max_output_tokens=data.max_output_tokens,
        request_timeout=data.request_timeout,
        context_window=data.context_window,
        context_usage_ratio=data.context_usage_ratio,
        keep_recent_turns=data.keep_recent_turns,
        tenant_id=target_tenant_id,
    )
    _validate_model_context_budget(model)
    db.add(model)
    await db.flush()

    # First enabled model for a tenant becomes that tenant's default.
    # Admins can later reassign via PATCH /llm-models/{id}/set-default.
    if model.tenant_id and model.enabled:
        from app.models.tenant import Tenant
        t_result = await db.execute(select(Tenant).where(Tenant.id == model.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant and tenant.default_model_id is None:
            tenant.default_model_id = model.id

    return _llm_model_out(model)


@router.post("/llm-models/{source_model_id}/clone", response_model=LLMModelOut)
async def clone_llm_model(
    source_model_id: uuid.UUID,
    data: LLMModelClone,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Idempotently clone a tenant model while retaining its stored secret."""
    from app.services.llm_model_config import (
        LLMModelConfigError,
        clone_tenant_llm_model,
    )

    source = await db.get(LLMModel, source_model_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source model not found")
    if not _is_platform_admin_user(current_user):
        if current_user.tenant_id is None or source.tenant_id != current_user.tenant_id:
            raise HTTPException(status_code=403, detail="Cannot clone another tenant's model")
    if source.tenant_id is None:
        raise HTTPException(status_code=400, detail="Source model is not tenant-scoped")

    try:
        cloned, _ = await clone_tenant_llm_model(
            db,
            source_model_id=source_model_id,
            tenant_id=source.tenant_id,
            model_key=data.model,
            label=data.label,
        )
    except LLMModelConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _llm_model_out(cloned)


@router.post("/llm-models/{model_id}/set-default", status_code=status.HTTP_204_NO_CONTENT)
async def set_default_llm_model(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Mark this model as the tenant's default for new agents."""
    result = await db.execute(select(LLMModel).where(LLMModel.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    _assert_tenant_scope(current_user, model.tenant_id)
    if not model.tenant_id:
        raise HTTPException(status_code=400, detail="Model is not tenant-scoped")
    if not model.enabled:
        raise HTTPException(status_code=400, detail="Model is disabled")

    from app.models.tenant import Tenant
    t_result = await db.execute(select(Tenant).where(Tenant.id == model.tenant_id))
    tenant = t_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Track the previous default so we can migrate agents that were
    # following it. Without this, an admin who switches the company
    # default would have to manually update every existing agent — and
    # users would never see the new default reflected in chat.
    previous_default = tenant.default_model_id
    tenant.default_model_id = model.id

    # Migrate agents whose primary_model_id matches the OLD tenant
    # default. They were "implicitly following the default" — make them
    # follow the new one. Agents whose model is something else (the user
    # explicitly picked it) are left alone.
    if previous_default and previous_default != model.id:
        from app.models.agent import Agent
        await db.execute(
            update(Agent)
            .where(Agent.tenant_id == tenant.id)
            .where(Agent.primary_model_id == previous_default)
            .values(primary_model_id=model.id)
        )
        logger.info(
            f"[set_default_llm_model] Migrated agents in tenant {tenant.id} "
            f"from {previous_default} -> {model.id}"
        )

    await db.commit()


@router.delete("/llm-models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_llm_model(
    model_id: uuid.UUID,
    force: bool = False,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove an LLM model from the pool."""
    result = await db.execute(select(LLMModel).where(LLMModel.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    _assert_tenant_scope(current_user, model.tenant_id)

    # Check if any agents reference this model
    from sqlalchemy import or_
    ref_result = await db.execute(
        select(Agent.name).where(
            or_(Agent.primary_model_id == model_id, Agent.fallback_model_id == model_id)
        )
    )
    agent_names = [row[0] for row in ref_result.all()]

    if agent_names and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"This model is used by {len(agent_names)} agent(s)",
                "agents": agent_names,
            },
        )

    # Nullify FK references in agents before deleting
    if agent_names:
        await db.execute(
            update(Agent).where(Agent.primary_model_id == model_id).values(primary_model_id=None)
        )
        await db.execute(
            update(Agent).where(Agent.fallback_model_id == model_id).values(fallback_model_id=None)
        )
    await db.delete(model)
    await db.commit()


@router.put("/llm-models/{model_id}", response_model=LLMModelOut)
async def update_llm_model(
    model_id: uuid.UUID,
    data: LLMModelUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update an existing LLM model in the pool (admin)."""
    result = await db.execute(select(LLMModel).where(LLMModel.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    _assert_tenant_scope(current_user, model.tenant_id)

    try:
        if data.provider:
            model.provider = data.provider
        if data.model:
            model.model = data.model
        if data.label is not None:
            model.label = data.label
        if hasattr(data, 'base_url') and data.base_url is not None:
            model.base_url = data.base_url
        if data.api_key and data.api_key.strip() and not data.api_key.startswith('****'):  # Skip masked values
            model.api_key_encrypted = encrypt_data(data.api_key.strip(), settings.SECRET_KEY)
        if "temperature" in data.model_fields_set:
            model.temperature = data.temperature
        if "reasoning_effort" in data.model_fields_set:
            model.reasoning_effort = data.reasoning_effort
        if data.max_tokens_per_day is not None:
            model.max_tokens_per_day = data.max_tokens_per_day
        if data.enabled is not None:
            model.enabled = data.enabled
        if hasattr(data, 'supports_vision') and data.supports_vision is not None:
            model.supports_vision = data.supports_vision
        if hasattr(data, 'max_output_tokens') and data.max_output_tokens is not None:
            model.max_output_tokens = data.max_output_tokens
        if hasattr(data, 'request_timeout') and data.request_timeout is not None:
            model.request_timeout = data.request_timeout
        if data.context_window is not None:
            model.context_window = data.context_window
        if data.context_usage_ratio is not None:
            model.context_usage_ratio = data.context_usage_ratio
        if data.keep_recent_turns is not None:
            model.keep_recent_turns = data.keep_recent_turns

        _validate_model_context_budget(model)

        await db.commit()
        await db.refresh(model)
        return _llm_model_out(model)
    except SQLAlchemyError as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update model")


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
