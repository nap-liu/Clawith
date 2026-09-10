"""Enterprise model pool configuration and connection tests."""

from app.api.enterprise_api_shared import (
    router, User, Agent, Depends, get_current_user, get_current_admin,
    get_provider_manifest, BaseModel, LLMModel, LLMModelOut, LLMModelCreate,
    LLMModelUpdate, LLMModelClone, async_session, select, update, or_,
    _assert_tenant_scope, _is_platform_admin_user, _validate_model_context_budget,
    get_model_api_key, HTTPException, create_llm_client, LLMMessage, uuid,
    AsyncSession, get_db, status, encrypt_data, settings, logger, SQLAlchemyError,
)
from app.services.llm.failure_outcome import render_message
from pydantic import Field
from app.services.llm.client_registry import resolve_api_protocol
from app.services.model_platform import model_service_platform
from app.services.model_headers import encrypt_model_headers, resolve_model_headers
from app.schemas.model_headers import ExtraHeaders
from app.services.model_capabilities import APIProtocol, ModelPurpose, model_modalities, model_purposes, supports_purpose

@router.get("/llm-providers")
async def list_llm_providers(
    current_user: User = Depends(get_current_user),
):
    """List supported LLM providers and capabilities from registry."""
    return get_provider_manifest()


class LLMTestRequest(BaseModel):
    extra_headers: ExtraHeaders | None = None
    api_protocol: APIProtocol | None = None
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    model_id: str | None = None  # existing model ID to use stored API key
    reasoning_effort: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)


def _llm_model_out(model: LLMModel, *, reveal_headers: bool = False) -> LLMModelOut:
    from app.services.llm.reasoning import capability_metadata

    out = LLMModelOut.model_validate(model)
    if reveal_headers:
        out.extra_headers = resolve_model_headers(model)
    out.service_platform = model_service_platform(model.provider, model.base_url)
    out.effective_api_protocol = resolve_api_protocol(model.provider, model.api_protocol)
    out.purposes = model_purposes(model)
    out.input_modalities = model_modalities(model)
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

    protocol = data.api_protocol
    output_limit = data.max_output_tokens
    reasoning_effort = data.reasoning_effort
    extra_headers = data.extra_headers
    if data.model_id:
        async with async_session() as session:
            stored = await session.get(LLMModel, uuid.UUID(data.model_id))
            if stored is not None:
                _assert_tenant_scope(current_user, stored.tenant_id)
                if "api_protocol" not in data.model_fields_set:
                    protocol = stored.api_protocol
                if "max_output_tokens" not in data.model_fields_set:
                    output_limit = stored.max_output_tokens
                if "reasoning_effort" not in data.model_fields_set:
                    reasoning_effort = stored.reasoning_effort
                if ("extra_headers" not in data.model_fields_set
                        and stored.extra_headers_encrypted is not None):
                    extra_headers = resolve_model_headers(stored)
    start = time.time()
    try:
        client = create_llm_client(
            provider=data.provider,
            model=data.model,
            api_key=api_key,
            base_url=data.base_url or None,
            api_protocol=protocol,
            extra_headers=extra_headers,
        )
        # Simple test: ask model to say "ok"
        try:
            response = await client.stream(
                messages=[LLMMessage(role="user", content="Say 'ok' and nothing else.")],
                max_tokens=min(1024, output_limit) if output_limit else 1024,
                reasoning_effort=reasoning_effort,
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
    purpose: ModelPurpose | None = None,
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
    if purpose:
        from app.services.model_capabilities import purpose_clause

        query = query.where(purpose_clause(purpose))
    result = await db.execute(query)
    models = []
    for m in result.scalars().all():
        out = _llm_model_out(m, reveal_headers=(
            _is_platform_admin_user(current_user) or current_user.role == "org_admin"
        ))
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
        api_protocol=data.api_protocol,
        purposes=list(dict.fromkeys(data.purposes)),
        input_modalities=list(dict.fromkeys(data.input_modalities or (["text", "image"] if data.supports_vision else ["text"]))),
        model=data.model,
        api_key_encrypted=encrypt_data(data.api_key, settings.SECRET_KEY),
        extra_headers_encrypted=encrypt_model_headers(data.extra_headers),
        base_url=data.base_url,
        label=data.label,
        temperature=data.temperature,
        reasoning_effort=data.reasoning_effort,
        max_tokens_per_day=data.max_tokens_per_day,
        enabled=data.enabled,
        supports_vision="image" in data.input_modalities if data.input_modalities is not None else data.supports_vision,
        max_output_tokens=data.max_output_tokens,
        request_timeout=data.request_timeout,
        context_window=data.context_window,
        context_usage_ratio=data.context_usage_ratio,
        keep_recent_turns=data.keep_recent_turns,
        tenant_id=target_tenant_id,
    )
    if supports_purpose(model) or supports_purpose(model, "media_understanding"):
        _validate_model_context_budget(model)
    db.add(model)
    await db.flush()

    # First enabled model for a tenant becomes that tenant's default.
    # Admins can later reassign via PATCH /llm-models/{id}/set-default.
    if model.tenant_id and model.enabled and supports_purpose(model):
        from app.models.tenant import Tenant
        t_result = await db.execute(select(Tenant).where(Tenant.id == model.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant and tenant.default_model_id is None:
            tenant.default_model_id = model.id

    return _llm_model_out(model, reveal_headers=True)


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
    return _llm_model_out(cloned, reveal_headers=True)


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
    if not supports_purpose(model):
        raise HTTPException(status_code=422, detail=render_message("modelPool.conversationRequired"))

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
    from app.api.enterprise_routes_model_defaults import clear_media_model_defaults

    await clear_media_model_defaults(db, model)
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
        if "api_protocol" in data.model_fields_set:
            model.api_protocol = data.api_protocol
        if "extra_headers" in data.model_fields_set:
            model.extra_headers_encrypted = encrypt_model_headers(data.extra_headers)
        if data.purposes is not None:
            if "conversation" not in data.purposes:
                from app.models.tenant import Tenant

                referenced = await db.scalar(select(Agent.id).where(or_(
                    Agent.primary_model_id == model.id, Agent.fallback_model_id == model.id,
                )).limit(1))
                default = await db.scalar(select(Tenant.id).where(Tenant.default_model_id == model.id).limit(1))
                if referenced or default:
                    raise HTTPException(status_code=409, detail=render_message("modelPool.modelAssigned"))
            model.purposes = list(dict.fromkeys(data.purposes))
        if data.input_modalities is not None:
            model.input_modalities = list(dict.fromkeys(data.input_modalities))
            model.supports_vision = "image" in data.input_modalities
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
        if data.supports_vision is not None and data.input_modalities is None:
            model.supports_vision = data.supports_vision
            modalities = [item for item in model_modalities(model) if item != "image"]
            model.input_modalities = modalities + (["image"] if data.supports_vision else [])
        if hasattr(data, 'max_output_tokens') and data.max_output_tokens is not None:
            model.max_output_tokens = data.max_output_tokens
        if "request_timeout" in data.model_fields_set:
            model.request_timeout = data.request_timeout
        if data.context_window is not None:
            model.context_window = data.context_window
        if data.context_usage_ratio is not None:
            model.context_usage_ratio = data.context_usage_ratio
        if data.keep_recent_turns is not None:
            model.keep_recent_turns = data.keep_recent_turns

        if supports_purpose(model) or supports_purpose(model, "media_understanding"):
            _validate_model_context_budget(model)

        await db.commit()
        await db.refresh(model)
        return _llm_model_out(model, reveal_headers=True)
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update model")




__all__ = [name for name in globals() if not name.startswith("__")]
