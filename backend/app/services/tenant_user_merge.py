"""Tenant-scoped soft merge for administrator-confirmed identity conflicts."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.org import AgentRelationship, ChannelUserBinding, OrgMember
from app.models.user import Identity, User

ROLE_RANK = {
    "member": 0,
    "agent_admin": 1,
    "org_admin": 2,
    "platform_admin": 3,
}


class TenantUserMergeError(ValueError):
    """The requested merge would violate tenant or authorization boundaries."""


async def _merge_relationships(
    db: AsyncSession,
    *,
    source_user_ids: set[uuid.UUID],
    target_user_id: uuid.UUID,
) -> tuple[int, int]:
    relationships = (
        await db.execute(
            select(AgentRelationship)
            .where(AgentRelationship.user_id.in_(source_user_ids))
            .with_for_update()
        )
    ).scalars().all()
    target_agent_ids = set(
        (
            await db.execute(
                select(AgentRelationship.agent_id).where(
                    AgentRelationship.user_id == target_user_id
                )
            )
        ).scalars()
    )
    updated = 0
    deduplicated = 0
    for relationship in relationships:
        if relationship.agent_id in target_agent_ids:
            await db.delete(relationship)
            deduplicated += 1
            continue
        relationship.user_id = target_user_id
        target_agent_ids.add(relationship.agent_id)
        updated += 1
    return updated, deduplicated


async def merge_tenant_users(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    implicated_user_ids: set[uuid.UUID],
    contact_source_user_ids: dict[str, uuid.UUID] | None = None,
    contact_source_values: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Converge current-tenant identity entrances while retaining history.

    The losing tenant memberships remain as inactive rows so historical business
    ownership and cross-tenant identities stay intact. Directory accounts,
    channel subjects, and active agent relationships move to the selected user.
    """
    user_ids = set(implicated_user_ids)
    user_ids.add(target_user_id)
    users = (
        await db.execute(
            select(User)
            .where(User.tenant_id == tenant_id, User.id.in_(user_ids))
            .options(selectinload(User.identity))
            .order_by(User.id)
            .with_for_update()
        )
    ).scalars().all()
    users_by_id = {user.id: user for user in users}
    if set(users_by_id) != user_ids:
        raise TenantUserMergeError("A merge candidate is outside the current tenant")
    target = users_by_id[target_user_id]
    if not target.is_active or target.identity_id is None:
        raise TenantUserMergeError("The selected retained user is not active")
    source_user_ids = user_ids - {target_user_id}
    if not source_user_ids:
        raise TenantUserMergeError("The conflict is already normalized")
    if actor_user_id in source_user_ids:
        raise TenantUserMergeError("You cannot disable your current administrator user")

    contact_sources = contact_source_user_ids or {}
    source_values = contact_source_values or {}
    if (set(contact_sources) | set(source_values)) - {"phone", "email"}:
        raise TenantUserMergeError("Only phone and email contact fields can be selected")
    if set(contact_sources) & set(source_values):
        raise TenantUserMergeError("A contact field can have only one selected source")
    if set(contact_sources.values()) - user_ids:
        raise TenantUserMergeError("A selected contact source is not a merge candidate")

    selected_values: dict[str, str] = {}
    selected_identities: dict[str, Identity | None] = {}
    for field, source_user_id in contact_sources.items():
        source_identity = users_by_id[source_user_id].identity
        if source_identity is None or not getattr(source_identity, field):
            raise TenantUserMergeError(
                f"The selected user does not have a {field} value"
            )
        selected_identities[field] = source_identity
        selected_values[field] = getattr(source_identity, field)

    implicated_identities = {
        user.identity_id for user in users if user.identity_id is not None
    }
    for field, value in source_values.items():
        normalized_value = str(value or "").strip()
        if not normalized_value:
            raise TenantUserMergeError(f"The selected source does not have a {field} value")
        owner = await db.scalar(
            select(Identity).where(getattr(Identity, field) == normalized_value).with_for_update()
        )
        if owner is not None and owner.id not in implicated_identities:
            raise TenantUserMergeError(
                "A selected contact belongs to an identity outside this merge"
            )
        selected_values[field] = normalized_value
        selected_identities[field] = owner

    changing_identity_ids = {
        identity.id
        for field, identity in selected_identities.items()
        if identity is not None
        and getattr(target.identity, field) != selected_values[field]
    }
    if any(
        getattr(target.identity, field) != value
        for field, value in selected_values.items()
    ):
        changing_identity_ids.add(target.identity.id)
    if changing_identity_ids:
        changing_identity_ids.add(target.identity.id)
        shared_identity = await db.scalar(
            select(User.identity_id)
            .where(
                User.identity_id.in_(changing_identity_ids),
                User.is_active.is_(True),
                or_(User.tenant_id != tenant_id, User.tenant_id.is_(None)),
            )
            .limit(1)
        )
        if shared_identity is not None:
            raise TenantUserMergeError(
                "A selected contact belongs to an identity used by another tenant"
            )

    for field, selected_value in selected_values.items():
        source_identity = selected_identities[field]
        if getattr(target.identity, field) == selected_value:
            continue
        setattr(target.identity, field, None)
        if source_identity is not None and source_identity.id != target.identity.id:
            setattr(source_identity, field, None)
        if field == "email":
            target.identity.email_verified = bool(
                source_identity and source_identity.email_verified
            )
            if source_identity is not None and source_identity.id != target.identity.id:
                source_identity.email_verified = False
        await db.flush()
        setattr(target.identity, field, selected_value)

    source_users = [users_by_id[user_id] for user_id in source_user_ids]
    highest_role = max(
        [target.role, *(user.role for user in source_users)],
        key=lambda role: ROLE_RANK.get(role, -1),
    )
    target.role = highest_role

    members = (
        await db.execute(
            select(OrgMember)
            .where(
                OrgMember.tenant_id == tenant_id,
                OrgMember.user_id.in_(source_user_ids),
            )
            .with_for_update()
        )
    ).scalars().all()
    for member in members:
        member.user_id = target_user_id

    bindings = (
        await db.execute(
            select(ChannelUserBinding)
            .where(
                ChannelUserBinding.tenant_id == tenant_id,
                ChannelUserBinding.user_id.in_(source_user_ids),
            )
            .with_for_update()
        )
    ).scalars().all()
    for binding in bindings:
        binding.user_id = target_user_id

    relationship_count, deduplicated_count = await _merge_relationships(
        db,
        source_user_ids=source_user_ids,
        target_user_id=target_user_id,
    )
    for source in source_users:
        source.is_active = False

    return {
        "merged_user_count": len(source_users),
        "directory_accounts_updated": len(members),
        "channel_bindings_updated": len(bindings),
        "relationships_updated": relationship_count,
        "relationships_deduplicated": deduplicated_count,
        "retained_role": highest_role,
        "selected_contact_fields": sorted(selected_values),
    }
