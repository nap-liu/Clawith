"""Shared organization-directory query helpers.

The enterprise directory and permission picker must resolve duplicate synced
profiles in exactly the same way.  Keep the canonical-member ranking here so a
single platform user never appears twice when identity providers overlap.
"""

import uuid

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import aliased

from app.models.agent import AgentPermission
from app.models.org import AgentRelationship, OrgDepartment, OrgMember


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

    query = select(OrgMember.id.label("om_id"), row_number)
    if relationship_counts is not None:
        query = query.outerjoin(
            relationship_counts,
            relationship_counts.c.member_id == OrgMember.id,
        )
    return query.where(*conditions).subquery()
