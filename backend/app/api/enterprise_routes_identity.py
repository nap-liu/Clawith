"""Mechanically separated enterprise API route group."""

from app.api.enterprise_api_shared import *  # noqa: F401,F403
from fastapi import Query
from sqlalchemy import text


# ─── SSO Derived State Helper ───────────────────────────

async def _sync_tenant_sso_state(db: AsyncSession, tenant_id: uuid.UUID):
    """Recompute tenant.sso_enabled based on channel-level sso_login_enabled flags.

    When any identity provider has sso_login_enabled=True, the tenant's
    sso_enabled is set to True and sso_domain is auto-assigned if empty.
    When all providers have sso_login_enabled=False, sso_enabled becomes False
    but sso_domain is preserved for potential re-enablement.

    Raises HTTPException(400) if IP mode and another tenant already owns the sso_domain.
    """
    from app.models.tenant import Tenant
    count_result = await db.execute(
        select(func.count(IdentityProvider.id)).where(
            IdentityProvider.tenant_id == tenant_id,
            IdentityProvider.sso_login_enabled == True,
            IdentityProvider.is_active == True,
        )
    )
    active_sso_count = count_result.scalar() or 0

    tenant_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = tenant_result.scalar_one_or_none()
    if not tenant:
        return

    tenant.sso_enabled = active_sso_count > 0

    # Auto-assign subdomain on first SSO enablement based on Platform rules
    if tenant.sso_enabled and not tenant.sso_domain and not getattr(tenant, 'is_default', False):
        sso_base = await resolve_base_url(db, tenant_id=str(tenant.id) if tenant else None)
        host = sso_base.split("://")[-1].split(":")[0].split("/")[0]
        is_ip = platform_service.is_ip_address(host)

        if is_ip:
            # IP mode: first clear ALL other tenants' sso_domain, then set for this tenant
            # (unique constraint - only one tenant can hold the IP domain)
            await db.execute(
                update(Tenant)
                .where(Tenant.id != tenant_id)
                .values(sso_domain=None, sso_enabled=False)
            )
            logger.info(f"[SSO] IP mode: cleared sso_domain for all other tenants, setting for tenant_id={tenant_id}")

        tenant.sso_domain = sso_base

    await db.commit()


async def _regenerate_all_sso_domains(db: AsyncSession):
    """Regenerate sso_domain for ALL tenants when public_base_url changes.

    - Domain mode: every tenant gets {slug}.{domain}, regardless of SSO status.
    - IP mode: only ONE tenant can hold the IP domain (unique constraint).
      The first SSO-enabled tenant keeps it; all others get sso_domain=None.
      If no SSO-enabled tenant exists, the first tenant in the list gets it.
    """
    base_url = await resolve_base_url(db)
    host = base_url.split("://")[-1].split(":")[0].split("/")[0]
    is_ip = platform_service.is_ip_address(host)

    # Fetch all tenants; put SSO-enabled ones first so they win the IP slot
    all_tenants_result = await db.execute(
        select(Tenant).order_by(Tenant.sso_enabled.desc(), Tenant.created_at.asc())
    )
    tenants = all_tenants_result.scalars().all()

    for i, tenant in enumerate(tenants):
        if is_ip:
            # IP mode: only one tenant can have SSO domain
            if i == 0:
                sso_base = await resolve_base_url(db, tenant_id=str(tenant.id) if tenant else None)
                tenant.sso_domain = sso_base
            else:
                tenant.sso_domain = None
        else:
            # Domain mode: each tenant gets their own subdomain (skip default tenant)
            if getattr(tenant, 'is_default', False):
                tenant.sso_domain = None
            else:
                sso_base = await resolve_base_url(db, tenant_id=str(tenant.id) if tenant else None)
                tenant.sso_domain = sso_base
        logger.info(f"[SSO regen] tenant={tenant.slug} sso_domain={tenant.sso_domain}")

    if tenants:
        await db.commit()


# ─── Identity Providers ─────────────────────────────────

@router.get("/identity-providers", response_model=list[IdentityProviderOut])
async def list_identity_providers(
    tenant_id: str | None = None,
    global_only: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List identity providers configured for the tenant."""
    # Authorization: non-platform admins can only see their own tenant's providers
    if tenant_id and not _is_global_platform_admin_user(current_user):
        if str(current_user.tenant_id) != tenant_id:
            raise HTTPException(status_code=403, detail="Cannot access other tenant's providers")

    query = select(IdentityProvider).order_by(IdentityProvider.created_at.desc())
    tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)

    if global_only:
        if not _is_global_platform_admin_user(current_user):
            raise HTTPException(status_code=403, detail="Only platform admin can access global identity providers")
        query = query.where(IdentityProvider.tenant_id.is_(None))
    elif tid:
        import uuid as _uuid
        query = query.where(IdentityProvider.tenant_id == _uuid.UUID(tid))
    elif not _is_global_platform_admin_user(current_user):
        raise HTTPException(status_code=400, detail="tenant_id is required for identity providers")

    result = await db.execute(query)
    providers = []
    for p in result.scalars().all():
        providers.append(_identity_provider_response(p))
    return providers


@router.post("/identity-providers/{provider_id}/discover-field-paths")
async def discover_identity_provider_field_paths(
    provider_id: uuid.UUID,
    capability: str,
    target_account: str | None = Query(default=None, max_length=320),
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Discover source paths and one bounded sample for administrator mapping."""
    result = await db.execute(
        select(IdentityProvider).where(IdentityProvider.id == provider_id)
    )
    provider = result.scalar_one_or_none()
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    if (
        not _is_global_platform_admin_user(current_user)
        and provider.tenant_id != current_user.tenant_id
    ):
        raise HTTPException(status_code=403, detail="Not authorized to inspect this provider")
    provider_type = provider.provider_type
    provider_cache_id = str(provider.id)
    config = dict(provider.config or {})
    await db.rollback()

    if capability == "login" and provider_type == "oauth2":
        from app.services.provider_field_discovery import discover_oidc_field_samples

        discovery = await discover_oidc_field_samples(
            config,
            provider_id=provider_cache_id,
        )
        return discovery
    if capability == "directory" and (
        provider_type == "scim"
        or config.get("directory_protocol") == "scim"
        or (config.get("capabilities") or {}).get("directory_protocol") == "scim"
    ):
        from app.services.scim_client import ScimClient

        client = ScimClient.from_provider_config(config)
        try:
            fields = list(await client.discover_user_fields(target_account))
            return {"fields": fields, "paths": [field["path"] for field in fields]}
        finally:
            await client.close()
    raise HTTPException(status_code=400, detail="Field discovery is unavailable")


class IdentityProviderCreate(BaseModel):
    provider_type: str
    name: str
    is_active: bool = True
    sso_login_enabled: bool = False
    sync_enabled: bool = False
    sync_interval_value: int | None = None
    sync_interval_unit: str | None = None
    config: dict = {}
    tenant_id: uuid.UUID | None = None


class IdentityProviderUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    sso_login_enabled: bool | None = None
    sync_enabled: bool | None = None
    sync_interval_value: int | None = None
    sync_interval_unit: str | None = None
    config: dict | None = None


class OAuth2Config(BaseModel):
    """OAuth2 provider configuration with friendly field names."""
    app_id: str | None = None          # Alias for client_id
    app_secret: str | None = None       # Alias for client_secret
    authorize_url: str | None = None    # OAuth2 authorize endpoint
    token_url: str | None = None        # OAuth2 token endpoint
    user_info_url: str | None = None    # OAuth2 user info endpoint
    scope: str | None = "openid profile email"
    scim_base_url: str | None = None
    scim_page_size: int | None = 500
    field_mapping: dict | None = None   # Custom field name mapping
    directory: dict | None = None

    def to_config_dict(self) -> dict:
        """Convert to config dict with both naming conventions for compatibility."""
        config = {}
        if self.app_id:
            config["app_id"] = self.app_id
            config["client_id"] = self.app_id
        if self.app_secret:
            config["app_secret"] = self.app_secret
            config["client_secret"] = self.app_secret
        if self.authorize_url:
            config["authorize_url"] = self.authorize_url
        if self.token_url:
            config["token_url"] = self.token_url
        if self.user_info_url:
            config["user_info_url"] = self.user_info_url
        if self.scope:
            config["scope"] = self.scope
        if self.scim_base_url:
            config["scim_base_url"] = self.scim_base_url
            config["directory_protocol"] = "scim"
        if self.scim_page_size:
            config["scim_page_size"] = self.scim_page_size
        if self.field_mapping is not None:
            config["field_mapping"] = self.field_mapping
        if self.directory is not None:
            config["directory"] = self.directory
        return config

    @classmethod
    def from_config_dict(cls, config: dict) -> "OAuth2Config":
        """Create from config dict, supporting both naming conventions."""
        return cls(
            app_id=config.get("app_id") or config.get("client_id"),
            app_secret=config.get("app_secret") or config.get("client_secret"),
            authorize_url=config.get("authorize_url"),
            token_url=config.get("token_url"),
            user_info_url=config.get("user_info_url"),
            scope=config.get("scope"),
            scim_base_url=config.get("scim_base_url"),
            scim_page_size=config.get("scim_page_size"),
            field_mapping=config.get("field_mapping"),
            directory=config.get("directory"),
        )


class IdentityProviderOAuth2Create(BaseModel):
    """Simplified OAuth2 provider creation with dedicated fields."""
    provider_type: str = "oauth2"
    name: str
    is_active: bool = True
    app_id: str
    app_secret: str
    authorize_url: str
    token_url: str
    user_info_url: str
    scope: str | None = "openid profile email"
    field_mapping: dict | None = None  # Custom field name mapping
    tenant_id: uuid.UUID | None = None


def normalize_oauth2_config(config: dict) -> dict:
    """Normalize OAuth2 config to use both naming conventions for compatibility."""
    if "app_id" in config or "app_secret" in config or "authorize_url" in config:
        # Mix of naming conventions - normalize
        normalized = {}
        if "app_id" in config:
            normalized["app_id"] = config["app_id"]
            normalized["client_id"] = config["app_id"]
        elif "client_id" in config:
            normalized["app_id"] = config["client_id"]
            normalized["client_id"] = config["client_id"]

        if "app_secret" in config:
            normalized["app_secret"] = config["app_secret"]
            normalized["client_secret"] = config["app_secret"]
        elif "client_secret" in config:
            normalized["app_secret"] = config["client_secret"]
            normalized["client_secret"] = config["client_secret"]

        # Copy URLs if present
        for key in ["authorize_url", "token_url", "user_info_url", "scope"]:
            if key in config:
                normalized[key] = config[key]

        return normalized
    return config

def validate_provider_config(provider_type: str, config: dict):
    """Validate identity provider config. Specific field checks are handled by the frontend."""
    if not isinstance(config, dict):
        raise HTTPException(status_code=422, detail="Configuration must be a JSON object")
    if provider_type in {"google", "github"}:
        client_id = config.get("client_id") or config.get("app_id")
        client_secret = config.get("client_secret") or config.get("app_secret")
        if not client_id or not client_secret:
            raise HTTPException(status_code=422, detail=f"{provider_type} requires client_id and client_secret")
    from app.services.provider_identity_policy import validate_identity_match_policy
    from app.services.org_sync_models import validate_enterprise_root_mapping
    from app.services.scim_directory import normalize_scim_user_field_mapping

    try:
        validate_identity_match_policy(config)
        validate_enterprise_root_mapping(config)
        directory = config.get("directory") or {}
        if "field_mapping" in directory:
            normalize_scim_user_field_mapping(directory.get("field_mapping"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return


def _apply_sync_policy(
    provider: IdentityProvider,
    *,
    enabled: bool | None,
    interval_value: int | None,
    interval_unit: str | None,
) -> None:
    """Apply one provider-neutral fixed-interval schedule."""
    from app.services.directory_sync_policy import next_sync_time, validate_sync_policy

    next_enabled = provider.sync_enabled if enabled is None else enabled
    next_value = provider.sync_interval_value if interval_value is None else interval_value
    next_unit = provider.sync_interval_unit if interval_unit is None else interval_unit
    try:
        validate_sync_policy(next_enabled, next_value, next_unit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    provider.sync_enabled = next_enabled
    provider.sync_interval_value = next_value
    provider.sync_interval_unit = next_unit
    provider.next_sync_at = (
        next_sync_time(datetime.now(timezone.utc), next_value, next_unit)
        if next_enabled and next_value is not None and next_unit is not None
        else None
    )


def _sanitize_identity_provider_config(provider_type: str, config: dict | None) -> dict | None:
    if config is None:
        return None
    secret_fields = {
        "app_secret",
        "appsecret",
        "client_secret",
        "secret",
        "bot_secret",
        "verify_aes_key",
        "google_admin_refresh_token",
        "google_admin_refresh_token_encrypted",
        "password",
        "private_key",
    }
    configured: list[str] = []

    def scrub(value, path: str = ""):
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                item_path = f"{path}.{key}" if path else key
                if key.lower() in secret_fields:
                    if item:
                        configured.append(item_path)
                    continue
                cleaned[key] = scrub(item, item_path)
            return cleaned
        if isinstance(value, list):
            return [scrub(item, path) for item in value]
        return value

    sanitized = scrub(config)
    sanitized["secret_fields_configured"] = sorted(configured)
    return sanitized


def _identity_provider_response(provider: IdentityProvider, sso_domain: str | None = None) -> dict:
    data = IdentityProviderOut.model_validate(provider).model_dump()
    data["config"] = _sanitize_identity_provider_config(provider.provider_type, provider.config)
    data["last_synced_at"] = (provider.config or {}).get("last_synced_at")
    if sso_domain is not None:
        data["sso_domain"] = sso_domain
    return data


async def _ensure_provider_type_available(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    provider_type: str,
) -> None:
    """Serialize provider creation and reject a duplicate type in one tenant."""
    lock_key = f"identity-provider-type:{tenant_id or 'global'}:{provider_type}"
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": lock_key},
    )
    query = select(IdentityProvider.id).where(
        IdentityProvider.provider_type == provider_type,
    )
    query = query.where(
        IdentityProvider.tenant_id == tenant_id
        if tenant_id is not None
        else IdentityProvider.tenant_id.is_(None)
    )
    if (await db.execute(query.limit(1))).scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail="This provider type is already configured for the tenant",
        )


@router.post("/identity-providers", response_model=IdentityProviderOut)
async def create_identity_provider(
    data: IdentityProviderCreate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new identity provider (Admin only)."""
    from app.services.auth_registry import auth_provider_registry

    # Validate config
    validate_provider_config(data.provider_type, data.config)

    # Validate and determine tenant_id
    tid = data.tenant_id
    is_platform_admin = _is_global_platform_admin_user(current_user)
    if is_platform_admin:
        # Platform admins can use any tenant_id (including None for global providers)
        pass
    else:
        # Non-platform admins: use request tenant_id if provided, else fall back to user's tenant
        if tid is None:
            tid = current_user.tenant_id
        elif str(tid) != str(current_user.tenant_id):
            # Validate they can only manage their own tenant
            raise HTTPException(status_code=403, detail="Can only create providers for your own tenant")

    if not tid and not (is_platform_admin and data.provider_type in {"google", "github"}):
        raise HTTPException(status_code=400, detail="tenant_id is required to create an identity provider")

    await _ensure_provider_type_available(
        db,
        tenant_id=tid,
        provider_type=data.provider_type,
    )

    if data.sso_login_enabled:
        if not await sso_service.validate_sso_enablement(db, tid):
             raise HTTPException(
                status_code=400,
                detail="IP address does not support multi-tenant SSO. Another tenant already has SSO enabled."
            )

    provider = IdentityProvider(
        provider_type=data.provider_type,
        name=data.name,
        is_active=data.is_active,
        sso_login_enabled=data.sso_login_enabled,
        sso_enabled_at=datetime.now(timezone.utc) if data.sso_login_enabled else None,
        config=data.config,
        tenant_id=tid
    )
    _apply_sync_policy(
        provider,
        enabled=data.sync_enabled,
        interval_value=data.sync_interval_value,
        interval_unit=data.sync_interval_unit,
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    auth_provider_registry._clear_cache(provider.provider_type)
    return _identity_provider_response(provider)


@router.post("/identity-providers/oauth2", response_model=IdentityProviderOut)
async def create_oauth2_provider(
    data: OAuth2ProviderCreate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create OAuth2 provider - strict config format only."""
    # Validate and determine tenant_id: platform admins may target any tenant
    # (or global via None); others are restricted to their own tenant.
    tid = data.tenant_id
    if not _is_global_platform_admin_user(current_user):
        if tid is None:
            tid = current_user.tenant_id
        elif str(tid) != str(current_user.tenant_id):
            raise HTTPException(status_code=403, detail="Can only create providers for your own tenant")
    if not tid:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    await _ensure_provider_type_available(
        db,
        tenant_id=tid,
        provider_type="oauth2",
    )

    # Build config dict from validated model
    config_dict = data.config.model_dump(mode='json', exclude_unset=True)
    if not config_dict.get("app_secret"):
        raise HTTPException(status_code=422, detail="OAuth2 client secret is required")
    if config_dict.get("scim_base_url"):
        config_dict["directory_protocol"] = "scim"
        config_dict["capabilities"] = {
            "login_protocol": "oauth2",
            "directory_protocol": "scim",
            "channel_protocols": [],
        }
    validate_provider_config("oauth2", config_dict)

    # Handle field_mapping: None means no mapping, empty dict also means None
    if config_dict.get('field_mapping') == {}:
        config_dict['field_mapping'] = None

    provider = IdentityProvider(
        provider_type="oauth2",
        name=data.name,
        is_active=data.is_active,
        sso_login_enabled=data.sso_login_enabled,
        sso_enabled_at=datetime.now(timezone.utc) if data.sso_login_enabled else None,
        config=config_dict,
        tenant_id=tid if isinstance(tid, uuid.UUID) else uuid.UUID(tid) if tid else None,
    )
    _apply_sync_policy(
        provider,
        enabled=data.sync_enabled,
        interval_value=data.sync_interval_value,
        interval_unit=data.sync_interval_unit,
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)

    # Sync tenant SSO state
    if provider.tenant_id:
        await _sync_tenant_sso_state(db, provider.tenant_id)

    return _identity_provider_response(provider)


@router.patch("/identity-providers/{provider_id}/oauth2", response_model=IdentityProviderOut)
async def update_oauth2_provider(
    provider_id: uuid.UUID,
    data: OAuth2ProviderUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update OAuth2 provider - config format only, no backward compatibility."""
    result = await db.execute(select(IdentityProvider).where(IdentityProvider.id == provider_id))
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if provider.provider_type != "oauth2":
        raise HTTPException(status_code=400, detail="Provider is not an OAuth2 provider")

    if not _is_global_platform_admin_user(current_user) and provider.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorized to update this provider")

    # Update basic fields
    if data.name is not None:
        provider.name = data.name
    if data.is_active is not None:
        provider.is_active = data.is_active
    if data.sso_login_enabled is not None:
        if data.sso_login_enabled and not provider.sso_login_enabled:
            provider.sso_enabled_at = datetime.now(timezone.utc)
        provider.sso_login_enabled = data.sso_login_enabled
    if any(
        value is not None
        for value in (data.sync_enabled, data.sync_interval_value, data.sync_interval_unit)
    ):
        _apply_sync_policy(
            provider,
            enabled=data.sync_enabled,
            interval_value=data.sync_interval_value,
            interval_unit=data.sync_interval_unit,
        )

    # Update config if provided
    if data.config is not None:
        config_dict = data.config.model_dump(mode='json', exclude_unset=True)
        if config_dict.get("scim_base_url"):
            config_dict["directory_protocol"] = "scim"
            config_dict["capabilities"] = {
                "login_protocol": "oauth2",
                "directory_protocol": "scim",
                "channel_protocols": [],
            }
        current_config = provider.config.copy()

        # Merge config fields
        for key, value in config_dict.items():
            if key == 'field_mapping':
                # field_mapping: None = clear, {} = use defaults, {...} = custom mapping
                if value is None:
                    current_config.pop('field_mapping', None)
                elif value == {}:
                    current_config.pop('field_mapping', None)
                else:
                    current_config['field_mapping'] = value
            elif key == 'directory' and isinstance(value, dict):
                current_config['directory'] = {
                    **(current_config.get('directory') or {}),
                    **value,
                }
            elif value not in (None, ""):
                current_config[key] = value

        validate_provider_config("oauth2", current_config)
        provider.config = current_config

    await db.commit()
    await db.refresh(provider)
    from app.services.auth_registry import auth_provider_registry
    auth_provider_registry._clear_cache(provider.provider_type)
    return _identity_provider_response(provider)


@router.put("/identity-providers/{provider_id}", response_model=IdentityProviderOut)
async def update_identity_provider(
    provider_id: uuid.UUID,
    data: IdentityProviderUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update an existing identity provider."""
    from app.services.auth_registry import auth_provider_registry

    result = await db.execute(select(IdentityProvider).where(IdentityProvider.id == provider_id))
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if not _is_global_platform_admin_user(current_user) and provider.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorized to update this provider")

    if data.name is not None:
        provider.name = data.name
    if data.is_active is not None:
        provider.is_active = data.is_active
    if data.sso_login_enabled is not None:
        if data.sso_login_enabled is True and not provider.sso_login_enabled:
            # Pre-check IP restriction before writing anything
            if not await sso_service.validate_sso_enablement(db, provider.tenant_id):
                raise HTTPException(
                    status_code=400,
                    detail="IP address does not support multi-tenant SSO. Another tenant already has SSO enabled."
                )
            provider.sso_enabled_at = datetime.now(timezone.utc)
        provider.sso_login_enabled = data.sso_login_enabled
    if any(
        value is not None
        for value in (data.sync_enabled, data.sync_interval_value, data.sync_interval_unit)
    ):
        _apply_sync_policy(
            provider,
            enabled=data.sync_enabled,
            interval_value=data.sync_interval_value,
            interval_unit=data.sync_interval_unit,
        )
    if data.config is not None:
        # Merge config
        new_config = provider.config.copy()
        secret_fields = {"app_secret", "appsecret", "client_secret", "secret", "bot_secret", "verify_aes_key"}
        for key, value in data.config.items():
            if key == "secret_fields_configured":
                continue
            if key in secret_fields and value in (None, ""):
                continue
            new_config[key] = value

        # Validate merged config
        validate_provider_config(provider.provider_type, new_config)

        provider.config = new_config

    await db.commit()
    await db.refresh(provider)
    auth_provider_registry._clear_cache(provider.provider_type)

    # Recompute tenant.sso_enabled derived state whenever sso_login_enabled changes
    sso_domain = None
    if data.sso_login_enabled is not None and provider.tenant_id:
        await _sync_tenant_sso_state(db, provider.tenant_id)
        from app.models.tenant import Tenant
        tenant_result = await db.execute(select(Tenant).where(Tenant.id == provider.tenant_id))
        t = tenant_result.scalar_one_or_none()
        if t:
            sso_domain = t.sso_domain

    return _identity_provider_response(provider, sso_domain=sso_domain)


@router.delete("/identity-providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_identity_provider(
    provider_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Retire a provider while preserving directory and sync audit facts."""
    result = await db.execute(select(IdentityProvider).where(IdentityProvider.id == provider_id))
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if not _is_global_platform_admin_user(current_user) and provider.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this provider")

    provider.is_active = False
    provider.sso_login_enabled = False
    provider.sync_enabled = False
    provider.next_sync_at = None
    await db.commit()


__all__ = [name for name in globals() if not name.startswith("__")]
