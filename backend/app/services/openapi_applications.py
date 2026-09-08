"""System credentials, delegated identity and durable integration audit."""
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.permissions import is_platform_admin_user

from app.models.audit import AuditLog
from app.models.openapi_application import OpenAPIApplication, OpenAPICredential, OpenAPIUserBinding
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.openapi_application import DelegatedUser
from app.services.authentication_state import require_active_authentication_principal
from app.services.canonical_user_resolver import CanonicalIdentityConflict, CanonicalUserResolver


def now():
    return datetime.now(timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def fail(code: str, status: int = 403):
    raise HTTPException(status_code=status, detail={"code": code, "message": code})


def application_active(app: OpenAPIApplication | None) -> bool:
    return bool(app and app.enabled and not app.revoked_at
                and (not app.expires_at or app.expires_at > now()))


def new_secret() -> str:
    return "oas_" + secrets.token_urlsafe(40)


async def audit(db: AsyncSession, action: str, *, application_id=None, user_id=None,
                employee_id=None, request_id=None, outcome="success", operation=None):
    db.add(AuditLog(user_id=user_id, agent_id=employee_id, action=f"openapi.{action}",
                    details={"application_id": str(application_id) if application_id else None,
                             "request_id": request_id, "outcome": outcome, "operation": operation}))
    await db.flush()


async def issue_system_token(db: AsyncSession, app: OpenAPIApplication, scopes):
    await db.execute(delete(OpenAPICredential).where(
        OpenAPICredential.expires_at < now() - timedelta(days=1)))
    token = "oat_" + secrets.token_urlsafe(40)
    db.add(OpenAPICredential(token_hash=digest(token), application_id=app.id,
                            generation=app.generation, kind="system", scopes=scopes,
                            expires_at=now() + timedelta(seconds=300)))
    await db.flush()
    return token


async def credential(db: AsyncSession, token: str, kind: str, *, lock=False):
    if len(token) > 4096:
        fail("invalid_token", 401)
    stmt = select(OpenAPICredential).where(OpenAPICredential.token_hash == digest(token))
    if lock:
        stmt = stmt.with_for_update()
    value = (await db.execute(stmt)).scalar_one_or_none()
    if not value or value.kind != kind or value.consumed_at or value.expires_at <= now():
        fail("invalid_token", 401)
    app = await db.get(OpenAPIApplication, value.application_id)
    if not application_active(app) or value.generation != app.generation:
        fail("application_disabled", 401)
    tenant = await db.get(Tenant, app.tenant_id)
    if not tenant or not tenant.is_active:
        fail("application_disabled", 401)
    if kind != "system" and (not app.trust_user_identity or "auth:login" not in app.scopes):
        fail("identity_delegation_disabled", 403)
    if kind != "system" and value.embed_origin and value.embed_origin not in app.embed_origins:
        fail("embed_origin_denied", 403)
    return app, value


async def authenticate_client(db: AsyncSession, client_id, secret: str):
    app = await db.get(OpenAPIApplication, client_id)
    if not application_active(app) or not hmac.compare_digest(app.secret_hash, digest(secret)):
        fail("invalid_client", 401)
    tenant = await db.get(Tenant, app.tenant_id)
    if not tenant or not tenant.is_active:
        fail("invalid_client", 401)
    return app


async def delegated_user(db: AsyncSession, app: OpenAPIApplication, claim: DelegatedUser):
    if not app.trust_user_identity:
        fail("identity_delegation_disabled")
    if abs(time.time() - claim.asserted_at) > 60:
        fail("invalid_user_assertion", 400)
    subject_hash = digest(claim.subject)
    # Serialize initial binding. A phone change can never silently retarget an
    # already bound application subject, including concurrent first requests.
    lock_key = int(digest(f"{app.id}:{subject_hash}")[:15], 16)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    binding = (await db.execute(select(OpenAPIUserBinding).where(
        OpenAPIUserBinding.application_id == app.id,
        OpenAPIUserBinding.subject_hash == subject_hash,
    ))).scalar_one_or_none()
    try:
        claims = await CanonicalUserResolver().resolve_identity_claims(
            db, email=None, phone=claim.phone, enrich=False, ordered_fields=["phone"],
            phone_equivalence=True)
    except CanonicalIdentityConflict:
        fail("identity_conflict", 409)
    if not claims.identity:
        fail("user_unavailable")
    users = (await db.execute(select(User).where(
        User.identity_id == claims.identity.id, User.tenant_id == app.tenant_id,
    ).options(selectinload(User.identity)).limit(2))).scalars().all()
    if len(users) != 1:
        fail("user_unavailable")
    user = users[0]
    if is_platform_admin_user(user):
        fail("identity_delegation_denied")
    if binding and binding.user_id != user.id:
        fail("identity_conflict", 409)
    try:
        await require_active_authentication_principal(db, user)
    except HTTPException:
        fail("user_unavailable")
    if not binding:
        db.add(OpenAPIUserBinding(application_id=app.id, subject_hash=subject_hash, user_id=user.id))
        await db.flush()
    return user


async def login_user(db: AsyncSession, app: OpenAPIApplication, value: OpenAPICredential):
    user = (await db.execute(select(User).where(User.id == value.user_id)
                            .options(selectinload(User.identity)))).scalar_one_or_none()
    if not user or user.tenant_id != app.tenant_id or is_platform_admin_user(user):
        fail("user_unavailable", 401)
    try:
        return await require_active_authentication_principal(db, user, status_code=401)
    except HTTPException:
        fail("user_unavailable", 401)
