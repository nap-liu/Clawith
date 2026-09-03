"""Shared organization-directory query helpers.

The enterprise directory and permission picker must resolve duplicate synced
profiles in exactly the same way.  Keep the canonical-member ranking here so a
single platform user never appears twice when identity providers overlap.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.agent import AgentPermission
from app.models.identity import IdentityProvider
from app.models.org import (
    AgentRelationship,
    ChannelUserBinding,
    DirectoryAccountGroup,
    OrgDepartment,
    OrgMember,
)
from app.models.user import Identity, User
from app.services.org_directory_tree import (
    directory_edges_subquery,
    directory_memberships_subquery,
    platform_standard_provider_ids,
    same_directory_provider,
    virtual_root_condition,
)
from app.services.provider_identity_policy import mask_identity_claim


def member_in_directory_group(member, *, group_id, provider_id):
    """Match an account through normalized membership with legacy fallback."""
    return or_(
        and_(
            member.department_id == group_id,
            same_directory_provider(member.provider_id, provider_id),
        ),
        exists(
            select(DirectoryAccountGroup.id).where(
                DirectoryAccountGroup.account_id == member.id,
                DirectoryAccountGroup.tenant_id == member.tenant_id,
                DirectoryAccountGroup.provider_id == provider_id,
                DirectoryAccountGroup.group_id == group_id,
            )
        ),
    )


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
        .outerjoin(IdentityProvider, IdentityProvider.id == OrgDepartment.provider_id)
        .where(
            OrgDepartment.id == department_id,
            OrgDepartment.tenant_id == tenant_id,
            OrgDepartment.status == "active",
            or_(
                OrgDepartment.provider_id.is_(None),
                IdentityProvider.is_active.is_(True),
            ),
        )
        .cte(name=name, recursive=True)
    )
    child = aliased(OrgDepartment)
    edges = directory_edges_subquery(tenant_id=tenant_id, name=f"{name}_edges")
    subtree = subtree.union(
        select(child.id, child.provider_id)
        .join(edges, edges.c.child_id == child.id)
        .join(
            subtree,
            and_(
                edges.c.parent_id == subtree.c.department_id,
                same_directory_provider(edges.c.provider_id, subtree.c.provider_id),
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
        .outerjoin(IdentityProvider, IdentityProvider.id == OrgDepartment.provider_id)
        .where(
            *root_conditions,
            or_(
                OrgDepartment.provider_id.is_(None),
                IdentityProvider.is_active.is_(True),
            ),
        )
        .cte(name=name, recursive=True)
    )
    child = aliased(OrgDepartment)
    edges = directory_edges_subquery(tenant_id=tenant_id, name=f"{name}_edges")
    grants = grants.union(
        select(
            grants.c.permission_id,
            grants.c.agent_id,
            child.id,
            child.provider_id,
            grants.c.access_level,
        )
        .join(edges, edges.c.child_id == child.id)
        .join(
            grants,
            and_(
                edges.c.parent_id == grants.c.department_id,
                same_directory_provider(edges.c.provider_id, grants.c.provider_id),
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

    conditions = [
        OrgMember.status == "active",
        or_(OrgMember.provider_id.is_(None), IdentityProvider.is_active.is_(True)),
    ]
    if tenant_id:
        conditions.append(OrgMember.tenant_id == tenant_id)
    if provider_id:
        conditions.append(OrgMember.provider_id == provider_id)
    if user_id:
        conditions.append(OrgMember.user_id == user_id)
    if department_ids is not None:
        conditions.append(
            or_(
                OrgMember.department_id.in_(department_ids),
                exists(
                    select(DirectoryAccountGroup.id).where(
                        DirectoryAccountGroup.account_id == OrgMember.id,
                        DirectoryAccountGroup.tenant_id == OrgMember.tenant_id,
                        DirectoryAccountGroup.provider_id == OrgMember.provider_id,
                        DirectoryAccountGroup.group_id.in_(department_ids),
                    )
                ),
            )
        )

    query = select(
        OrgMember.id.label("om_id"),
        OrgMember.user_id.label("user_id"),
        row_number,
    ).outerjoin(IdentityProvider, IdentityProvider.id == OrgMember.provider_id)
    if relationship_counts is not None:
        query = query.outerjoin(
            relationship_counts,
            relationship_counts.c.member_id == OrgMember.id,
        )
    return query.where(*conditions).subquery()


def _serialize_picker_department(
    department: OrgDepartment,
    *,
    provider: IdentityProvider | None,
    child_counts: dict[uuid.UUID, int],
    member_counts: dict[uuid.UUID, int],
    hidden_parent_ids: set[uuid.UUID],
) -> dict:
    return {
        "id": str(department.id),
        "name": department.name,
        "parent_id": (
            str(department.parent_id)
            if department.parent_id and department.parent_id not in hidden_parent_ids
            else None
        ),
        "provider_id": str(department.provider_id) if department.provider_id else None,
        "provider_name": provider.name if provider else None,
        "provider_type": provider.provider_type if provider else None,
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

    standard_provider_ids = await platform_standard_provider_ids(
        db,
        tenant_id=tenant_id,
    )
    conditions = [
        OrgDepartment.tenant_id == tenant_id,
        OrgDepartment.status == "active",
        OrgDepartment.member_count > 0,
        ~virtual_root_condition(),
    ]
    if standard_provider_ids is not None:
        conditions.append(OrgDepartment.provider_id.in_(standard_provider_ids))
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
            select(OrgDepartment)
            .outerjoin(IdentityProvider, IdentityProvider.id == OrgDepartment.provider_id)
            .where(
                OrgDepartment.id == parent_id,
                OrgDepartment.tenant_id == tenant_id,
                OrgDepartment.status == "active",
                OrgDepartment.member_count > 0,
                ~virtual_root_condition(),
                or_(
                    OrgDepartment.provider_id.is_(None),
                    IdentityProvider.is_active.is_(True),
                ),
            )
        )
        if (
            parent
            and standard_provider_ids is not None
            and parent.provider_id not in standard_provider_ids
        ):
            parent = None
        if not parent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Department not found",
            )
        edges = directory_edges_subquery(
            tenant_id=tenant_id, name="permission_picker_child_edges"
        )
        conditions.append(
            OrgDepartment.id.in_(
                select(edges.c.child_id).where(
                    edges.c.parent_id == parent_id,
                    same_directory_provider(
                        edges.c.provider_id,
                        parent.provider_id,
                    ),
                )
            )
        )
    else:
        incoming = directory_edges_subquery(
            tenant_id=tenant_id, name="permission_picker_incoming_edges"
        )
        visible_parent = aliased(OrgDepartment)
        conditions.append(
            ~exists(
                select(1)
                .select_from(incoming)
                .join(visible_parent, visible_parent.id == incoming.c.parent_id)
                .where(
                    incoming.c.child_id == OrgDepartment.id,
                    visible_parent.status == "active",
                    visible_parent.member_count > 0,
                    ~virtual_root_condition(visible_parent),
                )
            )
        )

    departments = (
        await db.scalars(
            select(OrgDepartment)
            .outerjoin(IdentityProvider, IdentityProvider.id == OrgDepartment.provider_id)
            .where(*conditions)
            .where(
                or_(
                    OrgDepartment.provider_id.is_(None),
                    IdentityProvider.is_active.is_(True),
                )
            )
            .order_by(OrgDepartment.name.asc())
            .limit(limit)
        )
    ).all()
    memberships = directory_memberships_subquery(
        tenant_id=tenant_id, name="permission_picker_memberships"
    )
    my_department = None
    if not normalized_search and parent_id is None:
        my_department = await db.scalar(
            select(OrgDepartment)
            .join(memberships, memberships.c.group_id == OrgDepartment.id)
            .join(OrgMember, OrgMember.id == memberships.c.account_id)
            .outerjoin(IdentityProvider, IdentityProvider.id == OrgDepartment.provider_id)
            .where(
                OrgMember.tenant_id == tenant_id,
                OrgMember.status == "active",
                OrgMember.user_id == current_user_id,
                OrgDepartment.status == "active",
                ~virtual_root_condition(),
                or_(
                    OrgDepartment.provider_id.is_(None),
                    IdentityProvider.is_active.is_(True),
                ),
            )
            .order_by(OrgMember.synced_at.desc())
            .limit(1)
        )

    hidden_root_query = select(OrgDepartment.id).where(
        OrgDepartment.tenant_id == tenant_id,
        OrgDepartment.status == "active",
        virtual_root_condition(),
    )
    if standard_provider_ids is not None:
        hidden_root_query = hidden_root_query.where(
            OrgDepartment.provider_id.in_(standard_provider_ids)
        )
    hidden_root_ids = set((await db.scalars(hidden_root_query)).all())

    target_ids = {department.id for department in departments}
    if my_department:
        target_ids.add(my_department.id)
    child_counts: dict[uuid.UUID, int] = {}
    member_counts: dict[uuid.UUID, int] = {}
    provider_ids = {
        department.provider_id
        for department in [*departments, *([my_department] if my_department else [])]
        if department.provider_id
    }
    providers_by_id = {
        provider.id: provider
        for provider in (
            await db.scalars(select(IdentityProvider).where(IdentityProvider.id.in_(provider_ids)))
        ).all()
    } if provider_ids else {}
    if target_ids:
        visible_edges = directory_edges_subquery(
            tenant_id=tenant_id, name="permission_picker_count_edges"
        )
        visible_child = aliased(OrgDepartment)
        visible_child_conditions = [
            visible_edges.c.parent_id.in_(target_ids),
            visible_child.status == "active",
            visible_child.member_count > 0,
        ]
        if hidden_root_ids:
            visible_child_conditions.append(
                visible_edges.c.child_id.not_in(hidden_root_ids)
            )
        child_counts = {
            row[0]: int(row[1])
            for row in (
                await db.execute(
                    select(visible_edges.c.parent_id, func.count(visible_edges.c.child_id))
                    .join(visible_child, visible_child.id == visible_edges.c.child_id)
                    .where(*visible_child_conditions)
                    .group_by(visible_edges.c.parent_id)
                )
            ).all()
            if row[0]
        }
        member_counts = {
            row[0]: int(row[1])
            for row in (
                await db.execute(
                    select(
                        memberships.c.group_id,
                        func.count(func.distinct(OrgMember.user_id)),
                    )
                    .join(
                        memberships,
                        memberships.c.account_id == OrgMember.id,
                    )
                    .join(User, User.id == OrgMember.user_id)
                    .outerjoin(Identity, Identity.id == User.identity_id)
                    .where(
                        OrgMember.tenant_id == tenant_id,
                        OrgMember.status == "active",
                        memberships.c.group_id.in_(target_ids),
                        User.tenant_id == tenant_id,
                        User.is_active.is_(True),
                        or_(Identity.id.is_(None), Identity.is_active.is_(True)),
                    )
                    .group_by(memberships.c.group_id)
                )
            ).all()
            if row[0]
        }

    return {
        "items": [
            _serialize_picker_department(
                department,
                provider=providers_by_id.get(department.provider_id),
                child_counts=child_counts,
                member_counts=member_counts,
                hidden_parent_ids=hidden_root_ids,
            )
            for department in departments
        ],
        "my_department": (
            _serialize_picker_department(
                my_department,
                provider=providers_by_id.get(my_department.provider_id),
                child_counts=child_counts,
                member_counts=member_counts,
                hidden_parent_ids=hidden_root_ids,
            )
            if my_department
            else None
        ),
}


async def load_directory_identity_summaries(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_ids: list[uuid.UUID] | set[uuid.UUID],
) -> tuple[dict[uuid.UUID, list[dict]], dict[uuid.UUID, list[dict]]]:
    """Load all active directory sources and channel bindings for tenant users."""
    ids = list(dict.fromkeys(user_ids))
    source_map: dict[uuid.UUID, list[dict]] = {user_id: [] for user_id in ids}
    binding_map: dict[uuid.UUID, list[dict]] = {user_id: [] for user_id in ids}
    if not ids:
        return source_map, binding_map

    source_rows = (
        await db.execute(
            select(OrgMember, IdentityProvider)
            .join(IdentityProvider, IdentityProvider.id == OrgMember.provider_id)
            .where(
                OrgMember.tenant_id == tenant_id,
                OrgMember.user_id.in_(ids),
                OrgMember.status == "active",
                IdentityProvider.is_active.is_(True),
            )
            .order_by(IdentityProvider.name, OrgMember.id)
        )
    ).all()
    source_ids = [source.id for source, _provider in source_rows]
    group_map: dict[uuid.UUID, list[str]] = {source_id: [] for source_id in source_ids}
    if source_ids:
        memberships = directory_memberships_subquery(
            tenant_id=tenant_id, name="directory_identity_source_memberships"
        )
        group_rows = (
            await db.execute(
                select(memberships.c.account_id, memberships.c.group_id).where(
                    memberships.c.account_id.in_(source_ids),
                )
            )
        ).all()
        for account_id, group_id in group_rows:
            group_map[account_id].append(str(group_id))
    for source, provider in source_rows:
        source_map[source.user_id].append({
            "member_id": str(source.id),
            "provider_id": str(provider.id),
            "provider_type": str(provider.provider_type),
            "provider_name": provider.name,
            "group_ids": group_map.get(source.id, []),
        })

    binding_rows = (
        await db.execute(
            select(ChannelUserBinding, IdentityProvider)
            .outerjoin(IdentityProvider, IdentityProvider.id == ChannelUserBinding.provider_id)
            .where(
                ChannelUserBinding.tenant_id == tenant_id,
                ChannelUserBinding.user_id.in_(ids),
            )
            .order_by(ChannelUserBinding.channel_type, ChannelUserBinding.id)
        )
    ).all()
    for binding, provider in binding_rows:
        binding_map[binding.user_id].append({
            "provider_id": str(binding.provider_id) if binding.provider_id else None,
            "provider_name": provider.name if provider else None,
            "provider_type": provider.provider_type if provider else None,
            "channel_type": binding.channel_type,
            "installation_scope": binding.installation_scope,
            "id_type": binding.id_type,
        })
    return source_map, binding_map


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
            select(OrgDepartment)
            .outerjoin(IdentityProvider, IdentityProvider.id == OrgDepartment.provider_id)
            .where(
                OrgDepartment.id == department_id,
                OrgDepartment.tenant_id == tenant_id,
                OrgDepartment.status == "active",
                or_(
                    OrgDepartment.provider_id.is_(None),
                    IdentityProvider.is_active.is_(True),
                ),
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
    search_rank = None
    filters = [
        User.tenant_id == tenant_id,
        User.is_active.is_(True),
        or_(Identity.id.is_(None), Identity.is_active.is_(True)),
    ]
    if excluded_user_ids:
        filters.append(User.id.not_in(excluded_user_ids))
    if normalized_search:
        pattern = f"%{normalized_search}%"
        searchable_columns = (
            User.display_name,
            Identity.email,
            Identity.phone,
            profile.name,
            profile.nickname,
            profile.name_translit_full,
            profile.name_translit_initial,
            profile.email,
            profile.phone,
        )
        filters.append(or_(*(column.ilike(pattern) for column in searchable_columns)))
        exact_value = normalized_search.casefold()
        search_rank = case(
            (
                or_(
                    *(func.lower(column) == exact_value for column in searchable_columns)
                ),
                0,
            ),
            else_=1,
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
    ordering = [func.coalesce(profile.name, User.display_name).asc(), User.id.asc()]
    if search_rank is not None:
        ordering.insert(0, search_rank.asc())
    members = (
        await db.execute(
            base_query.order_by(*ordering)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    user_ids = [user.id for user, _member, _identity in members]
    source_map, binding_map = await load_directory_identity_summaries(
        db, tenant_id=tenant_id, user_ids=user_ids
    )
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
                "phone_masked": mask_identity_claim(
                    "phone",
                    (identity.phone if identity else None)
                    or (member.phone if member else None),
                ),
                "directory_sources": source_map.get(user.id, []),
                "channel_bindings": binding_map.get(user.id, []),
            }
            for user, member, identity in members
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": page * page_size < total,
    }
