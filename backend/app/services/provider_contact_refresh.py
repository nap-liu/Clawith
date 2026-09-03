"""Refresh mutable contact claims behind an exact provider subject binding."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.identity import IdentityProvider
from app.models.user import Identity, User
from app.services.canonical_user_resolver import (
    IdentityClaims,
    canonical_user_resolver,
    normalize_email,
    normalize_phone,
)
from app.services.provider_identity_policy import (
    identity_match_order,
    record_identity_match_conflict,
    record_subject_contact_conflict,
)


@dataclass(frozen=True, slots=True)
class ProviderContactRefreshResult:
    changed_fields: tuple[str, ...] = ()
    conflicting_fields: tuple[str, ...] = ()

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicting_fields)


async def refresh_provider_bound_contacts(
    db: AsyncSession,
    *,
    user: User,
    provider: IdentityProvider,
    email: str | None,
    phone: str | None,
    source: str,
    source_member_id: uuid.UUID | None = None,
    verify_email: bool = True,
) -> ProviderContactRefreshResult:
    """Apply mutable contacts to the identity selected by an exact provider ID.

    Provider subjects identify the account. Phone and email are refreshed in
    place when unclaimed. A value already owned by another identity is the only
    case left for review; it never redirects or blocks the bound account.
    """
    if user.identity_id is None:
        return ProviderContactRefreshResult()

    identity = (
        await db.execute(
            select(Identity)
            .where(Identity.id == user.identity_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if identity is None:
        return ProviderContactRefreshResult()

    normalized_email = normalize_email(email)
    normalized_phone = normalize_phone(phone)
    claims = await canonical_user_resolver.resolve_identity_claims(
        db,
        email=normalized_email,
        phone=normalized_phone,
        enrich=False,
        ordered_fields=identity_match_order(provider),
    )
    conflicting_fields = tuple(
        field
        for field, candidate_id in claims.candidate_identity_ids.items()
        if candidate_id != identity.id
    )
    if conflicting_fields:
        conflict_claims = IdentityClaims(
            identity=claims.identity,
            email=claims.email,
            phone=claims.phone,
            matched_by=claims.matched_by,
            conflicting_fields=conflicting_fields,
            candidate_identity_ids=claims.candidate_identity_ids,
        )
        await record_subject_contact_conflict(
            db,
            provider=provider,
            tenant_id=user.tenant_id,
            user_id=user.id,
            claims=conflict_claims,
            source=source,
            source_member_id=source_member_id,
        )

    changed_fields: list[str] = []
    old_email = normalize_email(identity.email)
    old_phone = normalize_phone(identity.phone)
    if (
        normalized_email
        and "email" not in conflicting_fields
        and old_email != normalized_email
    ):
        identity.email = normalized_email
        if verify_email:
            identity.email_verified = True
        changed_fields.append("email")
    if (
        normalized_phone
        and "phone" not in conflicting_fields
        and old_phone != normalized_phone
    ):
        identity.phone = normalized_phone
        changed_fields.append("phone")

    if changed_fields:
        db.add(
            AuditLog(
                user_id=user.id,
                action="provider_subject_contacts_refreshed",
                details={
                    "tenant_id": str(user.tenant_id) if user.tenant_id else None,
                    "provider_id": str(provider.id),
                    "provider_type": provider.provider_type,
                    "source": source,
                    "source_member_id": (
                        str(source_member_id) if source_member_id else None
                    ),
                    "changed_fields": changed_fields,
                },
            )
        )
    user.identity = identity
    await db.flush()
    return ProviderContactRefreshResult(
        changed_fields=tuple(changed_fields),
        conflicting_fields=conflicting_fields,
    )


async def refresh_provider_user_profile(
    db: AsyncSession,
    *,
    user: User,
    user_info: object,
    provider: IdentityProvider,
    authoritative_contacts: bool = False,
    refresh_contacts: bool = True,
    source_member_id: uuid.UUID | None = None,
) -> ProviderContactRefreshResult:
    """Refresh profile data and, for an exact subject, mutable contacts."""
    incoming_name = str(getattr(user_info, "name", "") or "").strip()
    incoming_avatar = str(getattr(user_info, "avatar_url", "") or "").strip()
    if incoming_name and user.display_name != incoming_name:
        user.display_name = incoming_name
    if incoming_avatar and user.avatar_url != incoming_avatar:
        user.avatar_url = incoming_avatar

    if user.identity is None or not refresh_contacts:
        await db.flush()
        return ProviderContactRefreshResult()
    email = getattr(user_info, "email", None)
    phone = getattr(user_info, "mobile", None)
    if authoritative_contacts:
        return await refresh_provider_bound_contacts(
            db,
            user=user,
            provider=provider,
            email=email,
            phone=phone,
            source="sso_login",
            source_member_id=source_member_id,
            verify_email=True,
        )

    claims = await canonical_user_resolver.resolve_identity_claims(
        db,
        email=email,
        phone=phone,
        enrich=True,
        ordered_fields=identity_match_order(provider),
    )
    await record_identity_match_conflict(
        db,
        provider=provider,
        tenant_id=user.tenant_id,
        claims=claims,
        source="sso_login",
        user_id=user.id,
        source_member_id=source_member_id,
    )
    if claims.identity and claims.identity.id != user.identity.id:
        from app.services.canonical_user_resolver import CanonicalIdentityConflict

        raise CanonicalIdentityConflict("OAuth claims resolve to another identity")
    await db.flush()
    return ProviderContactRefreshResult()
