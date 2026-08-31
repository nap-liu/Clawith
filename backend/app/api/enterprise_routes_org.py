"""Mechanically separated enterprise API route group."""

from app.api.enterprise_api_shared import *  # noqa: F401,F403


# ─── Org Structure ──────────────────────────────────────

from app.models.org import OrgDepartment, OrgMember


@router.get("/org/departments")
async def list_org_departments(
    tenant_id: str | None = None,
    provider_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all departments, optionally filtered by tenant or provider."""
    # Tenant isolation rules:
    # 1. If tenant_id param is explicitly provided:
    #    - non-platform-admins: must match their own tenant_id
    #    - platform_admin with a tenant in token: must match that tenant
    #    - platform_admin without a tenant (global view): any tenant allowed
    # 2. If tenant_id param is NOT provided:
    #    - auto-scope to current_user.tenant_id when it is set (applies to ALL roles)
    #    - only a platform_admin with NO tenant_id in token can query unrestricted
    effective_tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    is_global_admin = (current_user.role == "platform_admin" and not effective_tenant_id)

    if tenant_id:
        # Validate requested tenant against user context
        if not is_global_admin and effective_tenant_id and effective_tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Cannot access other tenant's data")
    else:
        # Auto-scope: use the user's own tenant when available
        tenant_id = effective_tenant_id  # None only for true global admin

    query = select(OrgDepartment, IdentityProvider.name.label("provider_name"), IdentityProvider.provider_type).outerjoin(
        IdentityProvider, OrgDepartment.provider_id == IdentityProvider.id
    ).where(OrgDepartment.status == "active")
    if tenant_id:
        query = query.where(OrgDepartment.tenant_id == uuid.UUID(tenant_id))
    if provider_id:
        query = query.where(OrgDepartment.provider_id == uuid.UUID(provider_id))
    result = await db.execute(query.order_by(OrgDepartment.name))
    rows = result.all()
    # Calculate total members for this scope (for the "All" entry in frontend)
    total_q = select(func.count(OrgMember.id)).where(OrgMember.status == "active")
    if tenant_id:
        total_q = total_q.where(OrgMember.tenant_id == uuid.UUID(tenant_id))
    if provider_id:
        total_q = total_q.where(OrgMember.provider_id == uuid.UUID(provider_id))
    total_result = await db.execute(total_q)
    total_member = total_result.scalar() or 0

    return {
        "items": [
            {
                "id": str(d.id),
                "external_id": d.external_id,
                "provider_id": str(d.provider_id) if d.provider_id else None,
                "provider_name": provider_name if d.provider_id else None,
                "provider_type": provider_type if d.provider_id else None,
                "name": d.name,
                "parent_id": str(d.parent_id) if d.parent_id else None,
                "path": d.path,
                "member_count": d.member_count,
            }
            for d, provider_name, provider_type in rows
        ],
        "total_member": total_member,
    }



@router.get("/org/members")
async def list_org_members(
    department_id: str | None = None,
    search: str | None = None,
    tenant_id: str | None = None,
    provider_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List org members, optionally filtered by department, search, tenant, or provider."""
    # Tenant isolation rules:
    # 1. If tenant_id param is explicitly provided:
    #    - non-platform-admins: must match their own tenant_id
    #    - platform_admin with a tenant in token: must match that tenant
    #    - platform_admin without a tenant (global view): any tenant allowed
    # 2. If tenant_id param is NOT provided:
    #    - auto-scope to current_user.tenant_id when it is set (applies to ALL roles)
    #    - only a platform_admin with NO tenant_id in token can query unrestricted
    effective_tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    is_global_admin = (current_user.role == "platform_admin" and not effective_tenant_id)

    if tenant_id:
        # Validate requested tenant against user context
        if not is_global_admin and effective_tenant_id and effective_tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Cannot access other tenant's data")
    else:
        # Auto-scope: use the user's own tenant when available
        tenant_id = effective_tenant_id  # None only for true global admin

    tenant_uuid = uuid.UUID(tenant_id) if tenant_id else None
    provider_uuid = uuid.UUID(provider_id) if provider_id else None

    canonical = canonical_org_member_id_subquery(
        tenant_id=tenant_uuid,
        provider_id=provider_uuid,
    )
    query = (
        select(
            OrgMember,
            IdentityProvider.name.label("provider_name"),
            IdentityProvider.provider_type,
            User.display_name.label("user_display_name"),
        )
        .join(canonical, and_(OrgMember.id == canonical.c.om_id, canonical.c.rn == 1))
        .outerjoin(IdentityProvider, OrgMember.provider_id == IdentityProvider.id)
        .outerjoin(User, OrgMember.user_id == User.id)
        .where(OrgMember.status == "active")
    )
    if tenant_uuid:
        query = query.where(OrgMember.tenant_id == tenant_uuid)
    if department_id:
        # Get the department to find its path and then include all sub-departments
        dept_result = await db.execute(select(OrgDepartment).where(OrgDepartment.id == uuid.UUID(department_id)))
        target_dept = dept_result.scalar_one_or_none()
        if target_dept:
            # Build sub-department query: the selected dept itself, plus any dept whose path
            # starts with its path followed by a "/" (i.e., all descendants).
            sub_dept_conditions = [OrgDepartment.id == target_dept.id]
            if target_dept.path:
                # Use SQL LIKE to find all descendants based on path prefix
                sub_dept_conditions.append(OrgDepartment.path.like(f"{target_dept.path}/%"))
            sub_depts_query = select(OrgDepartment.id).where(or_(*sub_dept_conditions))
            sub_dept_ids_result = await db.execute(sub_depts_query)
            sub_dept_ids = [row[0] for row in sub_dept_ids_result.all()]
            query = query.where(OrgMember.department_id.in_(sub_dept_ids))
        else:
            # Fallback: exact match
            query = query.where(OrgMember.department_id == uuid.UUID(department_id))
    if provider_uuid:
        query = query.where(OrgMember.provider_id == provider_uuid)
    if search:
        query = query.where(
            or_(
                OrgMember.name.ilike(f"%{search}%"),
                OrgMember.nickname.ilike(f"%{search}%"),
                OrgMember.name_translit_full.ilike(f"%{search}%"),
                OrgMember.name_translit_initial.ilike(f"%{search}%"),
            )
        )
    query = query.order_by(OrgMember.name).limit(100)
    result = await db.execute(query)
    rows = result.all()
    member_paths = await derive_member_department_paths(
        db,
        [m for m, _provider_name, _provider_type, _user_display_name in rows],
    )
    return [
        {
            "id": str(m.id),
            "name": m.name,
            "nickname": m.nickname,
            "email": m.email,
            "phone": m.phone,
            "title": m.title,
            "department_path": member_paths.get(m.id, m.department_path),
            "avatar_url": m.avatar_url,
            "external_id": m.external_id,
            "provider_id": str(m.provider_id) if m.provider_id else None,
            "provider_name": provider_name if m.provider_id else None,
            "provider_type": provider_type if m.provider_id else None,
            "user_display_name": user_display_name,
        }
        for m, provider_name, provider_type, user_display_name in rows
    ]


@router.post("/org/sync")
async def trigger_org_sync(
    provider_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger org structure sync from a specific identity provider."""
    from app.services.org_sync_service import org_sync_service

    if not provider_id:
        raise HTTPException(status_code=400, detail="provider_id is required")

    try:
        pid = uuid.UUID(provider_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid provider_id")

    result = await db.execute(select(IdentityProvider).where(IdentityProvider.id == pid))
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if not provider.tenant_id:
        raise HTTPException(status_code=400, detail="Provider must be bound to a tenant")

    if not _is_platform_admin_user(current_user) and provider.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot sync other tenant's provider")

    return await org_sync_service.sync_provider(db, provider_id)


@router.get("/org/wecom-verify/{provider_id}")
async def wecom_org_sync_verify(
    provider_id: uuid.UUID,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Handle WeCom receive-message-server URL verification for the org sync app.

    WeCom sends a GET request with msg_signature, timestamp, nonce, echostr when
    the admin first saves the receive message server URL in the app settings.
    This endpoint decrypts and returns the echostr to complete the handshake.

    After this verification succeeds, the WeCom app's trusted IP whitelist becomes
    configurable, which is the prerequisite for using App-level credentials (AgentID +
    Secret) that have full contact read permission.

    Configure URL in WeCom: {BASE_URL}/api/enterprise/org/wecom-verify/{provider_id}

    Required provider config keys (set via the platform WeCom config page):
      - verify_token:   the Token string set in both WeCom and the platform
      - verify_aes_key: the EncodingAESKey provided by WeCom (43 chars, base64url)
    """
    from fastapi.responses import Response as _Response

    from app.api.wecom import _decrypt_msg, _verify_signature

    result = await db.execute(select(IdentityProvider).where(IdentityProvider.id == provider_id))
    provider = result.scalar_one_or_none()
    if not provider:
        return _Response(status_code=404)

    config = provider.config or {}
    token = config.get("verify_token", "")
    aes_key = config.get("verify_aes_key", "")

    if not token or not aes_key:
        logger.warning(
            f"[WeCom Verify] Provider {provider_id} is missing verify_token or verify_aes_key in config. "
            "Please configure them in the WeCom provider settings."
        )
        return _Response(status_code=400)

    # Verify signature to authenticate the request from WeCom
    expected_sig = _verify_signature(token, timestamp, nonce, echostr)
    if expected_sig != msg_signature:
        logger.warning(f"[WeCom Verify] Signature mismatch for provider {provider_id}")
        return _Response(status_code=403)

    # Decrypt echostr and return plaintext (WeCom confirms URL ownership)
    try:
        decrypted, _ = _decrypt_msg(aes_key, echostr)
        logger.info(f"[WeCom Verify] Successfully verified org sync callback for provider {provider_id}")
        return _Response(content=decrypted, media_type="text/plain")
    except Exception as e:
        logger.error(f"[WeCom Verify] Failed to decrypt echostr for provider {provider_id}: {e}")
        return _Response(status_code=500)


@router.get("/org/wecom-callback/{token}", include_in_schema=False)
async def wecom_callback_verify_universal(
    token: str,
    aes_key: str = "",
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
):
    """Universal WeCom callback URL verification endpoint (no database lookup required).

    Used to unlock the 企业可信IP configuration in the WeCom admin console.
    Unlike the provider-based endpoint, this accepts the verify_token in the URL
    path and the EncodingAESKey as a query parameter, so any tenant can use the
    publicly accessible server regardless of which server
    the WeCom provider is actually configured on.

    URL format to configure in WeCom App → 接收消息服务器URL:
      https://{public_host}/api/enterprise/org/wecom-callback/{verify_token}?aes_key={encoding_aes_key}

    WeCom will append msg_signature, timestamp, nonce, echostr to this URL automatically.
    Once WeCom verifies this URL, the app's 企业可信IP whitelist becomes configurable and
    the user can add their API server IPs to allow App-level user/get calls.
    """
    from fastapi.responses import Response as _Response

    from app.api.wecom import _decrypt_msg, _verify_signature

    if not token:
        return _Response(status_code=400, content="verify_token is required in URL path")

    if not aes_key:
        logger.warning("[WeCom Callback] Missing aes_key query param in universal callback URL")
        return _Response(status_code=400, content="aes_key query param is required")

    # Verify signature to authenticate the request as coming from WeCom servers
    expected_sig = _verify_signature(token, timestamp, nonce, echostr)
    if expected_sig != msg_signature:
        logger.warning(
            f"[WeCom Callback] Signature mismatch: token={token[:8]}... "
            f"expected={expected_sig[:16]}... got={msg_signature[:16]}..."
        )
        return _Response(status_code=403)

    # Decrypt echostr and return plaintext to complete WeCom URL verification
    try:
        decrypted, _ = _decrypt_msg(aes_key, echostr)
        logger.info(f"[WeCom Callback] Universal callback verified successfully for token={token[:8]}...")
        return _Response(content=decrypted, media_type="text/plain")
    except Exception as e:
        logger.error(f"[WeCom Callback] Failed to decrypt echostr: {e}")
        return _Response(status_code=500)


__all__ = [name for name in globals() if not name.startswith("__")]
