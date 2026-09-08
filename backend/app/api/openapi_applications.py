"""Tenant-admin lifecycle for external system applications."""
import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin
from app.database import get_db
from app.models.audit import AuditLog
from app.models.openapi_application import OpenAPIApplication
from app.models.tenant import Tenant
from app.schemas.openapi_application import ApplicationInput
from app.services.openapi_applications import audit, digest, fail, new_secret, now

router = APIRouter(prefix="/enterprise/openapi/applications", tags=["OpenAPI application administration"])


async def tenant_admin(tenant_id: uuid.UUID | None = None,
                       user=Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    # The normal tenant-switch owner chooses context, including for platform admins.
    # A query parameter may assert that context; it never grants another tenant.
    if not user.tenant_id:
        fail("tenant_required", 400)
    if tenant_id is not None and tenant_id != user.tenant_id:
        fail("tenant_access_denied", 403)
    tenant = await db.get(Tenant, user.tenant_id)
    if not tenant or not tenant.is_active:
        fail("tenant_unavailable", 403)
    return user


def projection(app):
    return {"id": str(app.id), "client_id": str(app.id), "name": app.name,
            "tenant_id": str(app.tenant_id), "enabled": app.enabled,
            "trust_user_identity": app.trust_user_identity, "scopes": app.scopes,
            "embed_origins": app.embed_origins, "redirect_origins": app.redirect_origins, "expires_at": app.expires_at,
            "revoked_at": app.revoked_at, "created_at": app.created_at,
            "rate_limit_per_minute": app.rate_limit_per_minute}


async def load(db, app_id, tenant_id):
    app = (await db.execute(select(OpenAPIApplication).where(
        OpenAPIApplication.id == app_id,
        OpenAPIApplication.tenant_id == tenant_id,
    ).with_for_update())).scalar_one_or_none()
    if not app:
        fail("application_not_found", 404)
    return app


@router.get("")
async def list_applications(user=Depends(tenant_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(OpenAPIApplication).where(
        OpenAPIApplication.tenant_id == user.tenant_id,
    ).order_by(OpenAPIApplication.created_at.desc()))).scalars()
    return [projection(app) for app in rows]


@router.post("", status_code=201)
async def create_application(body: ApplicationInput, response: Response,
                             user=Depends(tenant_admin), db: AsyncSession = Depends(get_db)):
    secret = new_secret()
    app = OpenAPIApplication(**body.model_dump(), tenant_id=user.tenant_id, secret_hash=digest(secret))
    db.add(app)
    await db.flush()
    await audit(db, "application.create", application_id=app.id, user_id=user.id)
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {**projection(app), "client_secret": secret}


@router.put("/{app_id}")
async def update_application(app_id: uuid.UUID, body: ApplicationInput,
                             user=Depends(tenant_admin), db: AsyncSession = Depends(get_db)):
    app = await load(db, app_id, user.tenant_id)
    if app.revoked_at:
        fail("application_revoked", 409)
    for key, value in body.model_dump().items():
        setattr(app, key, value)
    app.generation += 1
    await audit(db, "application.update", application_id=app.id, user_id=user.id)
    await db.commit()
    return projection(app)


@router.post("/{app_id}/rotate-secret")
async def rotate_secret(app_id: uuid.UUID, response: Response,
                        user=Depends(tenant_admin), db: AsyncSession = Depends(get_db)):
    app = await load(db, app_id, user.tenant_id)
    if app.revoked_at:
        fail("application_revoked", 409)
    secret = new_secret()
    app.secret_hash = digest(secret)
    app.generation += 1
    await audit(db, "application.rotate", application_id=app.id, user_id=user.id)
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {**projection(app), "client_secret": secret}


@router.post("/{app_id}/revoke")
async def revoke_application(app_id: uuid.UUID, user=Depends(tenant_admin), db: AsyncSession = Depends(get_db)):
    app = await load(db, app_id, user.tenant_id)
    app.enabled = False
    app.revoked_at = now()
    app.generation += 1
    await audit(db, "application.revoke", application_id=app.id, user_id=user.id)
    await db.commit()
    return projection(app)


@router.get("/{app_id}/audit")
async def application_audit(app_id: uuid.UUID, user=Depends(tenant_admin), db: AsyncSession = Depends(get_db)):
    await load(db, app_id, user.tenant_id)
    rows = (await db.execute(select(AuditLog).where(
        AuditLog.action.like("openapi.%"),
        AuditLog.details["application_id"].as_string() == str(app_id),
    ).order_by(AuditLog.created_at.desc()).limit(100))).scalars()
    return [{"id": str(row.id), "action": row.action, "created_at": row.created_at,
             "details": row.details} for row in rows]
