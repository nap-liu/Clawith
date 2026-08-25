"""Shared organization-directory query helpers.

The enterprise directory and permission picker must resolve duplicate synced
profiles in exactly the same way.  Keep the canonical-member ranking here so a
single platform user never appears twice when identity providers overlap.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.agent import AgentPermission
from app.models.org import AgentRelationship, OrgDepartment, OrgMember
from app.models.user import Identity, User


def same_directory_provider(left, right):
    """Null-safe provider equality for rows from the same directory tree."""
    return or_(left == right, and_(left.is_(None), right.is_(None)))


def department_subtree_cte(
    *,
    tenant_id: uuid.UUID,
    department_id: uuid.UUID,
    name: str = "department_subtree",
):
    """Return active department IDs rooted at ``department_id``.

    The traversal follows internal parent IDs and never crosses an identity
    provider boundary.  Names and display paths are deliberately not identities.
    ``UNION`` (rather than ``UNION ALL``) also makes malformed cycles terminate.
    """
    subtree = (
        select(
            OrgDepartment.id.label("department_id"),
            OrgDepartment.provider_id.label("provider_id"),
        )
        .where(
            OrgDepartment.id == department_id,
            OrgDepartment.tenant_id == tenant_id,
            OrgDepartment.status == "active",
        )
        .cte(name=name, recursive=True)
    )
    child = aliased(OrgDepartment)
    subtree = subtree.union(
        select(child.id, child.provider_id)
        .join(
            subtree,
            and_(
                child.parent_id == subtree.c.department_id,
                same_directory_provider(child.provider_id, subtree.c.provider_id),
            ),
        )
        .where(
            child.tenant_id == tenant_id,
            child.status == "active",
        )
    )
    return subtree


def agent_permission_department_subtree_cte(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    name: str = "agent_permission_department_subtree",
):
    """Expand department grants to active descendant IDs per agent."""
    root_conditions = [
        AgentPermission.scope_type == "department",
        OrgDepartment.tenant_id == tenant_id,
        OrgDepartment.status == "active",
    ]
    if agent_id is not None:
        root_conditions.append(AgentPermission.agent_id == agent_id)

    grants = (
        select(
            AgentPermission.id.label("permission_id"),
            AgentPermission.agent_id.label("agent_id"),
            OrgDepartment.id.label("department_id"),
            OrgDepartment.provider_id.label("provider_id"),
            AgentPermission.access_level.label("access_level"),
        )
        .select_from(AgentPermission)
        .join(OrgDepartment, AgentPermission.scope_id == OrgDepartment.id)
        .where(*root_conditions)
        .cte(name=name, recursive=True)
    )
    child = aliased(OrgDepartment)
    grants = grants.union(
        select(
            grants.c.permission_id,
            grants.c.agent_id,
            child.id,
            child.provider_id,
            grants.c.access_level,
        )
        .join(
            grants,
            and_(
                child.parent_id == grants.c.department_id,
                same_directory_provider(child.provider_id, grants.c.provider_id),
            ),
        )
        .where(
            child.tenant_id == tenant_id,
            child.status == "active",
        )
    )
    return grants


def canonical_org_member_id_subquery(
    *,
    tenant_id: uuid.UUID | None = None,
    provider_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    department_ids=None,
    prefer_directory_profile: bool = False,
):
    """Return a ranked subquery exposing one canonical OrgMember per user."""
    relationship_counts = None
    is_real_name = case(
        (
            and_(
                OrgMember.name.notlike("Oauth2 User %"),
                OrgMember.name.notlike("Dingtalk User %"),
                OrgMember.name.notlike("Wecom User %"),
                OrgMember.name.notlike("Feishu User %"),
            ),
            1,
        ),
        else_=0,
    )
    order_by = []
    if prefer_directory_profile:
        order_by.append(case((OrgMember.department_id.is_not(None), 1), else_=0).desc())
    else:
        # Legacy enterprise-directory selection prefers the profile already used
        # by relationships. Aggregate once instead of running a correlated count
        # for every OrgMember row. Permission-directory queries deliberately skip
        # this relationship-table work altogether.
        relationship_counts = (
            select(
                AgentRelationship.member_id.label("member_id"),
                func.count(AgentRelationship.id).label("relationship_count"),
            )
            .where(AgentRelationship.member_id.is_not(None))
            .group_by(AgentRelationship.member_id)
            .subquery()
        )
        order_by.append(
            func.coalesce(relationship_counts.c.relationship_count, 0).desc()
        )
    order_by.extend(
        [
            case((OrgMember.external_id.is_not(None), 1), else_=0).desc(),
            is_real_name.desc(),
            OrgMember.synced_at.asc(),
        ]
    )
    row_number = func.row_number().over(
        partition_by=func.coalesce(OrgMember.user_id, OrgMember.id),
        order_by=order_by,
    ).label("rn")

    conditions = [OrgMember.status == "active"]
    if tenant_id:
        conditions.append(OrgMember.tenant_id == tenant_id)
    if provider_id:
        conditions.append(OrgMember.provider_id == provider_id)
    if user_id:
        conditions.append(OrgMember.user_id == user_id)
    if department_ids is not None:
        conditions.append(OrgMember.department_id.in_(department_ids))

    query = select(
        OrgMember.id.label("om_id"),
        OrgMember.user_id.label("user_id"),
        row_number,
    )
    if relationship_counts is not None:
        query = query.outerjoin(
            relationship_counts,
            relationship_counts.c.member_id == OrgMember.id,
        )
    return query.where(*conditions).subquery()


def _serialize_picker_department(
    department: OrgDepartment,
    *,
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


async def permission_directory_departments(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    current_user_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = 100,
) -> dict:
    """Return the shared lazy department tree after a caller-specific access check."""
    if not tenant_id:
        return {"items": [], "my_department": None}

    conditions = [
        OrgDepartment.tenant_id == tenant_id,
        OrgDepartment.status == "active",
    ]
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        conditions.append(
            or_(
                OrgDepartment.name.ilike(pattern),
                OrgDepartment.path.ilike(pattern),
            )
        )
    elif parent_id:
        parent = await db.scalar(
            select(OrgDepartment).where(
                OrgDepartment.id == parent_id,
                OrgDepartment.tenant_id == tenant_id,
                OrgDepartment.status == "active",
            )
        )
        if not parent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Department not found",
            )
        conditions.extend(
            [
                OrgDepartment.parent_id == parent_id,
                (
                    OrgDepartment.provider_id == parent.provider_id
                    if parent.provider_id is not None
                    else OrgDepartment.provider_id.is_(None)
                ),
            ]
        )
    else:
        conditions.append(OrgDepartment.parent_id.is_(None))

    departments = (
        await db.scalars(
            select(OrgDepartment)
            .where(*conditions)
            .order_by(OrgDepartment.name.asc())
            .limit(limit)
        )
    ).all()
    my_department = None
    if not normalized_search and parent_id is None:
        my_department = await db.scalar(
            select(OrgDepartment)
            .join(OrgMember, OrgMember.department_id == OrgDepartment.id)
            .where(
                OrgMember.tenant_id == tenant_id,
                OrgMember.status == "active",
                OrgMember.user_id == current_user_id,
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
            row[0]: int(row[1])
            for row in (
                await db.execute(
                    select(OrgDepartment.parent_id, func.count(OrgDepartment.id))
                    .join(
                        parent_department,
                        parent_department.id == OrgDepartment.parent_id,
                    )
                    .where(
                        OrgDepartment.tenant_id == tenant_id,
                        OrgDepartment.status == "active",
                        OrgDepartment.parent_id.in_(target_ids),
                        parent_department.tenant_id == tenant_id,
                        parent_department.status == "active",
                        same_directory_provider(
                            OrgDepartment.provider_id,
                            parent_department.provider_id,
                        ),
                    )
                    .group_by(OrgDepartment.parent_id)
                )
            ).all()
            if row[0]
        }
        member_counts = {
            row[0]: int(row[1])
            for row in (
                await db.execute(
                    select(
                        OrgMember.department_id,
                        func.count(func.distinct(OrgMember.user_id)),
                    )
                    .join(User, User.id == OrgMember.user_id)
                    .where(
                        OrgMember.tenant_id == tenant_id,
                        OrgMember.status == "active",
                        OrgMember.department_id.in_(target_ids),
                        User.tenant_id == tenant_id,
                        User.is_active.is_(True),
                    )
                    .group_by(OrgMember.department_id)
                )
            ).all()
            if row[0]
        }

    return {
        "items": [
            _serialize_picker_department(
                department,
                child_counts=child_counts,
                member_counts=member_counts,
            )
            for department in departments
        ],
        "my_department": (
            _serialize_picker_department(
                my_department,
                child_counts=child_counts,
                member_counts=member_counts,
            )
            if my_department
            else None
        ),
    }


async def permission_directory_members(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    department_id: uuid.UUID | None = None,
    include_descendants: bool = False,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
    excluded_user_ids: set[uuid.UUID] | None = None,
) -> dict:
    """Return canonical active users after a caller-specific access check."""
    if not tenant_id:
        return {
            "items": [],
            "page": page,
            "page_size": page_size,
            "total": 0,
            "has_more": False,
        }

    canonical_department_ids = None
    normalized_search = (search or "").strip()
    if not normalized_search and department_id:
        department = await db.scalar(
            select(OrgDepartment).where(
                OrgDepartment.id == department_id,
                OrgDepartment.tenant_id == tenant_id,
                OrgDepartment.status == "active",
            )
        )
        if not department:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Department not found",
            )
        if include_descendants:
            subtree = department_subtree_cte(
                tenant_id=tenant_id,
                department_id=department.id,
                name="permission_picker_department_subtree",
            )
            canonical_department_ids = select(subtree.c.department_id)
        else:
            canonical_department_ids = [department.id]

    canonical = canonical_org_member_id_subquery(
        tenant_id=tenant_id,
        department_ids=canonical_department_ids,
        prefer_directory_profile=True,
    )
    profile = aliased(OrgMember)
    filters = [User.tenant_id == tenant_id, User.is_active.is_(True)]
    if excluded_user_ids:
        filters.append(User.id.not_in(excluded_user_ids))
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                User.display_name.ilike(pattern),
                User.title.ilike(pattern),
                Identity.email.ilike(pattern),
                Identity.username.ilike(pattern),
                profile.name.ilike(pattern),
                profile.nickname.ilike(pattern),
                profile.name_translit_full.ilike(pattern),
                profile.name_translit_initial.ilike(pattern),
                profile.department_path.ilike(pattern),
                profile.title.ilike(pattern),
                profile.email.ilike(pattern),
            )
        )
    elif department_id:
        filters.append(canonical.c.om_id.is_not(None))

    base_query = (
        select(User, profile, Identity)
        .select_from(User)
        .outerjoin(
            canonical,
            and_(canonical.c.user_id == User.id, canonical.c.rn == 1),
        )
        .outerjoin(profile, profile.id == canonical.c.om_id)
        .outerjoin(Identity, Identity.id == User.identity_id)
        .where(*filters)
    )
    total = int(
        await db.scalar(select(func.count()).select_from(base_query.subquery()))
        or 0
    )
    members = (
        await db.execute(
            base_query
            .order_by(func.coalesce(profile.name, User.display_name).asc(), User.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return {
        "items": [
            {
                "id": str(user.id),
                "member_id": str(member.id) if member else None,
                "name": user.display_name,
                "nickname": member.nickname if member else None,
                "department_id": (
                    str(member.department_id)
                    if member and member.department_id
                    else None
                ),
                "department_path": (member.department_path or "") if member else "",
                "title": user.title or "",
                "avatar_url": user.avatar_url,
                "email": identity.email if identity else None,
            }
            for user, member, identity in members
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": page * page_size < total,
    }
