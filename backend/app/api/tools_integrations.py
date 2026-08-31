"""Integration testing and category-config routes for tools."""

import uuid

from fastapi import Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.okr_feature import okr_feature_enabled
from app.core.security import get_current_user
from app.database import get_db
from app.models.tool import Tool
from app.models.user import User

from app.api.tools_models import CategoryConfigUpdate, EmailTestRequest
from app.api.tools_shared import (
    CATEGORY_CONFIG_PRIMARY_TOOL,
    _agent_visible_tool_clause,
    _decrypt_sensitive_fields,
    _encrypt_sensitive_fields,
    _load_agent_tool_assignments,
    get_sensitive_keys,
    get_tool_company_config,
    mask_sensitive_fields,
    router,
)


@router.post("/test-email")
async def test_email_connection(
    data: EmailTestRequest,
    current_user: User = Depends(get_current_user),
):
    """Test IMAP and SMTP email connections with provided config."""
    from app.services.email_service import test_connection

    try:
        result = await test_connection(data.config)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@router.get("/email-providers")
async def get_email_providers(
    current_user: User = Depends(get_current_user),
):
    """Get list of supported email provider presets with help text."""
    from app.services.email_service import EMAIL_PROVIDERS

    return {
        key: {
            "label": p["label"],
            "help_url": p.get("help_url", ""),
            "help_text": p.get("help_text", ""),
        }
        for key, p in EMAIL_PROVIDERS.items()
    }
# ─── Tool Category Sharing Config (Generic ChannelConfig) ───


@router.get("/agents/{agent_id}/category-config/{category}")
async def get_category_config(
    agent_id: uuid.UUID,
    category: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get shared configuration for a tool category.

    Returns both global_config (company-level, from Tool.config) and
    agent_config (agent-level override, from ChannelConfig) separately.
    Sensitive fields in global_config are masked for display.
    Company-level values always take precedence at runtime.
    """
    if not okr_feature_enabled() and category == "okr":
        raise HTTPException(status_code=404, detail="Tool category not found")

    from app.core.permissions import check_agent_access
    from app.models.channel_config import ChannelConfig

    agent, _ = await check_agent_access(db, current_user, agent_id)

    # ── 1. Load company-level (global) config from Tool.config ──────────────
    # Find a tool in this category that actually has config data.
    # We cannot just LIMIT 1 because most tools may have empty config.
    primary_tool_name = CATEGORY_CONFIG_PRIMARY_TOOL.get(category)
    all_cat_tools = await db.execute(
        select(Tool).where(
            Tool.category == category,
            Tool.enabled == True,
            _agent_visible_tool_clause(agent.tenant_id, await _load_agent_tool_assignments(db, agent_id)),
        ).order_by((Tool.name != primary_tool_name) if primary_tool_name else Tool.name, Tool.name)
    )
    raw_global: dict = {}
    cat_schema: dict | None = None
    for ct in all_cat_tools.scalars():
        company_config = await get_tool_company_config(db, ct, agent.tenant_id)
        if company_config:
            cat_schema = ct.config_schema
            raw_global = company_config
            break

    # Mask sensitive fields for UI display
    masked_global = mask_sensitive_fields(raw_global, cat_schema)

    # ── 2. Load agent-level config from ChannelConfig ───────────────────────
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == category,
        )
    )
    config = result.scalar_one_or_none()

    config_id = None
    is_configured = bool(raw_global) or config is not None
    raw_agent: dict = {}

    if config:
        config_id = str(config.id)
        full_agent = {
            "api_key": config.app_secret,
            **(config.extra_config or {}),
        }
        raw_agent = _decrypt_sensitive_fields(full_agent)
        # Remove None values produced by missing app_secret
        raw_agent = {k: v for k, v in raw_agent.items() if v is not None}

    # ── 3. Build effective config ───────────────────────────────────────────
    # Never return decrypted ChannelConfig credentials.  The update endpoint
    # already preserves omitted secrets, so the UI can use blank password fields.
    effective_config = {**raw_global, **raw_agent}
    sensitive_keys = get_sensitive_keys(cat_schema)
    public_agent = {key: value for key, value in raw_agent.items() if key not in sensitive_keys}
    public_effective = {
        key: value for key, value in effective_config.items() if key not in sensitive_keys
    }

    return {
        "id": config_id,
        "agent_id": str(agent_id),
        "category": category,
        "is_configured": is_configured,
        # Legacy field (backward-compat): full effective config for display
        "config": public_effective,
        # New fields for richer UI: show global and agent configs separately
        "global_config": masked_global,
        "agent_config": public_agent,
    }


@router.post("/agents/{agent_id}/category-config/{category}")
async def update_category_config(
    agent_id: uuid.UUID,
    category: str,
    data: CategoryConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update or create shared configuration for a tool category."""
    if not okr_feature_enabled() and category == "okr":
        raise HTTPException(status_code=404, detail="Tool category not found")

    from app.core.permissions import check_agent_access, is_agent_creator
    from app.models.channel_config import ChannelConfig

    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure category")

    # Encrypt sensitive fields
    encrypted_config = _encrypt_sensitive_fields(data.config)
    app_secret = encrypted_config.get("api_key") or encrypted_config.get("api_secret") or encrypted_config.get("app_secret")
    extra = {k: v for k, v in encrypted_config.items() if k not in ("api_key", "api_secret", "app_secret")}

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == category,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        if app_secret:
            existing.app_secret = app_secret
        # Merge extra config (note: extra is already encrypted)
        existing.extra_config = {**(existing.extra_config or {}), **extra}
        existing.is_configured = True
    else:
        config = ChannelConfig(
            agent_id=agent_id,
            channel_type=category,
            app_id=category,
            app_secret=app_secret,
            extra_config=extra,
            is_configured=True,
        )
        db.add(config)

    await db.commit()

    # Special logic for Atlassian: trigger sync
    if category == "atlassian":
        import asyncio

        from app.api.atlassian import _sync_atlassian_tools_for_agent
        # Need plaintext key for sync
        plaintext_key = data.config.get("api_key") or data.config.get("api_secret") or data.config.get("app_secret")
        asyncio.create_task(_sync_atlassian_tools_for_agent(agent_id, plaintext_key))

    return {"ok": True}


@router.delete("/agents/{agent_id}/category-config/{category}", status_code=204)
async def delete_category_config(
    agent_id: uuid.UUID,
    category: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove shared configuration for a tool category."""
    if not okr_feature_enabled() and category == "okr":
        raise HTTPException(status_code=404, detail="Tool category not found")

    from app.core.permissions import check_agent_access, is_agent_creator
    from app.models.channel_config import ChannelConfig

    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove config")

    await db.execute(
        delete(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == category,
        )
    )
    await db.commit()


@router.post("/agents/{agent_id}/category-config/{category}/test")
async def test_category_config(
    agent_id: uuid.UUID,
    category: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Test connectivity for a tool category."""
    if not okr_feature_enabled() and category == "okr":
        raise HTTPException(status_code=404, detail="Tool category not found")

    if category == "atlassian":
        from app.api.atlassian import test_atlassian_channel
        return await test_atlassian_channel(agent_id, current_user, db)
    elif category == "agentbay":
        from app.services.agentbay_client import test_agentbay_channel
        return await test_agentbay_channel(agent_id, current_user, db)

    return {"ok": True, "message": f"Settings for {category} saved."}
