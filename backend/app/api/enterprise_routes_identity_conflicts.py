"""Tenant-scoped identity conflict evidence and repair endpoints."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.enterprise_api_shared import router
from app.core.security import get_current_admin
from app.database import get_db
from app.models.audit import AuditLog
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.user import User
from app.services.identity_conflict_resolution import (
    RESOLUTION_AUDIT_ACTION,
    IdentityConflictResolutionError,
    identity_conflict_user_reference,
    resolve_identity_conflict,
)
from app.services.identity_conflict_context import effective_conflict_details
from app.services.provider_identity_policy import (
    identity_claim_digest,
)


CONFLICT_ACTIONS = (
    "identity_match_lower_priority_conflict",
    "provider_subject_contact_conflict",
)
REVIEW_ACTION = "identity_match_conflict_reviewed"
KNOWN_SOURCES = frozenset(
    {"directory_contact", "sso_login", "im_inbound", "account_registration"}
)
KNOWN_FIELDS = frozenset({"phone", "email"})
KNOWN_OUTCOMES = frozenset(
    {
        "source_correction_requested",
        "confirmed_distinct_people",
        "manual_identity_repair_required",
    }
)

ReviewStatus = Literal["pending", "reviewing", "reviewed", "resolved"]
ReviewOutcome = Literal[
    "source_correction_requested",
    "confirmed_distinct_people",
    "manual_identity_repair_required",
]
ResolutionAction = Literal[
    "rebind_source_to_highest_priority",
    "merge_users",
    "keep_people_separate",
]


class ConflictPlatformUser(BaseModel):
    reference: str
    display_name: str
    avatar_url: str | None
    title: str | None
    phone: str | None
    email: str | None
    is_active: bool


class ConflictFieldEvidence(BaseModel):
    field: Literal["phone", "email"]
    source_value: str
    candidate_user: ConflictPlatformUser | None
    is_highest_priority: bool
    is_conflicting: bool


class IdentityConflictItem(BaseModel):
    id: uuid.UUID
    provider_name: str | None
    source: str
    masked_identifier: str
    reason: str
    matched_by: str | None
    conflicting_fields: list[str]
    recommended_action: str
    status: ReviewStatus
    outcome: ReviewOutcome | None
    resolution_action: ResolutionAction | None
    bound_user: ConflictPlatformUser | None
    merge_candidates: list[ConflictPlatformUser]
    evidence: list[ConflictFieldEvidence]
    allowed_actions: list[ResolutionAction]
    repair_unavailable_reason: str | None
    evidence_changed: bool
    created_at: datetime


class IdentityConflictList(BaseModel):
    items: list[IdentityConflictItem]
    total: int


class IdentityConflictReview(BaseModel):
    status: Literal["reviewing", "reviewed"]
    outcome: ReviewOutcome | None = None

    @model_validator(mode="after")
    def require_review_outcome(self):
        if self.status == "reviewed" and self.outcome is None:
            raise ValueError("A reviewed conflict requires an outcome")
        if self.status == "reviewing" and self.outcome is not None:
            raise ValueError("A reviewing conflict cannot have an outcome")
        return self


class IdentityConflictResolution(BaseModel):
    action: ResolutionAction
    target_reference: str | None = None
    field_sources: dict[Literal["phone", "email"], str] | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.action == "merge_users" and not self.target_reference:
            raise ValueError("A merge requires the retained user")
        if self.action != "merge_users" and self.target_reference:
            raise ValueError("A retained user is only valid for merge")
        if self.action != "merge_users" and self.field_sources:
            raise ValueError("Contact sources are only valid for merge")
        return self


def _effective_tenant_id(current_user: User, requested: uuid.UUID | None) -> uuid.UUID:
    own_tenant_id = current_user.tenant_id
    if own_tenant_id is not None:
        if requested is not None and requested != own_tenant_id:
            raise HTTPException(status_code=403, detail="Cannot access another tenant's conflicts")
        return own_tenant_id
    if current_user.role != "platform_admin":
        raise HTTPException(status_code=403, detail="Tenant context is required")
    if requested is None:
        raise HTTPException(status_code=400, detail="tenant_id is required")
    return requested


def _tenant_detail_clause(tenant_id: uuid.UUID):
    return AuditLog.details["tenant_id"].as_string() == str(tenant_id)


def _safe_details(log: AuditLog) -> dict:
    return log.details if isinstance(log.details, dict) else {}


def _safe_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _safe_source(details: dict) -> str:
    source = str(details.get("source") or "unknown")
    return source if source in KNOWN_SOURCES else "unknown"


def _masked_identifier(log: AuditLog, tenant_id: uuid.UUID) -> str:
    details = _safe_details(log)
    subject = log.user_id or log.id
    material = ":".join(
        (str(tenant_id), str(details.get("provider_id") or ""), str(subject))
    )
    return f"••••{hashlib.sha256(material.encode()).hexdigest()[:8]}"


def _recommended_action(source: str, action: str) -> str:
    if action == "provider_subject_contact_conflict":
        return "choose_binding_owner"
    return {
        "directory_contact": "correct_source_and_resync",
        "sso_login": "verify_login_claim_mapping",
        "im_inbound": "verify_channel_binding",
        "account_registration": "verify_registration_claims",
    }.get(source, "inspect_identity_evidence")


async def _load_states(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conflict_ids: list[uuid.UUID],
) -> dict[str, tuple[ReviewStatus, ReviewOutcome | None, ResolutionAction | None]]:
    if not conflict_ids:
        return {}
    rows = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.action.in_((REVIEW_ACTION, RESOLUTION_AUDIT_ACTION)),
                _tenant_detail_clause(tenant_id),
                AuditLog.details["conflict_id"].as_string().in_(
                    [str(item) for item in conflict_ids]
                ),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        )
    ).scalars().all()
    states = {}
    for row in rows:
        details = _safe_details(row)
        conflict_id = str(details.get("conflict_id") or "")
        resolution = details.get("resolution_action")
        if row.action == RESOLUTION_AUDIT_ACTION and resolution in {
            "rebind_source_to_highest_priority",
            "merge_users",
            "keep_people_separate",
        }:
            states[conflict_id] = ("resolved", None, resolution)
            continue
        if conflict_id in states:
            continue
        status = details.get("status")
        outcome = details.get("outcome")
        if status in {"reviewing", "reviewed"}:
            states[conflict_id] = (
                status,
                outcome if outcome in KNOWN_OUTCOMES else None,
                None,
            )
    return states


async def _provider_names(
    db: AsyncSession, *, tenant_id: uuid.UUID, logs: list[AuditLog]
) -> dict[str, str]:
    provider_ids = {
        provider_id
        for log in logs
        if (provider_id := _safe_uuid(_safe_details(log).get("provider_id")))
    }
    if not provider_ids:
        return {}
    rows = (
        await db.execute(
            select(IdentityProvider.id, IdentityProvider.name).where(
                IdentityProvider.tenant_id == tenant_id,
                IdentityProvider.id.in_(provider_ids),
            )
        )
    ).all()
    return {str(provider_id): name for provider_id, name in rows}


async def _tenant_users(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    logs: list[AuditLog],
    details_by_id: dict[uuid.UUID, dict],
) -> dict[uuid.UUID, User]:
    user_ids: set[uuid.UUID] = set()
    for log in logs:
        if log.user_id:
            user_ids.add(log.user_id)
        details = details_by_id[log.id]
        if user_id := _safe_uuid(details.get("bound_user_id")):
            user_ids.add(user_id)
        for value in (details.get("candidate_user_ids") or {}).values():
            if user_id := _safe_uuid(value):
                user_ids.add(user_id)
    if not user_ids:
        return {}
    users = (
        await db.execute(
            select(User)
            .where(User.tenant_id == tenant_id, User.id.in_(user_ids))
            .options(selectinload(User.identity))
        )
    ).scalars().all()
    return {user.id: user for user in users}


async def _source_members(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    logs: list[AuditLog],
    details_by_id: dict[uuid.UUID, dict],
) -> dict[uuid.UUID, OrgMember]:
    member_ids = {
        member_id
        for log in logs
        if (member_id := _safe_uuid(details_by_id[log.id].get("source_member_id")))
    }
    if not member_ids:
        return {}
    members = (
        await db.execute(
            select(OrgMember).where(
                OrgMember.tenant_id == tenant_id,
                OrgMember.id.in_(member_ids),
            )
        )
    ).scalars().all()
    return {member.id: member for member in members}


def _user_summary(user: User | None, tenant_id: uuid.UUID) -> ConflictPlatformUser | None:
    if user is None:
        return None
    identity = user.identity
    return ConflictPlatformUser(
        reference=identity_conflict_user_reference(tenant_id, user.id),
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        title=user.title,
        phone=identity.phone if identity else None,
        email=identity.email if identity else None,
        is_active=bool(user.is_active and identity and identity.is_active),
    )


def _repair_actions(
    log: AuditLog,
    details: dict,
    *,
    member: OrgMember | None,
    evidence_changed: bool,
) -> tuple[list[ResolutionAction], str | None]:
    candidate_users = details.get("candidate_user_ids")
    claim_values = details.get("claim_values_masked")
    matched_by = details.get("matched_by")
    complete = bool(
        _safe_uuid(details.get("provider_id"))
        and _safe_uuid(details.get("source_member_id"))
        and (_safe_uuid(details.get("bound_user_id")) or log.user_id)
        and isinstance(candidate_users, dict)
        and _safe_uuid(candidate_users.get(matched_by))
        and isinstance(claim_values, dict)
        and claim_values.get(matched_by)
        and _safe_uuid(details.get("matched_identity_id"))
        and isinstance(details.get("evidence_fingerprint"), str)
    )
    if not complete:
        return [], "missing_verified_evidence"
    if member is None:
        return [], "invalid_source_scope"
    if evidence_changed:
        return [], "evidence_changed"
    return [
        "rebind_source_to_highest_priority",
        "merge_users",
        "keep_people_separate",
    ], None


async def _serialize_conflicts(
    db: AsyncSession, *, tenant_id: uuid.UUID, logs: list[AuditLog]
) -> list[IdentityConflictItem]:
    states = await _load_states(
        db, tenant_id=tenant_id, conflict_ids=[log.id for log in logs]
    )
    details_by_id = {
        log.id: await effective_conflict_details(
            db,
            tenant_id=tenant_id,
            conflict=log,
        )
        for log in logs
    }
    provider_names = await _provider_names(db, tenant_id=tenant_id, logs=logs)
    users = await _tenant_users(
        db,
        tenant_id=tenant_id,
        logs=logs,
        details_by_id=details_by_id,
    )
    members = await _source_members(
        db,
        tenant_id=tenant_id,
        logs=logs,
        details_by_id=details_by_id,
    )
    items: list[IdentityConflictItem] = []
    for log in logs:
        details = details_by_id[log.id]
        source = _safe_source(details)
        raw_fields = details.get("conflicting_fields")
        fields = (
            [field for field in raw_fields if field in KNOWN_FIELDS]
            if isinstance(raw_fields, list)
            else []
        )
        matched_by = details.get("matched_by")
        if matched_by not in KNOWN_FIELDS:
            matched_by = None
        candidate_user_ids = details.get("candidate_user_ids") or {}
        claim_values = details.get("claim_values_masked") or {}
        claim_digests = details.get("claim_value_digests") or {}
        member_id = _safe_uuid(details.get("source_member_id"))
        provider_id = _safe_uuid(details.get("provider_id"))
        member = members.get(member_id)
        if member is not None and member.provider_id != provider_id:
            member = None
        evidence_changed = False
        if member is not None and isinstance(claim_digests, dict):
            evidence_changed = any(
                identity_claim_digest(
                    tenant_id=tenant_id,
                    provider_id=provider_id,
                    source_member_id=member.id,
                    field=field,
                    value=getattr(member, field, None),
                )
                != expected
                for field, expected in claim_digests.items()
                if field in KNOWN_FIELDS
            )
        field_order = list(dict.fromkeys([matched_by, *fields, *claim_values.keys()]))
        evidence = []
        for field in field_order:
            current_value = (
                getattr(member, field, None)
                if member is not None
                else claim_values.get(field)
            )
            if field not in KNOWN_FIELDS or not isinstance(current_value, str):
                continue
            candidate_id = _safe_uuid(candidate_user_ids.get(field))
            evidence.append(
                ConflictFieldEvidence(
                    field=field,
                    source_value=current_value,
                    candidate_user=_user_summary(users.get(candidate_id), tenant_id),
                    is_highest_priority=field == matched_by,
                    is_conflicting=field in fields,
                )
            )
        status, outcome, resolution = states.get(
            str(log.id), ("pending", None, None)
        )
        merge_user_ids = {
            user_id
            for value in candidate_user_ids.values()
            if (user_id := _safe_uuid(value))
        }
        if bound_id := _safe_uuid(details.get("bound_user_id")) or log.user_id:
            merge_user_ids.add(bound_id)
        allowed_actions, unavailable = _repair_actions(
            log,
            details,
            member=member,
            evidence_changed=evidence_changed,
        )
        if status == "resolved":
            allowed_actions = []
            unavailable = None
        items.append(
            IdentityConflictItem(
                id=log.id,
                provider_name=provider_names.get(str(details.get("provider_id") or "")),
                source=source,
                masked_identifier=_masked_identifier(log, tenant_id),
                reason=(
                    "bound_subject_contact_mismatch"
                    if log.action == "provider_subject_contact_conflict"
                    else "lower_priority_identity_mismatch"
                ),
                matched_by=matched_by,
                conflicting_fields=fields,
                recommended_action=_recommended_action(source, log.action),
                status=status,
                outcome=outcome,
                resolution_action=resolution,
                bound_user=_user_summary(users.get(bound_id), tenant_id),
                merge_candidates=[
                    summary
                    for user_id in sorted(merge_user_ids, key=str)
                    if (summary := _user_summary(users.get(user_id), tenant_id))
                ],
                evidence=evidence,
                allowed_actions=allowed_actions,
                repair_unavailable_reason=unavailable,
                evidence_changed=evidence_changed,
                created_at=log.created_at,
            )
        )
    return items


async def _tenant_conflict(
    db: AsyncSession, *, tenant_id: uuid.UUID, conflict_id: uuid.UUID
) -> AuditLog:
    conflict = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.id == conflict_id,
                AuditLog.action.in_(CONFLICT_ACTIONS),
                _tenant_detail_clause(tenant_id),
            )
        )
    ).scalar_one_or_none()
    if conflict is None:
        raise HTTPException(status_code=404, detail="Identity conflict not found")
    return conflict


@router.get("/identity-conflicts", response_model=IdentityConflictList)
async def list_identity_conflicts(
    tenant_id: uuid.UUID | None = None,
    provider_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    effective_tenant = _effective_tenant_id(current_user, tenant_id)
    if provider_id is not None:
        provider_exists = await db.scalar(
            select(IdentityProvider.id).where(
                IdentityProvider.id == provider_id,
                IdentityProvider.tenant_id == effective_tenant,
            )
        )
        if provider_exists is None:
            raise HTTPException(status_code=404, detail="Identity provider not found")
    filters = [AuditLog.action.in_(CONFLICT_ACTIONS), _tenant_detail_clause(effective_tenant)]
    if provider_id is not None:
        filters.append(AuditLog.details["provider_id"].as_string() == str(provider_id))
    total = int(await db.scalar(select(func.count(AuditLog.id)).where(*filters)) or 0)
    logs = (
        await db.execute(
            select(AuditLog)
            .where(*filters)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return IdentityConflictList(
        items=await _serialize_conflicts(db, tenant_id=effective_tenant, logs=logs),
        total=total,
    )


@router.post("/identity-conflicts/{conflict_id}/resolve", response_model=IdentityConflictItem)
async def resolve_conflict(
    conflict_id: uuid.UUID,
    data: IdentityConflictResolution,
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    effective_tenant = _effective_tenant_id(current_user, tenant_id)
    try:
        result = await resolve_identity_conflict(
            db,
            tenant_id=effective_tenant,
            conflict_id=conflict_id,
            actor_user_id=current_user.id,
            action=data.action,
            target_reference=data.target_reference,
            field_sources=data.field_sources,
        )
        await db.commit()
    except LookupError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IdentityConflictResolutionError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Conflict repair raced with another account update",
        ) from exc
    return (
        await _serialize_conflicts(db, tenant_id=effective_tenant, logs=[result.conflict])
    )[0]


@router.post("/identity-conflicts/{conflict_id}/review", response_model=IdentityConflictItem)
async def review_identity_conflict(
    conflict_id: uuid.UUID,
    data: IdentityConflictReview,
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    effective_tenant = _effective_tenant_id(current_user, tenant_id)
    conflict = await _tenant_conflict(
        db, tenant_id=effective_tenant, conflict_id=conflict_id
    )
    db.add(
        AuditLog(
            user_id=current_user.id,
            action=REVIEW_ACTION,
            details={
                "tenant_id": str(effective_tenant),
                "conflict_id": str(conflict_id),
                "status": data.status,
                "outcome": data.outcome,
            },
        )
    )
    await db.commit()
    return (await _serialize_conflicts(db, tenant_id=effective_tenant, logs=[conflict]))[0]
