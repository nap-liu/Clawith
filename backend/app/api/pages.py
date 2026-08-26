"""Published-page rendering, access control, and owner management APIs."""

import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import String, and_, func, literal, or_, select, union_all, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import get_settings
from app.core.permissions import build_visible_agents_query, is_platform_admin_user
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.org import OrgDepartment, OrgMember
from app.models.published_page import (
    PublishedPage,
    PublishedPageAccess,
    PublishedPageAnonymousVisitor,
    PublishedPageVisitor,
)
from app.models.user import Identity, User
from app.services.notification_service import send_notification
from app.services.org_directory import canonical_org_member_id_subquery, department_subtree_cte, same_directory_provider
from app.services.published_page_access import (
    PAGE_SESSION_COOKIE,
    PAGE_SESSION_HOURS,
    can_manage_page,
    can_view_page,
    create_page_session,
    page_user_from_session,
)
from app.services.storage import get_storage_backend, normalize_storage_key

public_router = APIRouter(tags=["pages"])
router = APIRouter(prefix="/pages", tags=["pages"])
PUBLIC_VISITOR_COOKIE = "published_page_visitor"
PUBLIC_VISITOR_COOKIE_MAX_AGE = 365 * 24 * 3600
MAX_ANONYMOUS_VISITORS_PER_PAGE = 10_000
ANONYMOUS_VISITOR_OVERFLOW_KEY = "0" * 64
MAX_BULK_PAGE_ACCESS_UPDATES = 100
PUBLISHED_PAGE_UNAVAILABLE_PATH = "/published-page-unavailable"


class PageSessionRequest(BaseModel):
    short_id: str = Field(..., min_length=1, max_length=16)


class PageAccessUpdate(BaseModel):
    access_mode: str
    allowed_user_ids: list[uuid.UUID] = Field(default_factory=list)


class PageBulkAccessUpdate(PageAccessUpdate):
    page_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=MAX_BULK_PAGE_ACCESS_UPDATES)


class PageRequestResolution(BaseModel):
    status: str


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
    }
    if page.tenant_id:
        params["tenant_id"] = str(page.tenant_id)
    automatic_login = (request.query_params.get("auto_login") or "").lower() in {"1", "true"}
    if automatic_login:
        params["auto_login"] = "1"
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
        content_response.delete_cookie(PAGE_SESSION_COOKIE, path="/p/", samesite="lax")
        content_response.set_cookie(
            PAGE_SESSION_COOKIE,
            create_page_session(user.id),
            max_age=PAGE_SESSION_HOURS * 3600,
            httponly=True,
            samesite="lax",
            secure=_request_scheme(request) == "https",
            path="/",
        )
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
    return await _render_page_content(db, page, user, request)


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
    response.set_cookie(
        PAGE_SESSION_COOKIE,
        create_page_session(current_user.id),
        max_age=PAGE_SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=_request_scheme(request) == "https",
        path="/",
    )
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
    """Keep the former viewer API compatible without restoring its restrictions."""
    page, _user, access_error = await _resolve_page_view(short_id, request, db)
    if access_error == 401:
        raise HTTPException(401, "Page session expired")
    if access_error == 403:
        raise HTTPException(403, "无权访问此页面")

    response.headers["Cache-Control"] = "no-store"
    if page.access_mode == "public":
        _visitor_key, new_visitor_cookie = _anonymous_visitor(request, page.id)
        if new_visitor_cookie:
            _set_public_visitor_cookie(response, new_visitor_cookie, request)
    return {
        "title": page.title or page.source_path,
        "access_mode": page.access_mode,
        "watermark_identity": None,
        "watermark_text": None,
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


def _member_dict(row) -> dict:
    user, identity, access = row
    return {
        "id": str(user.id),
        "display_name": user.display_name,
        "email": identity.email if identity else None,
        "status": access.status,
        "requested_at": access.requested_at.isoformat() if access.requested_at else None,
    }


async def _page_actor_map(db: AsyncSession, pages: list[PublishedPage]) -> dict[uuid.UUID, dict]:
    user_ids = {
        user_id
        for page in pages
        for user_id in (page.user_id, page.last_published_by_user_id)
        if user_id is not None
    }
    if not user_ids:
        return {}
    rows = (await db.execute(
        select(User, Identity)
        .outerjoin(Identity, Identity.id == User.identity_id)
        .where(User.id.in_(user_ids))
    )).all()
    return {
        user.id: {
            "id": str(user.id),
            "display_name": user.display_name,
            "email": identity.email if identity else None,
        }
        for user, identity in rows
    }


async def _page_detail(db: AsyncSession, page: PublishedPage) -> dict:
    agent_name = await db.scalar(select(Agent.name).where(Agent.id == page.agent_id))
    actors = await _page_actor_map(db, [page])
    access_rows = (await db.execute(
        select(User, Identity, PublishedPageAccess)
        .join(PublishedPageAccess, PublishedPageAccess.user_id == User.id)
        .outerjoin(Identity, Identity.id == User.identity_id)
        .where(PublishedPageAccess.page_id == page.id)
        .order_by(PublishedPageAccess.requested_at.desc().nullslast(), User.display_name)
    )).all()
    authenticated_visitor_count = await db.scalar(
        select(func.count()).select_from(PublishedPageVisitor).where(PublishedPageVisitor.page_id == page.id)
    )
    anonymous_visitor_count = await db.scalar(
        select(func.count()).select_from(PublishedPageAnonymousVisitor).where(
            PublishedPageAnonymousVisitor.page_id == page.id
        )
    )
    return {
        "id": str(page.id), "short_id": page.short_id, "agent_id": str(page.agent_id), "agent_name": agent_name,
        "source_path": page.source_path, "title": page.title, "access_mode": page.access_mode,
        "view_count": page.view_count, "url": f"/p/{page.short_id}",
        "created_at": page.created_at.isoformat() if page.created_at else None,
        "updated_at": page.updated_at.isoformat() if page.updated_at else None,
        "created_by": actors.get(page.user_id),
        "last_published_by": actors.get(page.last_published_by_user_id),
        "last_published_at": page.last_published_at.isoformat() if page.last_published_at else None,
        "access_users": [_member_dict(row) for row in access_rows],
        "visitor_count": int(authenticated_visitor_count or 0) + int(anonymous_visitor_count or 0),
    }


async def _page_summaries(db: AsyncSession, rows: list[tuple[PublishedPage, str]]) -> list[dict]:
    actors = await _page_actor_map(db, [page for page, _agent_name in rows])
    page_ids = [page.id for page, _agent_name in rows]
    visitors_by_page: dict[uuid.UUID, list[dict]] = {page_id: [] for page_id in page_ids}
    visitor_counts: dict[uuid.UUID, int] = {}
    anonymous_visitor_counts: dict[uuid.UUID, int] = {}
    pending_request_counts: dict[uuid.UUID, int] = {}
    if page_ids:
        anonymous_visitor_counts = {
            row[0]: int(row[1]) for row in (await db.execute(
                select(PublishedPageAnonymousVisitor.page_id, func.count(PublishedPageAnonymousVisitor.id))
                .where(PublishedPageAnonymousVisitor.page_id.in_(page_ids))
                .group_by(PublishedPageAnonymousVisitor.page_id)
            )).all()
        }
        pending_request_counts = {
            row[0]: int(row[1]) for row in (await db.execute(
                select(PublishedPageAccess.page_id, func.count(PublishedPageAccess.id))
                .where(
                    PublishedPageAccess.page_id.in_(page_ids),
                    PublishedPageAccess.status == "pending",
                    PublishedPageAccess.requested_at.is_not(None),
                )
                .group_by(PublishedPageAccess.page_id)
            )).all()
        }
        ranked_visitors = select(
            PublishedPageVisitor.page_id.label("page_id"),
            PublishedPageVisitor.user_id.label("user_id"),
            PublishedPageVisitor.last_viewed_at.label("last_viewed_at"),
            func.row_number().over(
                partition_by=PublishedPageVisitor.page_id,
                order_by=PublishedPageVisitor.last_viewed_at.desc(),
            ).label("row_number"),
            func.count().over(partition_by=PublishedPageVisitor.page_id).label("visitor_count"),
        ).where(PublishedPageVisitor.page_id.in_(page_ids)).subquery()
        visitor_rows = (await db.execute(
            select(
                ranked_visitors.c.page_id,
                User.id,
                User.display_name,
                ranked_visitors.c.last_viewed_at,
                ranked_visitors.c.visitor_count,
            )
            .join(User, User.id == ranked_visitors.c.user_id)
            .where(ranked_visitors.c.row_number <= 3)
            .order_by(ranked_visitors.c.page_id, ranked_visitors.c.row_number)
        )).all()
        for page_id, user_id, display_name, last_viewed_at, visitor_count in visitor_rows:
            visitor_counts[page_id] = visitor_count
            visitors_by_page[page_id].append({
                "id": str(user_id),
                "display_name": display_name,
                "last_viewed_at": last_viewed_at.isoformat() if last_viewed_at else None,
            })

    summaries = []
    for page, agent_name in rows:
        visitors = visitors_by_page.get(page.id, [])
        summaries.append({
            "id": str(page.id),
            "short_id": page.short_id,
            "agent_id": str(page.agent_id),
            "agent_name": agent_name,
            "source_path": page.source_path,
            "title": page.title,
            "access_mode": page.access_mode,
            "view_count": page.view_count,
            "url": f"/p/{page.short_id}",
            "created_at": page.created_at.isoformat() if page.created_at else None,
            "updated_at": page.updated_at.isoformat() if page.updated_at else None,
            "created_by": actors.get(page.user_id),
            "last_published_by": actors.get(page.last_published_by_user_id),
            "last_published_at": page.last_published_at.isoformat() if page.last_published_at else None,
            "visitor_count": visitor_counts.get(page.id, 0) + anonymous_visitor_counts.get(page.id, 0),
            "pending_request_count": pending_request_counts.get(page.id, 0),
            "visitors": visitors,
        })
    return summaries


@router.get("/agent-options")
async def list_page_agent_options(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if is_platform_admin_user(current_user) or current_user.role == "org_admin":
        query = select(Agent).where(
            Agent.tenant_id == current_user.tenant_id,
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
    q: str | None = Query(default=None, max_length=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    base_conditions = [or_(
        PublishedPage.tenant_id == current_user.tenant_id,
        and_(PublishedPage.tenant_id.is_(None), Agent.tenant_id == current_user.tenant_id),
    )]
    if not (is_platform_admin_user(current_user) or current_user.role == "org_admin"):
        base_conditions.append(or_(Agent.creator_id == current_user.id, PublishedPage.user_id == current_user.id))
    conditions = list(base_conditions)
    selected_agent_ids = set(agent_ids)
    if agent_id:
        selected_agent_ids.add(agent_id)
    if selected_agent_ids:
        conditions.append(PublishedPage.agent_id.in_(selected_agent_ids))
    search_text = (q or "").strip()
    if search_text:
        escaped = search_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        conditions.append(or_(
            PublishedPage.title.ilike(pattern, escape="\\"),
            PublishedPage.source_path.ilike(pattern, escape="\\"),
        ))
    total = await db.scalar(
        select(func.count()).select_from(PublishedPage).join(Agent, Agent.id == PublishedPage.agent_id).where(*conditions)
    )
    rows = (await db.execute(
        select(PublishedPage, Agent.name)
        .join(Agent, Agent.id == PublishedPage.agent_id)
        .where(*conditions)
        .order_by(PublishedPage.updated_at.desc(), PublishedPage.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all()
    return {
        "items": await _page_summaries(db, rows),
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
    }


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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    published_page = await db.get(PublishedPage, page_id)
    if not published_page or not await can_manage_page(db, published_page, current_user):
        raise HTTPException(404, "Page not found")
    authenticated_visitors = (
        select(
            PublishedPageVisitor.user_id.label("id"),
            User.display_name.label("display_name"),
            Identity.email.label("email"),
            literal("authenticated", String).label("visitor_type"),
            literal(None, String).label("visitor_key"),
            PublishedPageVisitor.view_count.label("view_count"),
            PublishedPageVisitor.first_viewed_at.label("first_viewed_at"),
            PublishedPageVisitor.last_viewed_at.label("last_viewed_at"),
        )
        .join(PublishedPageVisitor, PublishedPageVisitor.user_id == User.id)
        .outerjoin(Identity, Identity.id == User.identity_id)
        .where(PublishedPageVisitor.page_id == page_id)
    )
    anonymous_visitors = select(
        PublishedPageAnonymousVisitor.id.label("id"),
        literal("匿名访客", String).label("display_name"),
        literal(None, String).label("email"),
        literal("anonymous", String).label("visitor_type"),
        PublishedPageAnonymousVisitor.visitor_key.label("visitor_key"),
        PublishedPageAnonymousVisitor.view_count.label("view_count"),
        PublishedPageAnonymousVisitor.first_viewed_at.label("first_viewed_at"),
        PublishedPageAnonymousVisitor.last_viewed_at.label("last_viewed_at"),
    ).where(PublishedPageAnonymousVisitor.page_id == page_id)
    combined_visitors = union_all(authenticated_visitors, anonymous_visitors).subquery()
    total = await db.scalar(select(func.count()).select_from(combined_visitors))
    rows = (await db.execute(
        select(combined_visitors)
        .order_by(combined_visitors.c.last_viewed_at.desc(), combined_visitors.c.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).mappings().all()
    return {
        "items": [{
            "id": (
                f"anonymous:{row['id']}" if row["visitor_type"] == "anonymous"
                else str(row["id"])
            ),
            "display_name": (
                (
                    "其他匿名访客"
                    if row["visitor_key"] == ANONYMOUS_VISITOR_OVERFLOW_KEY
                    else f"匿名访客 {row['visitor_key'][:12].upper()}"
                )
                if row["visitor_type"] == "anonymous"
                else row["display_name"]
            ),
            "email": row["email"],
            "visitor_type": row["visitor_type"],
            "view_count": row["view_count"],
            "first_viewed_at": row["first_viewed_at"].isoformat() if row["first_viewed_at"] else None,
            "last_viewed_at": row["last_viewed_at"].isoformat() if row["last_viewed_at"] else None,
        } for row in rows],
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
    }


async def _manageable_page(db: AsyncSession, page_id: uuid.UUID, user: User) -> PublishedPage:
    published_page = await db.get(PublishedPage, page_id)
    if not published_page or not await can_manage_page(db, published_page, user):
        raise HTTPException(404, "Page not found")
    return published_page


def _directory_department(
    department: OrgDepartment,
    child_counts: dict[uuid.UUID, int],
    member_counts: dict[uuid.UUID, int],
) -> dict:
    return {
        "id": str(department.id),
        "name": department.name,
        "parent_id": str(department.parent_id) if department.parent_id else None,
        "path": department.path,
        "has_children": child_counts.get(department.id, 0) > 0,
        "direct_member_count": member_counts.get(department.id, 0),
    }


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
    tenant_id = published_page.tenant_id
    if not tenant_id:
        return {"items": [], "my_department": None}
    conditions = [OrgDepartment.tenant_id == tenant_id, OrgDepartment.status == "active"]
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        conditions.append(or_(OrgDepartment.name.ilike(pattern), OrgDepartment.path.ilike(pattern)))
    elif parent_id:
        parent = await db.scalar(select(OrgDepartment).where(
            OrgDepartment.id == parent_id,
            OrgDepartment.tenant_id == tenant_id,
            OrgDepartment.status == "active",
        ))
        if not parent:
            raise HTTPException(404, "Department not found")
        conditions.extend([
            OrgDepartment.parent_id == parent_id,
            OrgDepartment.provider_id == parent.provider_id if parent.provider_id else OrgDepartment.provider_id.is_(None),
        ])
    else:
        conditions.append(OrgDepartment.parent_id.is_(None))
    departments = (await db.scalars(
        select(OrgDepartment).where(*conditions).order_by(OrgDepartment.name.asc()).limit(limit)
    )).all()
    my_department = None
    if not normalized_search and parent_id is None:
        my_department = await db.scalar(
            select(OrgDepartment)
            .join(OrgMember, OrgMember.department_id == OrgDepartment.id)
            .where(
                OrgMember.tenant_id == tenant_id,
                OrgMember.status == "active",
                OrgMember.user_id == current_user.id,
                OrgDepartment.status == "active",
            )
            .order_by(OrgMember.synced_at.desc())
            .limit(1)
        )
    target_ids = {department.id for department in departments}
    if my_department:
        target_ids.add(my_department.id)
    child_counts: dict[uuid.UUID, int] = {}
    member_counts: dict[uuid.UUID, int] = {}
    if target_ids:
        parent_department = aliased(OrgDepartment)
        child_counts = {
            row[0]: int(row[1]) for row in (await db.execute(
                select(OrgDepartment.parent_id, func.count(OrgDepartment.id))
                .join(parent_department, parent_department.id == OrgDepartment.parent_id)
                .where(
                    OrgDepartment.tenant_id == tenant_id,
                    OrgDepartment.status == "active",
                    OrgDepartment.parent_id.in_(target_ids),
                    parent_department.tenant_id == tenant_id,
                    parent_department.status == "active",
                    same_directory_provider(OrgDepartment.provider_id, parent_department.provider_id),
                )
                .group_by(OrgDepartment.parent_id)
            )).all() if row[0]
        }
        member_counts = {
            row[0]: int(row[1]) for row in (await db.execute(
                select(OrgMember.department_id, func.count(func.distinct(OrgMember.user_id)))
                .join(User, User.id == OrgMember.user_id)
                .where(
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.status == "active",
                    OrgMember.department_id.in_(target_ids),
                    User.tenant_id == tenant_id,
                    User.is_active.is_(True),
                )
                .group_by(OrgMember.department_id)
            )).all() if row[0]
        }
    return {
        "items": [_directory_department(item, child_counts, member_counts) for item in departments],
        "my_department": _directory_department(my_department, child_counts, member_counts) if my_department else None,
    }


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
    tenant_id = published_page.tenant_id
    if not tenant_id:
        return {"items": [], "page": page, "page_size": page_size, "total": 0, "has_more": False}
    filters = [
        OrgMember.tenant_id == tenant_id,
        OrgMember.status == "active",
        OrgMember.user_id.is_not(None),
        User.tenant_id == tenant_id,
        User.is_active.is_(True),
    ]
    canonical_department_ids = None
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(or_(
            OrgMember.name.ilike(pattern),
            OrgMember.nickname.ilike(pattern),
            OrgMember.name_translit_full.ilike(pattern),
            OrgMember.name_translit_initial.ilike(pattern),
            OrgMember.department_path.ilike(pattern),
            OrgMember.title.ilike(pattern),
            OrgMember.email.ilike(pattern),
        ))
    elif department_id:
        department = await db.scalar(select(OrgDepartment).where(
            OrgDepartment.id == department_id,
            OrgDepartment.tenant_id == tenant_id,
            OrgDepartment.status == "active",
        ))
        if not department:
            raise HTTPException(404, "Department not found")
        if include_descendants:
            subtree = department_subtree_cte(
                tenant_id=tenant_id,
                department_id=department.id,
                name="page_permission_picker_department_subtree",
            )
            canonical_department_ids = select(subtree.c.department_id)
            filters.append(OrgMember.department_id.in_(canonical_department_ids))
        else:
            canonical_department_ids = [department.id]
            filters.append(OrgMember.department_id == department.id)
    canonical = canonical_org_member_id_subquery(
        tenant_id=tenant_id,
        department_ids=canonical_department_ids,
        prefer_directory_profile=True,
    )
    base_query = (
        select(OrgMember)
        .join(canonical, and_(OrgMember.id == canonical.c.om_id, canonical.c.rn == 1))
        .join(User, User.id == OrgMember.user_id)
        .where(*filters)
    )
    total = int(await db.scalar(select(func.count()).select_from(base_query.subquery())) or 0)
    members = (await db.scalars(
        base_query.order_by(OrgMember.name.asc(), OrgMember.id.asc()).offset((page - 1) * page_size).limit(page_size)
    )).all()
    return {
        "items": [{
            "id": str(member.user_id),
            "member_id": str(member.id),
            "name": member.name,
            "nickname": member.nickname,
            "department_id": str(member.department_id) if member.department_id else None,
            "department_path": member.department_path or "",
            "title": member.title or "",
            "avatar_url": member.avatar_url,
            "email": member.email,
        } for member in members],
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": page * page_size < total,
    }


def _validate_access_mode(access_mode: str) -> None:
    if access_mode not in {"public", "authenticated", "restricted"}:
        raise HTTPException(422, "Invalid access mode")


async def _validated_allowed_user_ids(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    payload: PageAccessUpdate,
) -> set[uuid.UUID]:
    allowed_ids = set(payload.allowed_user_ids) if payload.access_mode == "restricted" else set()
    if not allowed_ids:
        return allowed_ids
    valid_ids = set((await db.scalars(select(User.id).where(
        User.id.in_(allowed_ids), User.tenant_id == tenant_id, User.is_active.is_(True)
    ))).all())
    if valid_ids != allowed_ids:
        raise HTTPException(422, "Allowed users must be active members of the same company")
    return allowed_ids


async def _apply_page_access_update(
    db: AsyncSession,
    page: PublishedPage,
    access_mode: str,
    allowed_ids: set[uuid.UUID],
    current_user: User,
) -> None:
    page.access_mode = access_mode
    existing = (await db.scalars(select(PublishedPageAccess).where(PublishedPageAccess.page_id == page.id))).all()
    by_user = {row.user_id: row for row in existing}
    now = datetime.now(timezone.utc)
    for row in existing:
        if row.status == "approved" and row.user_id not in allowed_ids:
            await db.delete(row)
    for user_id in allowed_ids:
        row = by_user.get(user_id)
        if row:
            row.status = "approved"
            row.resolved_at = now
            row.resolved_by = current_user.id
        else:
            db.add(PublishedPageAccess(
                page_id=page.id, user_id=user_id, status="approved", resolved_at=now, resolved_by=current_user.id
            ))
    db.add(AuditLog(user_id=current_user.id, agent_id=page.agent_id, action="published_page_access_updated", details={
        "page_id": str(page.id), "access_mode": access_mode,
        "allowed_user_ids": sorted(str(value) for value in allowed_ids),
    }))


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
