"""Tenant schemas, helpers, and self-service company creation route."""

import re
import secrets
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_authenticated_user
from app.database import get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.services.storage import normalize_storage_key

router = APIRouter()

# ─── Schemas ────────────────────────────────────────────

class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    target_tenant_id: uuid.UUID | None = None

class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    im_provider: str
    timezone: str = "UTC"
    country_region: str = "001"
    is_active: bool
    is_default: bool = False
    sso_enabled: bool = False
    sso_domain: str | None = None
    subdomain_prefix: str | None = None
    effective_base_url: str | None = None
    a2a_async_enabled: bool = True
    default_model_id: uuid.UUID | None = None
    logo_url: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class TenantUpdate(BaseModel):
    name: str | None = None
    im_provider: str | None = None
    timezone: str | None = None
    country_region: str | None = None
    is_active: bool | None = None
    is_default: bool | None = None
    sso_enabled: bool | None = None
    sso_domain: str | None = None
    subdomain_prefix: str | None = None
    effective_base_url: str | None = None
    a2a_async_enabled: bool | None = None


def _tenant_logo_key(tenant_id: uuid.UUID) -> str:
    return normalize_storage_key(f"_tenant_logos/{tenant_id}.png")


def _tenant_logo_url(tenant_id: uuid.UUID) -> str:
    return f"/api/tenants/{tenant_id}/logo?v={int(datetime.utcnow().timestamp())}"


async def _get_updateable_tenant(
    tenant_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> Tenant:
    if current_user.role == "org_admin":
        if not current_user.tenant_id:
            raise HTTPException(status_code=403, detail="Organization admin must belong to a company")
        if current_user.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Can only update your own company")
    elif current_user.role != "platform_admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


# ─── Helpers ────────────────────────────────────────────

def _slugify(name: str) -> str:
    """Generate a URL-friendly slug from a company name.

    Uses a layered transliteration strategy so non-Latin company names produce
    meaningful, readable slugs instead of collapsing to the generic 'company'
    placeholder:

      1. pypinyin   — CJK/Chinese characters → pinyin (e.g. '公司' → 'gongsi')
      2. anyascii   — remaining non-ASCII scripts → closest ASCII approximation
                      (Korean '안녕' → 'annyeong', Japanese 'ひらがな' → 'hiragana',
                       Arabic 'مرحبا' → 'mrhb', Cyrillic 'Привет' → 'Privet', …)
      3. NFKD norm  — accented Latin chars stripped of diacritics (é → e)

    A short random hex suffix is always appended to guarantee global uniqueness
    even when two tenants choose the same company name.
    """
    import unicodedata
    from pypinyin import lazy_pinyin
    from anyascii import anyascii

    # Step 1: Convert CJK characters to pinyin; non-CJK chars pass through unchanged.
    # lazy_pinyin with errors='default' keeps non-CJK chars as-is so they are
    # handled by the subsequent anyascii pass rather than being silently dropped.
    parts = lazy_pinyin(name, errors="default")
    text = "".join(parts)

    # Step 2: Convert remaining non-ASCII characters using anyascii.
    # anyascii is a no-op on ASCII input, so it is safe to apply to the whole
    # string after pypinyin has already processed the CJK portion.
    text = anyascii(text)

    # Step 3: Normalize any remaining accented Latin chars (é → e, ü → u, etc.)
    # and drop anything that still cannot be represented in ASCII.
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")

    # Step 4: Lowercase, collapse non-alphanumeric runs to hyphens, trim to 40 chars.
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    slug = slug.strip("-")[:40]

    if not slug:
        # Extremely unlikely after anyascii, but keep as a safety net
        # for inputs that are entirely punctuation or whitespace.
        slug = "company"

    # Add a short random hex suffix to ensure global uniqueness.
    slug = f"{slug}-{secrets.token_hex(3)}"
    return slug


async def _generate_subdomain_prefix(db: AsyncSession, base: str) -> str:
    """From a slug, generate a unique subdomain_prefix (strips random hex suffix)."""
    # Strip trailing hex suffix added by _slugify (e.g. "-a1b2c3")
    clean = re.sub(r"-[0-9a-f]{6}$", "", base)
    # Keep only lowercase letters, digits, hyphens
    prefix = re.sub(r"[^a-z0-9\-]", "", clean.lower())
    prefix = re.sub(r"-+", "-", prefix).strip("-")
    if len(prefix) < 2:
        prefix = f"co-{prefix}" if prefix else "company"
    if len(prefix) > 50:
        prefix = prefix[:50].rstrip("-")

    # Uniqueness check with counter suffix
    candidate = prefix
    counter = 1
    while True:
        result = await db.execute(
            select(Tenant).where(Tenant.subdomain_prefix == candidate)
        )
        if not result.scalar_one_or_none():
            return candidate
        candidate = f"{prefix}-{counter}"
        counter += 1


# ─── Self-Service: Create Company ───────────────────────
class SelfCreateResponse(BaseModel):
    """Response for self-create company, includes token for context switching."""
    tenant: TenantOut
    access_token: str | None = None  # Non-null when a new User record was created (multi-tenant switch)


@router.post("/self-create", response_model=SelfCreateResponse, status_code=status.HTTP_201_CREATED)
async def self_create_company(
    data: TenantCreate,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new company (self-service). The creator becomes org_admin.

    Supports both:
    - Registration flow (user has no tenant yet): assigns tenant directly
    - Switch-org flow (user already has a tenant): creates a new User record for the new tenant
    """
    # Block self-creation if locked to a specific tenant (Dedicated Link flow)
    if data.target_tenant_id is not None:
        raise HTTPException(status_code=403, detail="Company creation is not allowed via this link. Please join your assigned organization.")

    # Check if self-creation is allowed
    from app.models.system_settings import SystemSetting
    setting = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "allow_self_create_company")
    )
    s = setting.scalar_one_or_none()
    allowed = s.value.get("enabled", True) if s else True
    if not allowed and current_user.role != "platform_admin":
        raise HTTPException(status_code=403, detail="Company self-creation is currently disabled")

    slug = _slugify(data.name)
    tenant = Tenant(name=data.name, slug=slug, im_provider="web_only")
    db.add(tenant)
    await db.flush()

    # Auto-generate subdomain_prefix from slug
    if not tenant.subdomain_prefix:
        tenant.subdomain_prefix = await _generate_subdomain_prefix(db, slug)
        await db.flush()

    access_token = None

    from app.services.registration_service import registration_service

    if current_user.tenant_id is not None:
        # Multi-tenant: user already belongs to a company.
        # Create a NEW User record for the new tenant instead of overwriting.
        from app.core.security import create_access_token
        from app.models.participant import Participant

        new_user = User(
            identity_id=current_user.identity_id,
            tenant_id=tenant.id,
            display_name=current_user.display_name,
            role="org_admin",
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
    else:
        # Registration flow: user has no tenant yet, assign directly
        current_user.tenant_id = tenant.id
        current_user.role = "org_admin" if current_user.role == "member" else current_user.role
        # Inherit quota defaults from new tenant
        current_user.quota_message_limit = tenant.default_message_limit
        current_user.quota_message_period = tenant.default_message_period
        current_user.quota_max_agents = tenant.default_max_agents
        current_user.quota_agent_ttl_hours = tenant.default_agent_ttl_hours
        await db.flush()
        await registration_service.bind_org_member(db, current_user)

    try:
        from app.services.agent_seeder import seed_default_agents
        creator_id = new_user.id if access_token else current_user.id
        await seed_default_agents(tenant_id=tenant.id, creator_id=creator_id, db=db)
    except Exception as e:
        logger.warning(f"[self_create_company] Failed to seed default agents: {e}")

    await db.commit()

    return SelfCreateResponse(
        tenant=TenantOut.model_validate(tenant),
        access_token=access_token,
    )
