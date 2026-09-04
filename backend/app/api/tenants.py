"""Tenant (Company) management API.

Public endpoints for self-service company creation and joining.
Admin endpoints for platform-level company management.
"""

import re
import secrets
import uuid
import io
from datetime import datetime

from loguru import logger

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field
import sqlalchemy as sa
from sqlalchemy import func as sqla_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import get_current_user, require_role, get_authenticated_user, set_access_token_cookie
from app.database import get_db
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import User
from app.models.system_settings import SystemSetting
from app.services.storage import ensure_local_path, get_storage_backend, normalize_storage_key

router = APIRouter(prefix="/tenants", tags=["tenants"])

from app.api.tenant_self_service import (
    SelfCreateResponse,
    TenantCreate,
    TenantOut,
    TenantUpdate,
    _generate_subdomain_prefix,
    _get_updateable_tenant,
    _slugify,
    _tenant_logo_key,
    _tenant_logo_url,
    router as self_service_router,
    self_create_company,
)

for _tenant_api_symbol in (
    SelfCreateResponse,
    TenantCreate,
    TenantOut,
    TenantUpdate,
    self_create_company,
):
    _tenant_api_symbol.__module__ = __name__

router.include_router(self_service_router)


# ─── Self-Service: Join Company via Invite Code ─────────

class JoinRequest(BaseModel):
    invitation_code: str = Field(min_length=1, max_length=32)
    target_tenant_id: uuid.UUID | None = None


class JoinResponse(BaseModel):
    tenant: TenantOut
    role: str
    access_token: str | None = None  # Non-null when a new User record was created (multi-tenant switch)


@router.post("/join", response_model=JoinResponse)
async def join_company(
    data: JoinRequest,
    request: Request,
    response: Response,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Join an existing company using an invitation code.

    Supports both:
    - Registration flow (user has no tenant yet): assigns tenant directly
    - Switch-org flow (user already has a tenant): creates a new User record"""
    from app.models.invitation_code import InvitationCode
    ic_result = await db.execute(
        select(InvitationCode).where(
            InvitationCode.code == data.invitation_code,
            InvitationCode.is_active == True,
            InvitationCode.tenant_id.is_not(None),
        )
    )
    code_obj = ic_result.scalar_one_or_none()
    if not code_obj:
        raise HTTPException(status_code=400, detail="Invalid invitation code")

    # Verify matching tenant if locked (Dedicated Link flow)
    if data.target_tenant_id and str(code_obj.tenant_id) != str(data.target_tenant_id):
        raise HTTPException(status_code=403, detail="This invitation code does not belong to the required organization.")

    if code_obj.used_count >= code_obj.max_uses:
        raise HTTPException(status_code=400, detail="Invitation code has reached its usage limit")

    # Find the company
    t_result = await db.execute(select(Tenant).where(Tenant.id == code_obj.tenant_id))
    tenant = t_result.scalar_one_or_none()
    if not tenant or not tenant.is_active:
        raise HTTPException(status_code=400, detail="Company not found or is disabled")

    # Check if user already belongs to this specific tenant
    existing_membership = await db.execute(
        select(User).where(
            User.identity_id == current_user.identity_id,
            User.tenant_id == tenant.id,
        )
    )
    if existing_membership.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="You already belong to this company")

    # Check if this company has an org_admin already
    admin_check = await db.execute(
        select(sqla_func.count()).select_from(User).where(
            User.tenant_id == tenant.id,
            User.role.in_(["org_admin", "platform_admin"]),
        )
    )
    has_admin = admin_check.scalar() > 0

    # First joiner of an empty company becomes org_admin
    assigned_role = "member" if has_admin else "org_admin"

    access_token = None

    from app.services.registration_service import registration_service

    if current_user.tenant_id is not None:
        # Multi-tenant: user already belongs to a company.
        # Create a NEW User record for the new tenant.
        from app.core.security import create_access_token
        from app.models.participant import Participant

        new_user = User(
            identity_id=current_user.identity_id,
            tenant_id=tenant.id,
            display_name=current_user.display_name,
            role=assigned_role,
            registration_source="web",
            is_active=current_user.is_active,
            quota_message_limit=tenant.default_message_limit,
            quota_message_period=tenant.default_message_period,
            quota_max_agents=tenant.default_max_agents,
            quota_agent_ttl_hours=tenant.default_agent_ttl_hours,
        )
        db.add(new_user)
        await db.flush()

        # Create Participant for the new user record
        db.add(Participant(
            type="user",
            ref_id=new_user.id,
            display_name=new_user.display_name,
            avatar_url=new_user.avatar_url,
        ))
        await db.flush()
        await registration_service.bind_org_member(db, new_user)

        # Generate token scoped to the new user so frontend can switch context
        access_token = create_access_token(str(new_user.id), new_user.role)
        final_role = new_user.role
    else:
        # Registration flow: user has no tenant yet, assign directly
        current_user.tenant_id = tenant.id
        if current_user.role == "member":
            current_user.role = assigned_role
        # Inherit quota defaults from tenant
        current_user.quota_message_limit = tenant.default_message_limit
        current_user.quota_message_period = tenant.default_message_period
        current_user.quota_max_agents = tenant.default_max_agents
        current_user.quota_agent_ttl_hours = tenant.default_agent_ttl_hours
        final_role = current_user.role
        await db.flush()
        await registration_service.bind_org_member(db, current_user)

    # Increment invitation code usage
    code_obj.used_count += 1
    await db.flush()

    await db.commit()
    set_access_token_cookie(response, request, access_token)

    return JoinResponse(
        tenant=TenantOut.model_validate(tenant),
        role=final_role,
        access_token=access_token,
    )


# ─── Registration Config ───────────────────────────────

@router.get("/registration-config")
async def get_registration_config(db: AsyncSession = Depends(get_db)):
    """Public — returns whether self-creation of companies is allowed."""
    from app.models.system_settings import SystemSetting
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "allow_self_create_company")
    )
    s = result.scalar_one_or_none()
    allowed = s.value.get("enabled", True) if s else True
    return {"allow_self_create_company": allowed}


# ─── Public: Resolve Tenant by Domain ───────────────────

@router.get("/resolve-by-domain")
async def resolve_tenant_by_domain(
    domain: str,
    db: AsyncSession = Depends(get_db),
):
    """Resolve a tenant by its sso_domain or subdomain slug.

    sso_domain is stored as a full URL (for example, a tenant hostname or IP address).
    The incoming `domain` parameter is the host (without protocol).

    Lookup precedence:
    1. Exact match on tenant.sso_domain ending with the host (strips protocol)
    2. Extract the slug from a recognized legacy platform hostname and match tenant.slug
    3. Match platform global domain -> return default tenant
    """
    tenant = None

    from app.models.system_settings import SystemSetting
    setting_result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "sso_custom_domain_redirect_enabled")
    )
    setting_s = setting_result.scalar_one_or_none()
    sso_redirect_enabled = setting_s.value.get("enabled", True) if setting_s else True

    if sso_redirect_enabled:
        # 1. Match by stripping protocol from stored sso_domain
        # Normalize the configured SSO URL to a bare hostname for comparison.
        for proto in ("https://", "http://"):
            result = await db.execute(
                select(Tenant).where(Tenant.sso_domain == f"{proto}{domain}")
            )
            tenant = result.scalar_one_or_none()
            if tenant:
                break

        # 2. Try without port (e.g. domain = "1.2.3.4:3009" → try "1.2.3.4")
        if not tenant and ":" in domain:
            domain_no_port = domain.split(":")[0]
            for proto in ("https://", "http://"):
                result = await db.execute(
                    select(Tenant).where(Tenant.sso_domain.like(f"{proto}{domain_no_port}%"))
                )
                tenant = result.scalar_one_or_none()
                if tenant:
                    break

    # 3. Fallback: extract slug from subdomain pattern
    if not tenant:
        m = re.match(r"^([a-z0-9][a-z0-9\-]*[a-z0-9])\.clawith\.ai$", domain.lower())
        if m:
            slug = m.group(1)
            result = await db.execute(select(Tenant).where(Tenant.slug == slug))
            tenant = result.scalar_one_or_none()

    # 2.5 Subdomain prefix match
    # For a tenant subdomain of the configured global hostname, use its prefix.
    if not tenant:
        from urllib.parse import urlparse as _urlparse
        setting_r2 = await db.execute(
            select(SystemSetting).where(SystemSetting.key == "platform")
        )
        platform2 = setting_r2.scalar_one_or_none()
        if platform2 and platform2.value.get("public_base_url"):
            parsed2 = _urlparse(platform2.value["public_base_url"])
            global_hostname = parsed2.hostname
            # Strip port from domain for comparison
            domain_host = domain.split(":")[0] if ":" in domain else domain
            if global_hostname and domain_host.endswith(f".{global_hostname}"):
                prefix = domain_host[: -(len(global_hostname) + 1)]
                if prefix and "." not in prefix:  # only single-level prefix
                    result = await db.execute(
                        select(Tenant).where(Tenant.subdomain_prefix == prefix)
                    )
                    tenant = result.scalar_one_or_none()

    # 3. Match platform global domain -> return default tenant
    if not tenant:
        from urllib.parse import urlparse
        setting_r = await db.execute(
            select(SystemSetting).where(SystemSetting.key == "platform")
        )
        platform = setting_r.scalar_one_or_none()
        if platform and platform.value.get("public_base_url"):
            parsed = urlparse(platform.value["public_base_url"])
            global_host = parsed.hostname
            if parsed.port and parsed.port not in (80, 443):
                global_host = f"{global_host}:{parsed.port}"
            domain_host_only = domain.split(":")[0] if ":" in domain else domain
            if domain == global_host or domain == parsed.hostname or domain_host_only == parsed.hostname:
                result = await db.execute(
                    select(Tenant).where(Tenant.is_active == True, Tenant.is_default == True)
                )
                tenant = result.scalar_one_or_none()

    if not tenant or not tenant.is_active:
        raise HTTPException(status_code=404, detail="Tenant not found or not active")

    # Build effective_base_url
    platform_result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "platform")
    )
    platform_setting = platform_result.scalar_one_or_none()
    global_base_url = platform_setting.value.get("public_base_url") if platform_setting else None

    if tenant.sso_domain:
        d = tenant.sso_domain
        effective_base_url = d if d.startswith("http") else f"https://{d}"
    elif tenant.subdomain_prefix and global_base_url:
        from urllib.parse import urlparse as _up
        _p = _up(global_base_url)
        _h = f"{tenant.subdomain_prefix}.{_p.hostname}"
        if _p.port and _p.port not in (80, 443):
            _h = f"{_h}:{_p.port}"
        effective_base_url = f"{_p.scheme}://{_h}"
    elif global_base_url:
        effective_base_url = global_base_url
    else:
        effective_base_url = None

    return {
        "id": tenant.id,
        "name": tenant.name,
        "slug": tenant.slug,
        "sso_enabled": tenant.sso_enabled,
        "sso_domain": tenant.sso_domain,
        "subdomain_prefix": tenant.subdomain_prefix,
        "is_active": tenant.is_active,
        "effective_base_url": effective_base_url,
    }

# ─── Check Subdomain Prefix Availability ───────────────

@router.get("/check-prefix")
async def check_subdomain_prefix(
    prefix: str,
    exclude_tenant_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Check if a subdomain prefix is available (public)."""
    # Format validation
    if len(prefix) < 2 or not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$", prefix):
        return {"available": False, "reason": "Invalid format. Use lowercase letters, numbers, hyphens. Min 2 chars."}

    # Reserved prefixes
    reserved = {"www", "api", "admin", "app", "mail", "ftp", "dev", "staging", "test", "static", "cdn", "ns1", "ns2"}
    if prefix in reserved:
        return {"available": False, "reason": "This prefix is reserved."}

    # Uniqueness check
    query = select(Tenant).where(Tenant.subdomain_prefix == prefix)
    if exclude_tenant_id:
        query = query.where(Tenant.id != exclude_tenant_id)
    result = await db.execute(query)
    existing = result.scalar_one_or_none()
    if existing:
        return {"available": False, "reason": "This prefix is already taken."}

    return {"available": True}



# ─── Authenticated: List / Get ──────────────────────────

@router.get("/", response_model=list[TenantOut])
async def list_tenants(
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all tenants (platform_admin only)."""
    result = await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))
    return [TenantOut.model_validate(t) for t in result.scalars().all()]


@router.get("/me", response_model=TenantOut)
async def get_my_tenant(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current user's own tenant. Any authenticated member can read
    this — the wizard and the chat model switcher need default_model_id, which
    shouldn't require admin privileges.
    """
    if not current_user.tenant_id:
        raise HTTPException(status_code=404, detail="User is not in a tenant")
    result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantOut.model_validate(tenant)


@router.get("/me/token-usage")
async def get_my_tenant_token_usage(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return aggregate token and prompt-cache usage for the current company."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=404, detail="User is not in a tenant")

    row = (await db.execute(
        select(
            sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_today), 0).label("tokens_today"),
            sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_month), 0).label("tokens_month"),
            sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0).label("tokens_total"),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_read_tokens_today), 0).label("cache_today"),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_read_tokens_month), 0).label("cache_month"),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_read_tokens_total), 0).label("cache_total"),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_creation_tokens_today), 0).label("cache_creation_today"),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_creation_tokens_month), 0).label("cache_creation_month"),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_creation_tokens_total), 0).label("cache_creation_total"),
        ).where(Agent.tenant_id == current_user.tenant_id)
    )).one()

    def bucket(total: int, cache_read: int, cache_creation: int) -> dict:
        total = int(total or 0)
        cache_read = int(cache_read or 0)
        return {
            "total_tokens": total,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": int(cache_creation or 0),
            "cache_hit_rate": round(cache_read / total, 4) if total > 0 else 0,
        }

    return {
        "today": bucket(row.tokens_today, row.cache_today, row.cache_creation_today),
        "month": bucket(row.tokens_month, row.cache_month, row.cache_creation_month),
        "total": bucket(row.tokens_total, row.cache_total, row.cache_creation_total),
    }


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tenant details. Platform admins can view any; org_admins only their own."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    if current_user.role == "org_admin":
        if not current_user.tenant_id:
            raise HTTPException(status_code=403, detail="Organization admin must belong to a company")
        if current_user.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Build effective_base_url
    platform_result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "platform")
    )
    platform_setting = platform_result.scalar_one_or_none()
    global_base_url = platform_setting.value.get("public_base_url") if platform_setting else None

    if tenant.sso_domain:
        d = tenant.sso_domain
        effective_base_url = d if d.startswith("http") else f"https://{d}"
    elif tenant.subdomain_prefix and global_base_url:
        from urllib.parse import urlparse as _up2
        _p2 = _up2(global_base_url)
        _h2 = f"{tenant.subdomain_prefix}.{_p2.hostname}"
        if _p2.port and _p2.port not in (80, 443):
            _h2 = f"{_h2}:{_p2.port}"
        effective_base_url = f"{_p2.scheme}://{_h2}"
    elif global_base_url:
        effective_base_url = global_base_url
    else:
        effective_base_url = None

    out = TenantOut.model_validate(tenant).model_dump()
    out["effective_base_url"] = effective_base_url
    return out


@router.put("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: uuid.UUID,
    data: TenantUpdate,
    current_user: User = Depends(require_role("org_admin", "platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update tenant settings. Platform admins can update any; org_admins only their own."""
    if current_user.role == "org_admin":
        if not current_user.tenant_id:
            raise HTTPException(status_code=403, detail="Organization admin must belong to a company")
        if current_user.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Can only update your own company")
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    update_data = data.model_dump(exclude_unset=True)
    
    # SSO configuration is managed exclusively by the company's own org_admin
    # via the Enterprise Settings page. Platform admins should not override it here.
    if current_user.role == "platform_admin":
        update_data.pop("sso_enabled", None)
        update_data.pop("sso_domain", None)

    # effective_base_url is computed, not stored
    update_data.pop("effective_base_url", None)

    # Slug is not updatable via this API; ignore any slug field
    update_data.pop("slug", None)

    # Handle is_default: only platform_admin can set it
    if "is_default" in update_data:
        if current_user.role != "platform_admin":
            update_data.pop("is_default", None)
        elif not update_data["is_default"] and tenant.is_default:
            # Prevent disabling the current default company directly
            raise HTTPException(
                status_code=400,
                detail="Cannot disable default company directly. Set another company as default instead."
            )
        elif update_data["is_default"]:
            # Clear is_default on all other tenants first
            await db.execute(
                sa.update(Tenant).where(Tenant.id != tenant_id).values(is_default=False)
            )

    # Validate subdomain_prefix if provided
    if "subdomain_prefix" in update_data:
        prefix = update_data["subdomain_prefix"]
        if prefix:
            if not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$", prefix) or len(prefix) < 2:
                raise HTTPException(status_code=400, detail="Invalid subdomain prefix format. Use lowercase letters, numbers, hyphens. Min 2 chars.")
            reserved = {"www", "api", "admin", "app", "mail", "ftp", "dev", "staging", "test", "static", "cdn"}
            if prefix in reserved:
                raise HTTPException(status_code=400, detail="This subdomain prefix is reserved")
        update_data["subdomain_prefix"] = prefix or None

    for field, value in update_data.items():
        setattr(tenant, field, value)
    await db.flush()
    return TenantOut.model_validate(tenant)


@router.get("/{tenant_id}/logo")
async def get_tenant_logo(tenant_id: uuid.UUID):
    """Serve a tenant logo. Logos are public UI assets, addressed by UUID."""
    storage = get_storage_backend()
    key = _tenant_logo_key(tenant_id)
    if not await storage.exists(key):
        raise HTTPException(status_code=404, detail="Logo not found")
    path = await ensure_local_path(key)
    return FileResponse(path, media_type="image/png")


@router.post("/{tenant_id}/logo", response_model=TenantOut)
async def upload_tenant_logo(
    tenant_id: uuid.UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(require_role("org_admin", "platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Upload a cropped square company logo.

    The frontend crops to a 1:1 PNG before upload. The backend keeps a hard
    1 MB limit and stores the image outside git-managed source files.
    """
    tenant = await _get_updateable_tenant(tenant_id, current_user, db)
    if file.content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise HTTPException(status_code=400, detail="Logo must be a PNG, JPEG, or WebP image")

    data = await file.read()
    if len(data) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="Logo image must be 1 MB or smaller")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid image file") from exc
    if image.width != image.height:
        raise HTTPException(status_code=400, detail="Logo image must be a 1:1 square")

    output = io.BytesIO()
    image.convert("RGBA").save(output, format="PNG", optimize=True)
    png_data = output.getvalue()
    if len(png_data) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="Logo image must be 1 MB or smaller after processing")

    storage = get_storage_backend()
    await storage.write_bytes(_tenant_logo_key(tenant_id), png_data, content_type="image/png")

    config = dict(tenant.im_config or {})
    config["logo_url"] = _tenant_logo_url(tenant_id)
    tenant.im_config = config
    await db.flush()
    return TenantOut.model_validate(tenant)


@router.delete("/{tenant_id}/logo", response_model=TenantOut)
async def delete_tenant_logo(
    tenant_id: uuid.UUID,
    current_user: User = Depends(require_role("org_admin", "platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Remove a custom company logo and fall back to the generated default."""
    tenant = await _get_updateable_tenant(tenant_id, current_user, db)

    storage = get_storage_backend()
    key = _tenant_logo_key(tenant_id)
    if await storage.exists(key):
        await storage.delete(key)

    config = dict(tenant.im_config or {})
    config.pop("logo_url", None)
    tenant.im_config = config
    await db.flush()
    return TenantOut.model_validate(tenant)


@router.put("/{tenant_id}/assign-user/{user_id}")
async def assign_user_to_tenant(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "member",
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Assign a user to a tenant with a specific role."""
    # Verify tenant
    t_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    if not t_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Verify user
    u_result = await db.execute(select(User).where(User.id == user_id))
    user = u_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if role not in ("org_admin", "agent_admin", "member"):
        raise HTTPException(status_code=400, detail="Invalid role")

    user.tenant_id = tenant_id
    user.role = role
    await db.flush()
    return {"status": "ok", "user_id": str(user_id), "tenant_id": str(tenant_id), "role": role}


# ─── Authenticated: Delete Company ─────────────────────

@router.delete("/{tenant_id}")
async def delete_tenant(
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete a company and ALL its data.

    Only the org_admin of the specified tenant (or a platform_admin) may call
    this endpoint.  After deletion the caller receives a `fallback_tenant_id`
    pointing to another company the user's identity belongs to, or `None` if
    the user has no other company.

    Deletion is performed in proper FK order to avoid constraint violations:
    agent-level data → agents → OKR/org data → users → tenant.
    """
    from sqlalchemy import text

    # ── Auth check ──────────────────────────────────────────────────────────
    is_platform_admin = getattr(current_user, "role", None) == "platform_admin"
    is_own_org_admin = (
        getattr(current_user, "role", None) == "org_admin"
        and str(current_user.tenant_id) == str(tenant_id)
    )
    if not is_platform_admin and not is_own_org_admin:
        raise HTTPException(status_code=403, detail="Only the org admin of this company (or a platform admin) can delete it")

    # ── Verify tenant exists ─────────────────────────────────────────────────
    t_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = t_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tid = str(tenant_id)

    # ── Find identity_id BEFORE any deletions (for the fallback lookup later) ─
    identity_id = current_user.identity_id

    # ── Cascade deletions in safe FK order ───────────────────────────────────
    # Helper shorthand
    agent_sub = "SELECT id FROM agents WHERE tenant_id = :tid"

    # 1. Approval requests (has agent_id FK to agents — must delete before agents)
    await db.execute(text(
        f"DELETE FROM approval_requests WHERE agent_id IN ({agent_sub})"
    ), {"tid": tid})

    # 2. Notifications (has both user_id + agent_id FKs — must delete before both)
    await db.execute(text(
        f"DELETE FROM notifications WHERE agent_id IN ({agent_sub})"
    ), {"tid": tid})
    await db.execute(text(
        "DELETE FROM notifications WHERE user_id IN (SELECT id FROM users WHERE tenant_id = :tid)"
    ), {"tid": tid})

    # 3. Bi-directional agent-to-agent relationships
    await db.execute(text(
        f"DELETE FROM agent_agent_relationships "
        f"WHERE agent_id IN ({agent_sub}) OR target_agent_id IN ({agent_sub})"
    ), {"tid": tid})

    # 4. Agent-to-human relationships
    await db.execute(text(
        f"DELETE FROM agent_relationships WHERE agent_id IN ({agent_sub})"
    ), {"tid": tid})

    # 5. Task logs → tasks
    await db.execute(text(
        f"DELETE FROM task_logs "
        f"WHERE task_id IN (SELECT id FROM tasks WHERE agent_id IN ({agent_sub}))"
    ), {"tid": tid})
    await db.execute(text(
        f"DELETE FROM tasks WHERE agent_id IN ({agent_sub})"
    ), {"tid": tid})

    # 6. chat_messages has no session_id — delete directly via agent_id
    await db.execute(text(
        f"DELETE FROM chat_messages WHERE agent_id IN ({agent_sub})"
    ), {"tid": tid})
    # 6b. Chat sessions
    await db.execute(text(
        f"DELETE FROM chat_sessions WHERE agent_id IN ({agent_sub})"
    ), {"tid": tid})

    # 7. Agent triggers  (table: agent_triggers, NOT triggers)
    await db.execute(text(
        f"DELETE FROM agent_triggers WHERE agent_id IN ({agent_sub})"
    ), {"tid": tid})

    # 8. DingTalk provisioning, channel configs, permissions, credentials
    await db.execute(text(
        f"DELETE FROM dingtalk_channel_provisioning_sessions WHERE agent_id IN ({agent_sub})"
    ), {"tid": tid})
    await db.execute(text(
        f"DELETE FROM channel_configs WHERE agent_id IN ({agent_sub})"
    ), {"tid": tid})
    await db.execute(text(
        f"DELETE FROM agent_permissions WHERE agent_id IN ({agent_sub})"
    ), {"tid": tid})
    await db.execute(text(
        f"DELETE FROM agent_credentials WHERE agent_id IN ({agent_sub})"
    ), {"tid": tid})

    # 9. Agents
    await db.execute(text("DELETE FROM agents WHERE tenant_id = :tid"), {"tid": tid})

    # 10. OKR data (okr_key_results, okr_alignments, okr_progress_logs cascade from okr_objectives FK)
    await db.execute(text("DELETE FROM okr_settings WHERE tenant_id = :tid"), {"tid": tid})
    await db.execute(text("DELETE FROM work_reports WHERE tenant_id = :tid"), {"tid": tid})
    await db.execute(text("DELETE FROM okr_objectives WHERE tenant_id = :tid"), {"tid": tid})

    # 11. Org structure
    await db.execute(text("DELETE FROM org_members WHERE tenant_id = :tid"), {"tid": tid})
    await db.execute(text("DELETE FROM org_departments WHERE tenant_id = :tid"), {"tid": tid})

    # 12. Invitation codes
    await db.execute(text("DELETE FROM invitation_codes WHERE tenant_id = :tid"), {"tid": tid})

    # 12. Users of this tenant
    await db.execute(text("DELETE FROM users WHERE tenant_id = :tid"), {"tid": tid})

    # 13. Delete the tenant itself
    await db.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid})

    await db.commit()

    # ── Find fallback tenant for the caller ──────────────────────────────────
    fallback_result = await db.execute(
        select(User.tenant_id).where(
            User.identity_id == identity_id,
            User.tenant_id != tenant_id,
        ).limit(1)
    )
    fallback_row = fallback_result.first()
    fallback_tenant_id = str(fallback_row[0]) if fallback_row else None

    return {"status": "deleted", "fallback_tenant_id": fallback_tenant_id}
