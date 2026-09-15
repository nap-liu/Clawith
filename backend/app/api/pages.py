"""Published-page rendering, access control, and owner management APIs."""

import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.page_admin_helpers import (
    _apply_page_access_update,
    _page_detail,
    _page_summaries,
    _validate_access_mode,
    _validated_allowed_user_ids,
)
from app.api.page_admin_queries import list_manageable_pages
from app.api.page_visitor_queries import list_visitors
from app.api.page_models import (
    PageAccessUpdate,
    PageBulkAccessUpdate,
    PageRequestResolution,
    PageSessionRequest,
)
from app.config import get_settings
from app.core.permissions import build_visible_agents_query, is_platform_admin_user
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.published_page import (
    PublishedPage,
    PublishedPageAccess,
    PublishedPageAnonymousVisitor,
    PublishedPageVisitor,
)
from app.models.user import Identity, User
from app.services.notification_service import send_notification
from app.services.org_directory import (
    permission_directory_departments,
    permission_directory_members,
)
from app.services.published_page_access import (
    PAGE_SESSION_COOKIE,
    can_manage_page,
    can_view_page,
    set_page_session_cookie,
    page_user_from_session,
)
from app.services.storage import get_storage_backend, normalize_storage_key

public_router = APIRouter(tags=["pages"])
router = APIRouter(prefix="/pages", tags=["pages"])
PUBLIC_VISITOR_COOKIE = "published_page_visitor"
PUBLIC_VISITOR_COOKIE_MAX_AGE = 365 * 24 * 3600
MAX_ANONYMOUS_VISITORS_PER_PAGE = 10_000
ANONYMOUS_VISITOR_OVERFLOW_KEY = "0" * 64
PUBLISHED_PAGE_UNAVAILABLE_PATH = "/published-page-unavailable"
logger = logging.getLogger(__name__)


def _request_scheme(request: Request) -> str:
    forwarded_scheme = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    if forwarded_scheme in {"http", "https"}:
        return forwarded_scheme
    request_scheme = request.url.scheme.lower()
    return request_scheme if request_scheme in {"http", "https"} else "http"


def _access_ui_redirect(page: PublishedPage, request: Request, denied: bool = False) -> RedirectResponse:
    return_url = str(request.url.replace(scheme=_request_scheme(request)))
    params = {
        "short_id": page.short_id,
        "return_to": return_url,
        "auto_login": "1",
    }
    if page.tenant_id:
        params["tenant_id"] = str(page.tenant_id)
    requested_sso = request.query_params.get("sso")
    if requested_sso:
        params["sso"] = requested_sso
    if denied:
        params["denied"] = "1"
    return RedirectResponse(f"/published-page-access?{urlencode(params)}", status_code=302)


def _page_storage_key(page: PublishedPage) -> str:
    return normalize_storage_key(f"{page.agent_id}/{page.source_path}")


async def _page_source_exists(page: PublishedPage) -> bool:
    storage = get_storage_backend()
    storage_key = _page_storage_key(page)
    return await storage.exists(storage_key) and await storage.is_file(storage_key)


def _anonymous_visitor(request: Request, page_id: uuid.UUID) -> tuple[str, str | None]:
    """Return a page-scoped aggregate key and a new cookie value when needed."""
    cookie_value = request.cookies.get(PUBLIC_VISITOR_COOKIE)
    try:
        visitor_id = uuid.UUID(cookie_value) if cookie_value else uuid.uuid4()
        new_cookie = None if cookie_value else str(visitor_id)
    except (TypeError, ValueError):
        visitor_id = uuid.uuid4()
        new_cookie = str(visitor_id)
    visitor_key = hmac.new(
        get_settings().SECRET_KEY.encode("utf-8"),
        f"{page_id}:{visitor_id}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return visitor_key, new_cookie


def _public_watermark_text(visitor_key: str, accessed_at: datetime) -> str:
    """Build the anonymous label rendered by the platform-owned viewer layer."""
    accessed_at_text = accessed_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"匿名访客 {visitor_key[:12].upper()} · {accessed_at_text}"


def _set_public_visitor_cookie(response: Response, value: str, request: Request) -> None:
    response.set_cookie(
        PUBLIC_VISITOR_COOKIE,
        value,
        max_age=PUBLIC_VISITOR_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_request_scheme(request) == "https",
        # The management APIs and report route share this opaque visitor ID.
        # Only the page-scoped HMAC is persisted by the backend.
        path="/",
    )


async def _record_anonymous_view(db: AsyncSession, page_id: uuid.UUID, visitor_key: str) -> None:
    """Aggregate a visitor while keeping the public endpoint's row count bounded."""
    now = datetime.now(timezone.utc)
    existing = await db.execute(
        update(PublishedPageAnonymousVisitor)
        .where(
            PublishedPageAnonymousVisitor.page_id == page_id,
            PublishedPageAnonymousVisitor.visitor_key == visitor_key,
        )
        .values(
            view_count=PublishedPageAnonymousVisitor.view_count + 1,
            last_viewed_at=now,
        )
    )
    if existing.rowcount:
        return

    # Only first-time keys take this page-scoped lock. Repeat views remain a
    # single UPDATE, while concurrent new keys cannot exceed the hard cap.
    await db.execute(select(func.pg_advisory_xact_lock(page_id.int & 0x7FFF_FFFF_FFFF_FFFF)))
    existing = await db.execute(
        update(PublishedPageAnonymousVisitor)
        .where(
            PublishedPageAnonymousVisitor.page_id == page_id,
            PublishedPageAnonymousVisitor.visitor_key == visitor_key,
        )
        .values(
            view_count=PublishedPageAnonymousVisitor.view_count + 1,
            last_viewed_at=now,
        )
    )
    if existing.rowcount:
        return

    visitor_count = await db.scalar(
        select(func.count()).select_from(PublishedPageAnonymousVisitor).where(
            PublishedPageAnonymousVisitor.page_id == page_id,
            PublishedPageAnonymousVisitor.visitor_key != ANONYMOUS_VISITOR_OVERFLOW_KEY,
        )
    )
    effective_key = (
        visitor_key
        if int(visitor_count or 0) < MAX_ANONYMOUS_VISITORS_PER_PAGE
        else ANONYMOUS_VISITOR_OVERFLOW_KEY
    )
    anonymous_stmt = insert(PublishedPageAnonymousVisitor).values(
        page_id=page_id,
        visitor_key=effective_key,
        view_count=1,
        first_viewed_at=now,
        last_viewed_at=now,
    )
    await db.execute(
        anonymous_stmt.on_conflict_do_update(
            constraint="uq_published_page_anonymous_visitor",
            set_={
                "view_count": PublishedPageAnonymousVisitor.view_count + 1,
                "last_viewed_at": now,
            },
        )
    )


async def _record_view(
    db: AsyncSession,
    page: PublishedPage,
    user: User | None,
    anonymous_visitor_key: str | None = None,
) -> None:
    await db.execute(
        update(PublishedPage).where(PublishedPage.id == page.id).values(view_count=PublishedPage.view_count + 1)
    )
    if user is not None and user.tenant_id == page.tenant_id:
        now = datetime.now(timezone.utc)
        stmt = insert(PublishedPageVisitor).values(
            page_id=page.id, user_id=user.id, view_count=1, first_viewed_at=now, last_viewed_at=now
        )
        await db.execute(
            stmt.on_conflict_do_update(
                constraint="uq_published_page_visitors_page_user",
                set_={"view_count": PublishedPageVisitor.view_count + 1, "last_viewed_at": now},
            )
        )
    elif anonymous_visitor_key:
        await _record_anonymous_view(db, page.id, anonymous_visitor_key)


async def _render_page_content(
    db: AsyncSession,
    page: PublishedPage,
    user: User | None,
    request: Request,
) -> Response:
    storage = get_storage_backend()
    storage_key = _page_storage_key(page)
    html_content = await storage.read_bytes(storage_key)
    anonymous_visitor_key = None
    new_visitor_cookie = None
    if user is None:
        anonymous_visitor_key, new_visitor_cookie = _anonymous_visitor(request, page.id)
    await _record_view(db, page, user, anonymous_visitor_key)
    await db.commit()
    content_response = Response(
        content=html_content,
        headers={
            "Cache-Control": "no-store",
            "Content-Type": "text/html",
        },
    )
    if user is not None:
        set_page_session_cookie(content_response, request, user.id)
    elif new_visitor_cookie:
        _set_public_visitor_cookie(content_response, new_visitor_cookie, request)
    return content_response


async def _resolve_page_view(
    short_id: str,
    request: Request,
    db: AsyncSession,
) -> tuple[PublishedPage, User | None, int | None]:
    page = await db.scalar(select(PublishedPage).where(PublishedPage.short_id == short_id))
    if not page or not await _page_source_exists(page):
        raise HTTPException(status_code=404, detail="Published page not found")

    user = (
        None
        if page.access_mode == "public"
        else await page_user_from_session(db, request.cookies.get(PAGE_SESSION_COOKIE))
    )
    if page.access_mode != "public" and user is None:
        return page, None, 401
    if not await can_view_page(db, page, user):
        return page, user, 403
    return page, user, None


@public_router.get("/p/{short_id}")
async def render_page(short_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        page, user, access_error = await _resolve_page_view(short_id, request, db)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return RedirectResponse(PUBLISHED_PAGE_UNAVAILABLE_PATH, status_code=302)
    if access_error == 401:
        return _access_ui_redirect(page, request)
    if access_error == 403:
        return _access_ui_redirect(page, request, denied=True)

    # The platform viewer owns watermark rendering. The report itself is still
    # returned byte-for-byte in an unrestricted, same-origin iframe: there is
    # deliberately no sandbox, CSP, or route-owned browser capability policy.
    if request.query_params.get("__report_embed") == "1":
        return await _render_page_content(db, page, user, request)

    viewer_response = Response(headers={
        "X-Accel-Redirect": "/__published_page_viewer",
        "Cache-Control": "no-store",
    })
    if user is not None:
        set_page_session_cookie(viewer_response, request, user.id)
    else:
        _visitor_key, new_visitor_cookie = _anonymous_visitor(request, page.id)
        if new_visitor_cookie:
            _set_public_visitor_cookie(viewer_response, new_visitor_cookie, request)
    return viewer_response


@router.post("/session")
async def create_render_session(
    payload: PageSessionRequest,
    response: Response,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    page = await db.scalar(select(PublishedPage).where(PublishedPage.short_id == payload.short_id))
    if not page:
        raise HTTPException(404, "Page not found")
    if page.tenant_id != current_user.tenant_id:
        raise HTTPException(403, "无权访问此页面")
    if not await _page_source_exists(page):
        raise HTTPException(404, "Source file no longer exists")
    set_page_session_cookie(response, request, current_user.id)
    pending = bool(await db.scalar(select(PublishedPageAccess.id).where(
        PublishedPageAccess.page_id == page.id,
        PublishedPageAccess.user_id == current_user.id,
        PublishedPageAccess.status == "pending",
    )))
    return {
        "allowed": await can_view_page(db, page, current_user),
        "title": page.title or page.source_path,
        "access_mode": page.access_mode,
        "request_pending": pending,
    }


@router.delete("/session")
async def clear_render_session(response: Response):
    response.delete_cookie(PAGE_SESSION_COOKIE, path="/", samesite="lax")
    response.delete_cookie(PAGE_SESSION_COOKIE, path="/p/", samesite="lax")
    return {"ok": True}


@router.get("/{short_id}/viewer-context")
async def get_published_page_viewer_context(
    short_id: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Return the identity context used by the unrestricted platform viewer."""
    page, user, access_error = await _resolve_page_view(short_id, request, db)
    if access_error == 401:
        raise HTTPException(401, "Page session expired")
    if access_error == 403:
        raise HTTPException(403, "无权访问此页面")

    response.headers["Cache-Control"] = "no-store"
    watermark_identity = None
    watermark_text = None
    if user is None:
        visitor_key, new_visitor_cookie = _anonymous_visitor(request, page.id)
        try:
            watermark_text = _public_watermark_text(visitor_key, datetime.now(timezone.utc))
        except Exception:
            # Watermark formatting remains fail-open so the report can load.
            logger.exception("Failed to build public page watermark", extra={"page_id": str(page.id)})
        if new_visitor_cookie:
            _set_public_visitor_cookie(response, new_visitor_cookie, request)
    else:
        identity = await db.get(Identity, user.identity_id) if user.identity_id else None
        watermark_identity = {
            "display_name": user.display_name,
            "username": identity.username if identity else None,
            "primary_mobile": identity.phone if identity else None,
        }
    return {
        "title": page.title or page.source_path,
        "access_mode": page.access_mode,
        "watermark_identity": watermark_identity,
        "watermark_text": watermark_text,
        "allow_top_navigation": True,
    }


@router.get("/{short_id}/content")
async def get_published_page_content(
    short_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    page, user, access_error = await _resolve_page_view(short_id, request, db)
    if access_error == 401:
        raise HTTPException(401, "Page session expired")
    if access_error == 403:
        raise HTTPException(403, "无权访问此页面")
    return await _render_page_content(db, page, user, request)


@router.post("/{short_id}/request-access")
async def request_page_access(
    short_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    page = await db.scalar(select(PublishedPage).where(PublishedPage.short_id == short_id))
    if not page:
        raise HTTPException(404, "Page not found")
    if page.access_mode != "restricted" or page.tenant_id != current_user.tenant_id:
        raise HTTPException(403, "This page does not accept access requests")
    if await can_view_page(db, page, current_user):
        return {"status": "approved"}

    row = await db.scalar(select(PublishedPageAccess).where(
        PublishedPageAccess.page_id == page.id, PublishedPageAccess.user_id == current_user.id
    ))
    if row and row.status == "pending":
        return {"status": "pending"}
    if row:
        row.status = "pending"
        row.requested_at = datetime.now(timezone.utc)
        row.resolved_at = None
        row.resolved_by = None
    else:
        row = PublishedPageAccess(
            page_id=page.id, user_id=current_user.id, status="pending", requested_at=datetime.now(timezone.utc)
        )
        db.add(row)
    await db.flush()
    await send_notification(
        db,
        user_id=page.user_id,
        type="page_access_pending",
        title="页面访问权限申请",
        body=f"{current_user.display_name} 申请访问「{page.title or page.source_path}」",
        link=f"/published-pages?page={page.id}",
        ref_id=page.id,
        sender_name=current_user.display_name,
    )
    db.add(AuditLog(user_id=current_user.id, agent_id=page.agent_id, action="published_page_access_requested", details={"page_id": str(page.id)}))
    await db.commit()
    return {"status": "pending"}




@router.get("/agent-options")
async def list_page_agent_options(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if is_platform_admin_user(current_user) or current_user.role == "org_admin":
        query = select(Agent).where(
            Agent.tenant_id == current_user.tenant_id,
            Agent.scope == "standard",
            Agent.is_deleted.is_(False),
        )
    else:
        query = build_visible_agents_query(current_user, tenant_id=current_user.tenant_id)
    visible_agents = (await db.scalars(query.order_by(Agent.name.asc(), Agent.id.asc()))).all()
    return [{"id": str(option.id), "name": option.name} for option in visible_agents]


@router.get("/mine")
async def list_my_pages(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    agent_id: uuid.UUID | None = None,
    agent_ids: list[uuid.UUID] = Query(default=[]),
    access_mode: Literal["public", "authenticated", "restricted"] | None = None,
    q: str | None = Query(default=None, max_length=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_manageable_pages(
        db,
        current_user=current_user,
        page=page,
        page_size=page_size,
        agent_id=agent_id,
        agent_ids=agent_ids,
        access_mode=access_mode,
        search=q,
    )


@router.get("/{page_id}/detail")
async def get_page_detail(page_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    page = await db.get(PublishedPage, page_id)
    if not page or not await can_manage_page(db, page, current_user):
        raise HTTPException(404, "Page not found")
    return await _page_detail(db, page)


@router.delete("/{page_id}")
async def delete_published_page(
    page_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    page = await db.get(PublishedPage, page_id)
    if not page or not await can_manage_page(db, page, current_user):
        raise HTTPException(404, "Page not found")
    await db.delete(page)
    await db.commit()
    return {"ok": True}


@router.get("/{page_id}/visitors")
async def list_page_visitors(
    page_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    published_page = await db.get(PublishedPage, page_id)
    if not published_page or not await can_manage_page(db, published_page, current_user):
        raise HTTPException(404, "Page not found")
    return await list_visitors(
        db, page_id=page_id, page=page, page_size=page_size,
        search=q, overflow_key=ANONYMOUS_VISITOR_OVERFLOW_KEY,
    )


async def _manageable_page(db: AsyncSession, page_id: uuid.UUID, user: User) -> PublishedPage:
    published_page = await db.get(PublishedPage, page_id)
    if not published_page or not await can_manage_page(db, published_page, user):
        raise HTTPException(404, "Page not found")
    return published_page


@router.get("/{page_id}/directory/departments")
async def get_page_directory_departments(
    page_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    published_page = await _manageable_page(db, page_id, current_user)
    return await permission_directory_departments(
        db,
        tenant_id=published_page.tenant_id,
        current_user_id=current_user.id,
        parent_id=parent_id,
        search=search,
        limit=limit,
    )


@router.get("/{page_id}/directory/members")
async def get_page_directory_members(
    page_id: uuid.UUID,
    department_id: uuid.UUID | None = None,
    include_descendants: bool = False,
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    published_page = await _manageable_page(db, page_id, current_user)
    return await permission_directory_members(
        db,
        tenant_id=published_page.tenant_id,
        department_id=department_id,
        include_descendants=include_descendants,
        search=search,
        page=page,
        page_size=page_size,
    )




@router.put("/batch/access")
async def update_pages_access(
    payload: PageBulkAccessUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _validate_access_mode(payload.access_mode)
    page_ids = list(dict.fromkeys(payload.page_ids))
    pages = list((await db.scalars(
        select(PublishedPage)
        .where(PublishedPage.id.in_(page_ids))
        .order_by(PublishedPage.id)
        .with_for_update()
    )).all())
    if len(pages) != len(page_ids):
        raise HTTPException(404, "One or more pages were not found")
    for page in pages:
        if not await can_manage_page(db, page, current_user):
            raise HTTPException(404, "One or more pages were not found")
        if page.tenant_id is None:
            page.tenant_id = current_user.tenant_id
    allowed_ids = await _validated_allowed_user_ids(db, current_user.tenant_id, payload)
    for page in pages:
        await _apply_page_access_update(db, page, payload.access_mode, allowed_ids, current_user)
    await db.commit()
    return {"updated_count": len(pages)}


@router.put("/{page_id}/access")
async def update_page_access(
    page_id: uuid.UUID,
    payload: PageAccessUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _validate_access_mode(payload.access_mode)
    page = await db.scalar(
        select(PublishedPage).where(PublishedPage.id == page_id).with_for_update()
    )
    if not page or not await can_manage_page(db, page, current_user):
        raise HTTPException(404, "Page not found")
    if page.tenant_id is None:
        page.tenant_id = current_user.tenant_id
    allowed_ids = await _validated_allowed_user_ids(db, page.tenant_id, payload)
    await _apply_page_access_update(db, page, payload.access_mode, allowed_ids, current_user)
    await db.commit()
    # PostgreSQL updates ``updated_at`` server-side. Refresh explicitly before
    # serializing so accessing the expired attribute never triggers sync IO in
    # the async ORM context (the first real change otherwise raises MissingGreenlet).
    await db.refresh(page)
    return await _page_detail(db, page)


@router.put("/{page_id}/requests/{user_id}")
async def resolve_page_request(
    page_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: PageRequestResolution,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.status not in {"approved", "rejected"}:
        raise HTTPException(422, "Status must be approved or rejected")
    page = await db.get(PublishedPage, page_id)
    if not page or not await can_manage_page(db, page, current_user):
        raise HTTPException(404, "Page not found")
    row = await db.scalar(select(PublishedPageAccess).where(
        PublishedPageAccess.page_id == page_id, PublishedPageAccess.user_id == user_id
    ))
    if not row or row.status != "pending":
        raise HTTPException(404, "Pending request not found")
    row.status = payload.status
    row.resolved_at = datetime.now(timezone.utc)
    row.resolved_by = current_user.id
    await send_notification(
        db, user_id=user_id, type="page_access_resolved",
        title="页面访问申请已处理",
        body=f"你对「{page.title or page.source_path}」的访问申请已{'通过' if payload.status == 'approved' else '拒绝'}",
        link=f"/p/{page.short_id}" if payload.status == "approved" else None,
        ref_id=page.id, sender_name=current_user.display_name,
    )
    db.add(AuditLog(user_id=current_user.id, agent_id=page.agent_id, action="published_page_access_resolved", details={
        "page_id": str(page.id), "requester_id": str(user_id), "status": payload.status
    }))
    await db.commit()
    return {"status": payload.status}


@router.get("/list")
async def list_pages(agent_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.core.permissions import check_agent_access
    _agent, access = await check_agent_access(db, current_user, agent_id)
    if access != "manage":
        raise HTTPException(403, "Manage access required")
    rows = (await db.execute(
        select(PublishedPage, Agent.name)
        .join(Agent, Agent.id == PublishedPage.agent_id)
        .where(PublishedPage.agent_id == agent_id)
        .order_by(PublishedPage.created_at.desc())
    )).all()
    return await _page_summaries(db, rows)
