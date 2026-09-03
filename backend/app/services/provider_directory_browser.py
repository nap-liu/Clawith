"""Lazy, provider-scoped organization browser queries."""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.identity import IdentityProvider
from app.models.org import OrgDepartment, OrgMember
from app.services.org_directory_tree import (
    directory_edges_subquery,
    same_directory_provider,
    virtual_root_condition,
)
from app.services.org_sync_models import strip_virtual_root_path


async def _active_provider(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider_id: uuid.UUID,
) -> IdentityProvider:
    provider = await db.scalar(
        select(IdentityProvider).where(
            IdentityProvider.id == provider_id,
            IdentityProvider.tenant_id == tenant_id,
            IdentityProvider.is_active.is_(True),
        )
    )
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Identity provider not found",
        )
    return provider


async def provider_directory_departments(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = 100,
) -> dict:
    """Return one visible directory level, or bounded search results."""
    provider = await _active_provider(
        db,
        tenant_id=tenant_id,
        provider_id=provider_id,
    )
    normalized_search = (search or "").strip()
    edges = directory_edges_subquery(
        tenant_id=tenant_id,
        name="provider_browser_edges",
    )
    conditions = [
        OrgDepartment.tenant_id == tenant_id,
        OrgDepartment.provider_id == provider_id,
        OrgDepartment.status == "active",
        OrgDepartment.member_count > 0,
        ~virtual_root_condition(),
    ]
    if normalized_search:
        pattern = f"%{normalized_search}%"
        conditions.append(
            or_(OrgDepartment.name.ilike(pattern), OrgDepartment.path.ilike(pattern))
        )
    elif parent_id:
        parent = await db.scalar(
            select(OrgDepartment.id).where(
                OrgDepartment.id == parent_id,
                OrgDepartment.tenant_id == tenant_id,
                OrgDepartment.provider_id == provider_id,
                OrgDepartment.status == "active",
                OrgDepartment.member_count > 0,
                ~virtual_root_condition(),
            )
        )
        if not parent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Department not found",
            )
        conditions.append(
            OrgDepartment.id.in_(
                select(edges.c.child_id).where(
                    edges.c.parent_id == parent_id,
                    same_directory_provider(edges.c.provider_id, provider_id),
                )
            )
        )
    else:
        visible_parent = aliased(OrgDepartment)
        conditions.append(
            ~exists(
                select(1)
                .select_from(edges)
                .join(visible_parent, visible_parent.id == edges.c.parent_id)
                .where(
                    edges.c.child_id == OrgDepartment.id,
                    same_directory_provider(edges.c.provider_id, provider_id),
                    visible_parent.tenant_id == tenant_id,
                    visible_parent.provider_id == provider_id,
                    visible_parent.status == "active",
                    visible_parent.member_count > 0,
                    ~virtual_root_condition(visible_parent),
                )
            )
        )

    departments = (
        await db.scalars(
            select(OrgDepartment)
            .where(*conditions)
            .order_by(OrgDepartment.name.asc())
            .limit(limit)
        )
    ).all()
    hidden_root_ids = set(
        (
            await db.scalars(
                select(OrgDepartment.id).where(
                    OrgDepartment.tenant_id == tenant_id,
                    OrgDepartment.provider_id == provider_id,
                    OrgDepartment.status == "active",
                    virtual_root_condition(),
                )
            )
        ).all()
    )
    target_ids = {department.id for department in departments}
    child_counts: dict[uuid.UUID, int] = {}
    if target_ids:
        visible_child = aliased(OrgDepartment)
        child_counts = {
            row.parent_id: int(row.child_count)
            for row in (
                await db.execute(
                    select(
                        edges.c.parent_id,
                        func.count(func.distinct(edges.c.child_id)).label("child_count"),
                    )
                    .join(visible_child, visible_child.id == edges.c.child_id)
                    .where(
                        edges.c.parent_id.in_(target_ids),
                        same_directory_provider(edges.c.provider_id, provider_id),
                        visible_child.tenant_id == tenant_id,
                        visible_child.provider_id == provider_id,
                        visible_child.status == "active",
                        visible_child.member_count > 0,
                        ~virtual_root_condition(visible_child),
                    )
                    .group_by(edges.c.parent_id)
                )
            ).all()
        }

    total_member = 0
    if parent_id is None and not normalized_search:
        total_member = int(
            await db.scalar(
                select(func.count(func.distinct(func.coalesce(OrgMember.user_id, OrgMember.id)))).where(
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.provider_id == provider_id,
                    OrgMember.status == "active",
                )
            )
            or 0
        )

    return {
        "items": [
            {
                "id": str(department.id),
                "external_id": department.external_id,
                "provider_id": str(provider.id),
                "provider_name": provider.name,
                "provider_type": provider.provider_type,
                "name": department.name,
                "parent_id": (
                    str(department.parent_id)
                    if department.parent_id and department.parent_id not in hidden_root_ids
                    else None
                ),
                "path": (
                    strip_virtual_root_path(department.path)
                    if department.parent_id in hidden_root_ids
                    else department.path
                ),
                "member_count": department.member_count,
                "has_children": child_counts.get(department.id, 0) > 0,
            }
            for department in departments
        ],
        "total_member": total_member,
    }
