"""Platform-standard organization tree query helpers."""

import uuid

from sqlalchemy import and_, func, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.identity import IdentityProvider
from app.models.org import (
    DirectoryAccountGroup,
    DirectoryGroupEdge,
    OrgDepartment,
    OrgMember,
)
from app.models.user import Identity, User


def same_directory_provider(left, right):
    """Null-safe provider equality for rows from the same directory tree."""
    left_null = left.is_(None) if hasattr(left, "is_") else left is None
    right_null = right.is_(None) if hasattr(right, "is_") else right is None
    return or_(left == right, and_(left_null, right_null))


def directory_edges_subquery(*, tenant_id: uuid.UUID, name: str):
    """Return normalized and compatibility group edges without duplicates."""
    return union(
        select(
            DirectoryGroupEdge.parent_group_id.label("parent_id"),
            DirectoryGroupEdge.child_group_id.label("child_id"),
            DirectoryGroupEdge.provider_id.label("provider_id"),
        ).where(DirectoryGroupEdge.tenant_id == tenant_id),
        select(
            OrgDepartment.parent_id.label("parent_id"),
            OrgDepartment.id.label("child_id"),
            OrgDepartment.provider_id.label("provider_id"),
        ).where(
            OrgDepartment.tenant_id == tenant_id,
            OrgDepartment.parent_id.is_not(None),
        ),
    ).subquery(name)


def directory_memberships_subquery(*, tenant_id: uuid.UUID, name: str):
    """Return normalized and compatibility account memberships once."""
    return union(
        select(
            DirectoryAccountGroup.account_id.label("account_id"),
            DirectoryAccountGroup.group_id.label("group_id"),
            DirectoryAccountGroup.provider_id.label("provider_id"),
        ).where(DirectoryAccountGroup.tenant_id == tenant_id),
        select(
            OrgMember.id.label("account_id"),
            OrgMember.department_id.label("group_id"),
            OrgMember.provider_id.label("provider_id"),
        ).where(
            OrgMember.tenant_id == tenant_id,
            OrgMember.department_id.is_not(None),
        ),
    ).subquery(name)


def virtual_root_condition(department=OrgDepartment):
    """Match only legacy transport roots, never a real company root."""
    return and_(
        department.parent_id.is_(None),
        func.lower(func.coalesce(department.name, "")) == "root",
        func.lower(func.coalesce(department.external_id, "")).in_(("0", "1", "root")),
    )


def nonempty_directory_groups_cte(
    *,
    tenant_id: uuid.UUID,
    provider_ids: set[uuid.UUID] | None = None,
    name: str,
    require_platform_user: bool = True,
):
    """Return groups containing an active account or a non-empty descendant."""
    memberships = directory_memberships_subquery(
        tenant_id=tenant_id,
        name=f"{name}_memberships",
    )
    seed_filters = [
        OrgDepartment.tenant_id == tenant_id,
        OrgDepartment.status == "active",
        OrgMember.tenant_id == tenant_id,
        OrgMember.status == "active",
        or_(OrgDepartment.provider_id.is_(None), IdentityProvider.is_active.is_(True)),
    ]
    if require_platform_user:
        seed_filters.extend(
            [
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                or_(Identity.id.is_(None), Identity.is_active.is_(True)),
            ]
        )
    if provider_ids is not None:
        seed_filters.append(OrgDepartment.provider_id.in_(provider_ids))

    groups_query = (
        select(
            OrgDepartment.id.label("group_id"),
            OrgDepartment.provider_id.label("provider_id"),
        )
        .select_from(OrgDepartment)
        .join(memberships, memberships.c.group_id == OrgDepartment.id)
        .join(OrgMember, OrgMember.id == memberships.c.account_id)
        .outerjoin(IdentityProvider, IdentityProvider.id == OrgDepartment.provider_id)
    )
    if require_platform_user:
        groups_query = groups_query.join(User, User.id == OrgMember.user_id).outerjoin(
            Identity,
            Identity.id == User.identity_id,
        )
    groups = groups_query.where(*seed_filters).distinct().cte(name=name, recursive=True)
    parent = aliased(OrgDepartment)
    edges = directory_edges_subquery(tenant_id=tenant_id, name=f"{name}_edges")
    groups = groups.union(
        select(parent.id, parent.provider_id)
        .join(edges, edges.c.parent_id == parent.id)
        .join(
            groups,
            and_(
                edges.c.child_id == groups.c.group_id,
                same_directory_provider(edges.c.provider_id, groups.c.provider_id),
            ),
        )
        .where(
            parent.tenant_id == tenant_id,
            parent.status == "active",
        )
    )
    return groups


def _directory_protocol(provider: IdentityProvider) -> str | None:
    config = provider.config or {}
    return (
        ((config.get("capabilities") or {}).get("directory_protocol"))
        or config.get("directory_protocol")
        or ("scim" if provider.provider_type == "scim" else None)
    )


async def platform_standard_provider_ids(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> set[uuid.UUID] | None:
    """Prefer the tenant's standard SCIM directory view over vendor source trees."""
    providers = (
        await db.scalars(
            select(IdentityProvider).where(
                IdentityProvider.tenant_id == tenant_id,
                IdentityProvider.is_active.is_(True),
            )
        )
    ).all()
    scim_provider_ids = {
        provider.id for provider in providers if _directory_protocol(provider) == "scim"
    }
    return scim_provider_ids or None
