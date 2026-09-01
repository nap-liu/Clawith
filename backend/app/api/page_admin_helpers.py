"""Published page management helpers shared by page admin routes."""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.page_models import PageAccessUpdate
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.published_page import (
    PublishedPage,
    PublishedPageAccess,
    PublishedPageAnonymousVisitor,
    PublishedPageVisitor,
)
from app.models.user import Identity, User


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
