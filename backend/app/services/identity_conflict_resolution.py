"""Safe, tenant-scoped repair actions for identity match conflicts."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.audit import AuditLog
from app.models.identity import IdentityProvider
from app.models.org import AgentRelationship, ChannelUserBinding, OrgMember
from app.models.user import User
from app.services.canonical_user_resolver import canonical_user_resolver
from app.services.identity_conflict_context import effective_conflict_details
from app.services.provider_identity_policy import identity_claim_digest, identity_match_order
from app.services.tenant_user_merge import TenantUserMergeError, merge_tenant_users

RESOLUTION_AUDIT_ACTION = "identity_match_conflict_resolved"
SOURCE_ACCOUNT_REFERENCE = "SOURCE_ACCOUNT"
RESOLUTION_ACTIONS = frozenset(
    {
        "rebind_source_to_highest_priority",
        "merge_users",
        "keep_people_separate",
    }
)


class IdentityConflictResolutionError(ValueError):
    """A conflict cannot be repaired without violating an identity invariant."""


@dataclass(slots=True)
class ResolutionResult:
    conflict: AuditLog
    action: str
    changed: bool


def identity_conflict_user_reference(
    tenant_id: uuid.UUID, user_id: uuid.UUID
) -> str:
    material = f"{tenant_id}:{user_id}:identity-conflict"
    return f"USR-{hashlib.sha256(material.encode()).hexdigest()[:8].upper()}"


def _details(log: AuditLog) -> dict[str, Any]:
    return log.details if isinstance(log.details, dict) else {}


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _tenant_clause(tenant_id: uuid.UUID):
    return AuditLog.details["tenant_id"].as_string() == str(tenant_id)


async def _prior_resolution(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conflict_id: uuid.UUID,
) -> AuditLog | None:
    return (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.action == RESOLUTION_AUDIT_ACTION,
                _tenant_clause(tenant_id),
                AuditLog.details["conflict_id"].as_string() == str(conflict_id),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _load_user(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> User | None:
    if user_id is None:
        return None
    return (
        await db.execute(
            select(User)
            .where(User.id == user_id, User.tenant_id == tenant_id)
            .options(selectinload(User.identity))
            .with_for_update()
        )
    ).scalar_one_or_none()


def _member_subjects(member: OrgMember, provider_type: str) -> list[tuple[str, str]]:
    external_type = {
        "dingtalk": "staff_id",
        "feishu": "user_id",
        "wecom": "user_id",
    }.get(provider_type, "external_id")
    candidates = (
        (external_type, member.external_id),
        ("open_id", member.open_id),
        ("union_id", member.unionid),
    )
    return [
        (id_type, str(subject).strip())
        for id_type, subject in candidates
        if str(subject or "").strip()
    ]


async def _current_claims_match_audit(
    db: AsyncSession,
    *,
    provider: IdentityProvider,
    member: OrgMember,
    details: dict[str, Any],
) -> bool:
    matched_identity_id = _uuid(details.get("matched_identity_id"))
    matched_by = details.get("matched_by")
    if matched_identity_id is None or matched_by not in {"phone", "email"}:
        return False
    audited_digests = details.get("claim_value_digests")
    if not isinstance(audited_digests, dict) or any(
        identity_claim_digest(
            tenant_id=provider.tenant_id,
            provider_id=provider.id,
            source_member_id=member.id,
            field=field,
            value=getattr(member, field, None),
        )
        != expected
        for field, expected in audited_digests.items()
        if field in {"phone", "email"}
    ):
        return False
    claims = await canonical_user_resolver.resolve_identity_claims(
        db,
        email=member.email,
        phone=member.phone,
        enrich=False,
        ordered_fields=identity_match_order(provider),
    )
    if claims.identity is None or claims.identity.id != matched_identity_id:
        return False
    audited_candidates = details.get("candidate_identity_ids")
    if not isinstance(audited_candidates, dict):
        return False
    for field, identity_id in audited_candidates.items():
        if field not in {"phone", "email"}:
            continue
        if str(claims.candidate_identity_ids.get(field) or "") != str(identity_id):
            return False
    return True


async def _rebind_channel_subjects(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider: IdentityProvider,
    member: OrgMember,
    previous_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
) -> int:
    subjects = _member_subjects(member, str(provider.provider_type))
    if not subjects:
        return 0
    subject_filter = or_(
        *(
            (ChannelUserBinding.id_type == id_type)
            & (ChannelUserBinding.subject == subject)
            for id_type, subject in subjects
        )
    )
    bindings = (
        await db.execute(
            select(ChannelUserBinding)
            .where(
                ChannelUserBinding.tenant_id == tenant_id,
                ChannelUserBinding.provider_id == provider.id,
                subject_filter,
            )
            .with_for_update()
        )
    ).scalars().all()
    unexpected = {
        binding.user_id
        for binding in bindings
        if binding.user_id not in {previous_user_id, target_user_id}
    }
    if unexpected:
        raise IdentityConflictResolutionError(
            "Source channel subjects point to another tenant user"
        )
    changed = 0
    for binding in bindings:
        if binding.user_id == previous_user_id:
            binding.user_id = target_user_id
            changed += 1
    return changed


async def _rebind_relationships(
    db: AsyncSession,
    *,
    member: OrgMember,
    previous_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
) -> int:
    relationships = (
        await db.execute(
            select(AgentRelationship)
            .where(AgentRelationship.member_id == member.id)
            .with_for_update()
        )
    ).scalars().all()
    changed = 0
    for relationship in relationships:
        if relationship.user_id == target_user_id:
            continue
        if relationship.user_id != previous_user_id:
            raise IdentityConflictResolutionError(
                "Source relationships point to another tenant user"
            )
        duplicate = await db.scalar(
            select(AgentRelationship.id).where(
                AgentRelationship.agent_id == relationship.agent_id,
                AgentRelationship.user_id == target_user_id,
                AgentRelationship.id != relationship.id,
            )
        )
        if duplicate is not None:
            raise IdentityConflictResolutionError(
                "Rebinding would duplicate an existing agent relationship"
            )
        relationship.user_id = target_user_id
        changed += 1
    return changed


async def resolve_identity_conflict(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conflict_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    action: str,
    target_reference: str | None = None,
    field_sources: dict[str, str] | None = None,
) -> ResolutionResult:
    """Execute one explicit repair while holding all affected rows in one transaction."""
    if action not in RESOLUTION_ACTIONS:
        raise IdentityConflictResolutionError("Unsupported identity conflict action")
    conflict = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.id == conflict_id,
                AuditLog.action.in_(
                    (
                        "identity_match_lower_priority_conflict",
                        "provider_subject_contact_conflict",
                    )
                ),
                _tenant_clause(tenant_id),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if conflict is None:
        raise LookupError("Identity conflict not found")

    normalized_request = {
        "action": action,
        "target_reference": target_reference if action == "merge_users" else None,
        "field_sources": dict(sorted((field_sources or {}).items())),
    }
    prior = await _prior_resolution(
        db, tenant_id=tenant_id, conflict_id=conflict_id
    )
    if prior is not None:
        prior_details = _details(prior)
        prior_action = prior_details.get("resolution_action")
        prior_request = prior_details.get("resolution_request")
        if prior_request == normalized_request or (
            prior_request is None
            and prior_action == action
            and action != "merge_users"
            and not field_sources
        ):
            return ResolutionResult(conflict=conflict, action=action, changed=False)
        raise IdentityConflictResolutionError("Identity conflict is already resolved")

    details = await effective_conflict_details(
        db,
        tenant_id=tenant_id,
        conflict=conflict,
        lock=True,
    )
    provider_id = _uuid(details.get("provider_id"))
    member_id = _uuid(details.get("source_member_id"))
    bound_user_id = _uuid(details.get("bound_user_id")) or conflict.user_id
    candidate_user_ids = details.get("candidate_user_ids")
    matched_by = details.get("matched_by")
    highest_priority_user_id = (
        _uuid(candidate_user_ids.get(matched_by))
        if isinstance(candidate_user_ids, dict)
        else None
    )
    candidate_user_ids_set = {
        candidate_id
        for value in (candidate_user_ids or {}).values()
        if (candidate_id := _uuid(value))
    }
    if bound_user_id:
        candidate_user_ids_set.add(bound_user_id)
    if action == "merge_users":
        targets_by_reference = {
            identity_conflict_user_reference(tenant_id, user_id): user_id
            for user_id in candidate_user_ids_set
        }
        target_user_id = targets_by_reference.get(str(target_reference or ""))
        if target_user_id is None:
            raise IdentityConflictResolutionError(
                "The retained user must be one of the verified merge candidates"
            )
        contact_source_user_ids: dict[str, uuid.UUID] = {}
        source_contact_fields: set[str] = set()
        for field, reference in (field_sources or {}).items():
            if field not in {"phone", "email"}:
                raise IdentityConflictResolutionError(
                    "Only phone and email contact fields can be selected"
                )
            if reference == SOURCE_ACCOUNT_REFERENCE:
                source_contact_fields.add(field)
                continue
            source_user_id = targets_by_reference.get(str(reference or ""))
            if source_user_id is None:
                raise IdentityConflictResolutionError(
                    "A selected contact source is not a verified merge candidate"
                )
            contact_source_user_ids[field] = source_user_id
    else:
        target_user_id = highest_priority_user_id
        contact_source_user_ids = {}
        source_contact_fields = set()
    if not all((provider_id, member_id, bound_user_id, target_user_id)):
        raise IdentityConflictResolutionError(
            "This historical conflict lacks verified repair evidence"
        )
    provider = (
        await db.execute(
            select(IdentityProvider)
            .where(
                IdentityProvider.id == provider_id,
                IdentityProvider.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    member = (
        await db.execute(
            select(OrgMember)
            .where(
                OrgMember.id == member_id,
                OrgMember.tenant_id == tenant_id,
                OrgMember.provider_id == provider_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    bound_user = await _load_user(
        db, tenant_id=tenant_id, user_id=bound_user_id
    )
    target_user = await _load_user(
        db, tenant_id=tenant_id, user_id=target_user_id
    )
    if provider is None or member is None or bound_user is None or target_user is None:
        raise IdentityConflictResolutionError("Conflict repair scope is no longer valid")
    if member.user_id not in {bound_user.id, target_user.id}:
        raise IdentityConflictResolutionError("Source account binding changed after review")
    if action == "keep_people_separate" and member.user_id != bound_user.id:
        raise IdentityConflictResolutionError("Source account binding changed after review")
    if not await _current_claims_match_audit(
        db, provider=provider, member=member, details=details
    ):
        raise IdentityConflictResolutionError(
            "Source contact evidence changed; synchronize and review again"
        )
    if not target_user.is_active or not target_user.identity or not target_user.identity.is_active:
        raise IdentityConflictResolutionError("Matched platform user is disabled")

    mutation: dict[str, Any] = {}
    if action == "rebind_source_to_highest_priority":
        if target_user.identity_id != _uuid(details.get("matched_identity_id")):
            raise IdentityConflictResolutionError(
                "Matched tenant user no longer owns the audited platform identity"
            )
        relationship_count = await _rebind_relationships(
            db,
            member=member,
            previous_user_id=bound_user.id,
            target_user_id=target_user.id,
        )
        binding_count = await _rebind_channel_subjects(
            db,
            tenant_id=tenant_id,
            provider=provider,
            member=member,
            previous_user_id=bound_user.id,
            target_user_id=target_user.id,
        )
        member.user_id = target_user.id
        mutation.update(
            channel_bindings_updated=binding_count,
            relationships_updated=relationship_count,
        )
    elif action == "merge_users":
        source_contact_values = {
            field: value
            for field in source_contact_fields
            if (value := getattr(member, field, None))
        }
        if source_contact_fields != set(source_contact_values):
            raise IdentityConflictResolutionError(
                "The selected source contact is no longer available"
            )
        try:
            mutation.update(
                await merge_tenant_users(
                    db,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    target_user_id=target_user.id,
                    implicated_user_ids=candidate_user_ids_set,
                    contact_source_user_ids=contact_source_user_ids,
                    contact_source_values=source_contact_values,
                )
            )
        except TenantUserMergeError as exc:
            raise IdentityConflictResolutionError(str(exc)) from exc
        mutation["target_reference"] = identity_conflict_user_reference(
            tenant_id, target_user.id
        )
    elif action == "keep_people_separate":
        mutation["binding_preserved"] = True

    await db.flush()
    db.add(
        AuditLog(
            user_id=actor_user_id,
            action=RESOLUTION_AUDIT_ACTION,
            details={
                "tenant_id": str(tenant_id),
                "provider_id": str(provider.id),
                "conflict_id": str(conflict.id),
                "source_member_id": str(member.id),
                "previous_user_id": str(bound_user.id),
                "target_user_id": str(target_user.id),
                "resolution_action": action,
                "resolution_request": normalized_request,
                "status": "resolved",
                "evidence_fingerprint": details.get("evidence_fingerprint"),
                **mutation,
            },
        )
    )
    await db.flush()
    return ResolutionResult(conflict=conflict, action=action, changed=True)
