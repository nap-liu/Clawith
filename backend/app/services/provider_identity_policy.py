"""Provider-scoped identity matching policy.

Only the exact contact evaluation order is configurable. Normalization and
conflict handling remain platform invariants so every adapter makes the same
decision for the same provider configuration.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.user import User


DEFAULT_IDENTITY_MATCH_ORDER = ("phone", "email")
SUPPORTED_IDENTITY_MATCH_FIELDS = frozenset(DEFAULT_IDENTITY_MATCH_ORDER)


def normalize_identity_match_order(fields: Any) -> tuple[str, ...]:
    """Validate and canonicalize one ordered contact-field subset."""
    if not isinstance(fields, (list, tuple)):
        raise ValueError("identity_match_policy.ordered_fields must be an array")
    normalized = tuple(
        field.strip().lower() if isinstance(field, str) else ""
        for field in fields
    )
    if (
        not normalized
        or any(not field for field in normalized)
        or len(normalized) != len(set(normalized))
        or not set(normalized).issubset(SUPPORTED_IDENTITY_MATCH_FIELDS)
    ):
        raise ValueError(
            "identity_match_policy.ordered_fields must contain unique "
            "non-empty supported fields"
        )
    return normalized


def identity_match_order(source: Any = None) -> tuple[str, ...]:
    """Return the validated match order from a provider or config mapping."""
    config = getattr(source, "config", source)
    if not isinstance(config, Mapping):
        return DEFAULT_IDENTITY_MATCH_ORDER
    policy = config.get("identity_match_policy")
    if policy is None:
        return DEFAULT_IDENTITY_MATCH_ORDER
    if not isinstance(policy, Mapping):
        raise ValueError("identity_match_policy must be an object")
    fields = policy.get("ordered_fields", DEFAULT_IDENTITY_MATCH_ORDER)
    return normalize_identity_match_order(fields)


def validate_identity_match_policy(config: Mapping[str, Any]) -> None:
    """Reject provider policies the shared resolver cannot honor safely."""
    order = identity_match_order(config)
    policy = config.get("identity_match_policy")
    if not isinstance(policy, Mapping):
        return
    if policy.get("version", 1) != 1:
        raise ValueError("identity_match_policy.version must be 1")
    if policy.get("match_mode", "normalized_exact") != "normalized_exact":
        raise ValueError("identity_match_policy.match_mode must be normalized_exact")
    conflict_mode = policy.get(
        "on_lower_priority_conflict", "bind_highest_priority_and_flag"
    )
    if conflict_mode != "bind_highest_priority_and_flag":
        raise ValueError(
            "identity_match_policy.on_lower_priority_conflict is not supported"
        )
    assert order


def with_identity_match_policy(
    config: Mapping[str, Any] | None,
    ordered_fields: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """Return a config copy carrying the canonical versioned policy shape."""
    candidate = dict(config or {})
    normalized = normalize_identity_match_order(ordered_fields)
    candidate["identity_match_policy"] = {
        "version": 1,
        "ordered_fields": list(normalized),
        "match_mode": "normalized_exact",
        "on_lower_priority_conflict": "bind_highest_priority_and_flag",
        "allow_name_match": False,
    }
    validate_identity_match_policy(candidate)
    return candidate


def mask_identity_claim(field: str, value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if field == "phone":
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) >= 8:
            return f"{digits[:3]}****{digits[-4:]}"
        return f"{digits[:1]}***{digits[-1:]}" if digits else None
    if field == "email" and "@" in value:
        local, domain = value.rsplit("@", 1)
        visible = local[:2] if len(local) > 1 else local[:1]
        return f"{visible}***@{domain.lower()}"
    return None


def identity_claim_digest(
    *,
    tenant_id: Any,
    provider_id: Any,
    source_member_id: Any,
    field: str,
    value: str | None,
) -> str | None:
    normalized = str(value or "").strip()
    if field == "email":
        normalized = normalized.lower()
    elif field == "phone":
        normalized = "".join(
            character for character in normalized if character.isdigit()
        )
    if not normalized or field not in SUPPORTED_IDENTITY_MATCH_FIELDS:
        return None
    material = "\0".join(
        (
            str(tenant_id),
            str(provider_id),
            str(source_member_id),
            field,
            normalized,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


async def identity_conflict_evidence(
    db: Any, *, tenant_id: Any, claims: Any
) -> dict[str, Any]:
    """Build the tenant-scoped references used to revalidate a conflict."""
    candidate_identity_ids = {
        field: identity_id
        for field, identity_id in (
            getattr(claims, "candidate_identity_ids", {}) or {}
        ).items()
        if field in SUPPORTED_IDENTITY_MATCH_FIELDS and identity_id
    }
    candidate_user_ids: dict[str, str] = {}
    if tenant_id and candidate_identity_ids:
        rows = (
            await db.execute(
                select(User.id, User.identity_id).where(
                    User.tenant_id == tenant_id,
                    User.identity_id.in_(set(candidate_identity_ids.values())),
                )
            )
        ).all()
        users_by_identity = {identity_id: user_id for user_id, identity_id in rows}
        candidate_user_ids = {
            field: str(users_by_identity[identity_id])
            for field, identity_id in candidate_identity_ids.items()
            if identity_id in users_by_identity
        }
    claim_values_masked = {
        field: masked
        for field in SUPPORTED_IDENTITY_MATCH_FIELDS
        if (masked := mask_identity_claim(field, getattr(claims, field, None)))
    }
    fingerprint_payload = {
        "matched_by": getattr(claims, "matched_by", None),
        "claims": {
            field: getattr(claims, field, None)
            for field in sorted(SUPPORTED_IDENTITY_MATCH_FIELDS)
        },
        "candidates": {
            field: str(identity_id)
            for field, identity_id in sorted(candidate_identity_ids.items())
        },
    }
    return {
        "matched_identity_id": (
            str(getattr(getattr(claims, "identity", None), "id", "")) or None
        ),
        "candidate_identity_ids": {
            field: str(identity_id)
            for field, identity_id in candidate_identity_ids.items()
        },
        "candidate_user_ids": candidate_user_ids,
        "claim_values_masked": claim_values_masked,
        "evidence_fingerprint": hashlib.sha256(
            json.dumps(
                fingerprint_payload, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
    }


async def _evidence_is_suppressed(
    db: Any,
    *,
    tenant_id: Any,
    provider_id: Any,
    source_member_id: Any,
    evidence_fingerprint: str,
) -> bool:
    if not all((tenant_id, provider_id, source_member_id, evidence_fingerprint)):
        return False
    return bool(
        await db.scalar(
            select(AuditLog.id)
            .where(
                AuditLog.action == "identity_match_conflict_resolved",
                AuditLog.details["tenant_id"].as_string() == str(tenant_id),
                AuditLog.details["provider_id"].as_string() == str(provider_id),
                AuditLog.details["source_member_id"].as_string()
                == str(source_member_id),
                AuditLog.details["evidence_fingerprint"].as_string()
                == evidence_fingerprint,
                AuditLog.details["resolution_action"].as_string().in_(
                    ("keep_people_separate", "merge_users")
                ),
            )
            .limit(1)
        )
    )


async def record_identity_match_conflict(
    db: Any,
    *,
    provider: Any,
    tenant_id: Any,
    claims: Any,
    source: str,
    user_id: Any = None,
    source_member_id: Any = None,
) -> bool:
    """Persist a PII-free flag when a lower-priority claim points elsewhere."""
    conflicting_fields = tuple(getattr(claims, "conflicting_fields", ()) or ())
    if not conflicting_fields:
        return False
    config = getattr(provider, "config", None) or {}
    policy = config.get("identity_match_policy") or {}
    evidence = await identity_conflict_evidence(
        db, tenant_id=tenant_id, claims=claims
    )
    evidence["claim_value_digests"] = {
        field: digest
        for field in SUPPORTED_IDENTITY_MATCH_FIELDS
        if (
            digest := identity_claim_digest(
                tenant_id=tenant_id,
                provider_id=getattr(provider, "id", None),
                source_member_id=source_member_id,
                field=field,
                value=getattr(claims, field, None),
            )
        )
    }
    if await _evidence_is_suppressed(
        db,
        tenant_id=tenant_id,
        provider_id=getattr(provider, "id", None),
        source_member_id=source_member_id,
        evidence_fingerprint=evidence["evidence_fingerprint"],
    ):
        return False
    db.add(
        AuditLog(
            user_id=user_id,
            action="identity_match_lower_priority_conflict",
            details={
                "tenant_id": str(tenant_id) if tenant_id else None,
                "provider_id": str(getattr(provider, "id", "")) or None,
                "provider_type": str(getattr(provider, "provider_type", "")),
                "source": source,
                "policy_version": policy.get("version", 1),
                "matched_by": getattr(claims, "matched_by", None),
                "conflicting_fields": list(conflicting_fields),
                "bound_user_id": str(user_id) if user_id else None,
                "source_member_id": str(source_member_id) if source_member_id else None,
                **evidence,
            },
        )
    )
    return True


async def record_subject_contact_conflict(
    db: Any,
    *,
    provider: Any,
    tenant_id: Any,
    user_id: Any,
    claims: Any,
    source: str,
    source_member_id: Any = None,
) -> bool:
    """Flag an established provider subject whose contact points elsewhere."""
    evidence = await identity_conflict_evidence(
        db, tenant_id=tenant_id, claims=claims
    )
    evidence["claim_value_digests"] = {
        field: digest
        for field in SUPPORTED_IDENTITY_MATCH_FIELDS
        if (
            digest := identity_claim_digest(
                tenant_id=tenant_id,
                provider_id=getattr(provider, "id", None),
                source_member_id=source_member_id,
                field=field,
                value=getattr(claims, field, None),
            )
        )
    }
    if await _evidence_is_suppressed(
        db,
        tenant_id=tenant_id,
        provider_id=getattr(provider, "id", None),
        source_member_id=source_member_id,
        evidence_fingerprint=evidence["evidence_fingerprint"],
    ):
        return False
    db.add(
        AuditLog(
            user_id=user_id,
            action="provider_subject_contact_conflict",
            details={
                "tenant_id": str(tenant_id),
                "provider_id": str(getattr(provider, "id", "")) or None,
                "provider_type": str(getattr(provider, "provider_type", "")),
                "source": source,
                "matched_by": getattr(claims, "matched_by", None),
                "conflicting_fields": list(
                    getattr(claims, "conflicting_fields", ()) or ()
                ),
                "bound_user_id": str(user_id),
                "source_member_id": str(source_member_id) if source_member_id else None,
                **evidence,
            },
        )
    )
    return True
