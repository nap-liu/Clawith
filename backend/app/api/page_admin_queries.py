"""Published-page management list queries."""

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.page_admin_helpers import _page_summaries
from app.core.permissions import is_platform_admin_user
from app.models.agent import Agent
from app.models.published_page import PublishedPage
from app.models.user import User


def _escaped_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _address_search_terms(search_text: str) -> list[str]:
    terms = [search_text]
    if "/p/" in search_text:
        short_id = search_text.rsplit("/p/", 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")
        if short_id and short_id != search_text:
            terms.append(short_id)
    return terms


async def list_manageable_pages(
    db: AsyncSession,
    *,
    current_user: User,
    page: int,
    page_size: int,
    agent_id: uuid.UUID | None,
    agent_ids: list[uuid.UUID],
    access_mode: str | None,
    search: str | None,
) -> dict:
    """List tenant-scoped pages the caller may manage, with exact and fuzzy filters."""
    creator = aliased(User, name="page_creator")
    modifier = aliased(User, name="page_modifier")
    conditions = [or_(
        PublishedPage.tenant_id == current_user.tenant_id,
        and_(PublishedPage.tenant_id.is_(None), Agent.tenant_id == current_user.tenant_id),
    )]
    if not (is_platform_admin_user(current_user) or current_user.role == "org_admin"):
        conditions.append(or_(Agent.creator_id == current_user.id, PublishedPage.user_id == current_user.id))

    selected_agent_ids = set(agent_ids)
    if agent_id:
        selected_agent_ids.add(agent_id)
    if selected_agent_ids:
        conditions.append(PublishedPage.agent_id.in_(selected_agent_ids))
    if access_mode:
        conditions.append(PublishedPage.access_mode == access_mode)

    search_text = (search or "").strip()
    if search_text:
        patterns = [_escaped_pattern(term) for term in _address_search_terms(search_text)]
        conditions.append(or_(
            *(PublishedPage.title.ilike(pattern, escape="\\") for pattern in patterns),
            *(PublishedPage.source_path.ilike(pattern, escape="\\") for pattern in patterns),
            *(PublishedPage.short_id.ilike(pattern, escape="\\") for pattern in patterns),
            *(creator.display_name.ilike(pattern, escape="\\") for pattern in patterns),
            *(modifier.display_name.ilike(pattern, escape="\\") for pattern in patterns),
        ))

    count_query = (
        select(func.count())
        .select_from(PublishedPage)
        .join(Agent, Agent.id == PublishedPage.agent_id)
    )
    rows_query = select(PublishedPage, Agent.name).join(Agent, Agent.id == PublishedPage.agent_id)
    if search_text:
        count_query = count_query.outerjoin(creator, creator.id == PublishedPage.user_id).outerjoin(
            modifier, modifier.id == PublishedPage.last_published_by_user_id
        )
        rows_query = rows_query.outerjoin(creator, creator.id == PublishedPage.user_id).outerjoin(
            modifier, modifier.id == PublishedPage.last_published_by_user_id
        )

    total = await db.scalar(count_query.where(*conditions))
    rows = (await db.execute(
        rows_query
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
