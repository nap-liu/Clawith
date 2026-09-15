"""Paginated visitor queries after published-page management authorization."""

import uuid

from sqlalchemy import String, case, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.page_admin_queries import _escaped_pattern
from app.models.published_page import PublishedPageAnonymousVisitor, PublishedPageVisitor
from app.models.user import Identity, User


async def list_visitors(
    db: AsyncSession, *, page_id: uuid.UUID, page: int, page_size: int,
    search: str | None, overflow_key: str,
) -> dict:
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
    conditions = []
    search_text = (search or "").strip()
    if search_text:
        pattern = _escaped_pattern(search_text)
        anonymous_name = case(
            (combined_visitors.c.visitor_key == overflow_key, literal("其他匿名访客")),
            else_=literal("匿名访客 ") + func.upper(func.substr(combined_visitors.c.visitor_key, 1, 12)),
        )
        display_name = case(
            (combined_visitors.c.visitor_type == "anonymous", anonymous_name),
            else_=combined_visitors.c.display_name,
        )
        conditions.append(or_(
            display_name.ilike(pattern, escape="\\"),
            combined_visitors.c.email.ilike(pattern, escape="\\"),
        ))
    total = await db.scalar(select(func.count()).select_from(combined_visitors).where(*conditions))
    rows = (await db.execute(
        select(combined_visitors)
        .where(*conditions)
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
                    if row["visitor_key"] == overflow_key
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
