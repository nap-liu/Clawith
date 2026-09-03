"""Revalidate current evidence for new and historical identity conflicts."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.audit import AuditLog
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.user import User
from app.services.canonical_user_resolver import canonical_user_resolver
from app.services.provider_identity_policy import (
    identity_claim_digest,
    identity_conflict_evidence,
    identity_match_order,
)


def safe_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def conflict_details_complete(details: dict[str, Any]) -> bool:
    matched_by = details.get("matched_by")
    candidates = details.get("candidate_user_ids")
    return bool(
        safe_uuid(details.get("provider_id"))
        and safe_uuid(details.get("source_member_id"))
        and safe_uuid(details.get("bound_user_id"))
        and isinstance(candidates, dict)
        and safe_uuid(candidates.get(matched_by))
        and safe_uuid(details.get("matched_identity_id"))
        and isinstance(details.get("evidence_fingerprint"), str)
    )


def _matches_historical_log(
    conflict: AuditLog,
    details: dict[str, Any],
    *,
    bound_user: User,
    claims: Any,
) -> bool:
    matched_by = details.get("matched_by")
    if matched_by in {"phone", "email"} and claims.matched_by != matched_by:
        return False
    logged_fields = {
        field
        for field in details.get("conflicting_fields") or []
        if field in {"phone", "email"}
    }
    if logged_fields and not logged_fields.issubset(set(claims.conflicting_fields)):
        return False
    if conflict.action == "identity_match_lower_priority_conflict":
        return bool(claims.conflicting_fields)
    return bool(
        claims.identity
        and bound_user.identity_id
        and claims.identity.id != bound_user.identity_id
    )


async def effective_conflict_details(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conflict: AuditLog,
    lock: bool = False,
) -> dict[str, Any]:
    """Return verified current evidence without rewriting the immutable audit row.

    Older conflict rows predate durable source references. They are actionable only
    when exactly one current provider account bound to the audited tenant user
    reproduces the same conflict under the provider's current match policy.
    """
    details = dict(conflict.details) if isinstance(conflict.details, dict) else {}
    if conflict_details_complete(details):
        return details
    provider_id = safe_uuid(details.get("provider_id"))
    bound_user_id = safe_uuid(details.get("bound_user_id")) or conflict.user_id
    if provider_id is None or bound_user_id is None:
        return details
    provider_stmt = select(IdentityProvider).where(
        IdentityProvider.id == provider_id,
        IdentityProvider.tenant_id == tenant_id,
    )
    user_stmt = (
        select(User)
        .where(User.id == bound_user_id, User.tenant_id == tenant_id)
        .options(selectinload(User.identity))
    )
    member_stmt = select(OrgMember).where(
        OrgMember.tenant_id == tenant_id,
        OrgMember.provider_id == provider_id,
        OrgMember.user_id == bound_user_id,
    )
    if lock:
        provider_stmt = provider_stmt.with_for_update()
        user_stmt = user_stmt.with_for_update()
        member_stmt = member_stmt.with_for_update()
    provider = (await db.execute(provider_stmt)).scalar_one_or_none()
    bound_user = (await db.execute(user_stmt)).scalar_one_or_none()
    members = (await db.execute(member_stmt)).scalars().all()
    if provider is None or bound_user is None:
        return details

    matches: list[tuple[OrgMember, Any]] = []
    for member in members:
        claims = await canonical_user_resolver.resolve_identity_claims(
            db,
            email=member.email,
            phone=member.phone,
            enrich=False,
            ordered_fields=identity_match_order(provider),
        )
        if _matches_historical_log(
            conflict, details, bound_user=bound_user, claims=claims
        ):
            matches.append((member, claims))
    if len(matches) != 1:
        return details

    member, claims = matches[0]
    evidence = await identity_conflict_evidence(
        db, tenant_id=tenant_id, claims=claims
    )
    evidence["claim_value_digests"] = {
        field: digest
        for field in ("phone", "email")
        if (
            digest := identity_claim_digest(
                tenant_id=tenant_id,
                provider_id=provider.id,
                source_member_id=member.id,
                field=field,
                value=getattr(member, field, None),
            )
        )
    }
    return {
        **details,
        "bound_user_id": str(bound_user.id),
        "source_member_id": str(member.id),
        "matched_by": claims.matched_by,
        "conflicting_fields": list(claims.conflicting_fields),
        **evidence,
    }
