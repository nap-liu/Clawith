"""Fresh corporate-directory claims used for identity reconciliation.

Persisted ``OrgMember`` contact fields are profile data.  They may be stale or
may have been retained when a provider stopped returning a protected field, so
identity matching must use only the values observed in the current provider
response.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from app.services.canonical_user_resolver import normalize_email, normalize_phone


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedDirectoryClaims:
    """One short-lived set of raw claims from an authenticated directory call."""

    tenant_id: uuid.UUID
    provider_id: uuid.UUID
    external_id: str
    observed_at: datetime
    raw_email: str | None = None
    raw_org_email: str | None = None
    raw_mobile: str | None = None
    source: str = "directory"

    @classmethod
    def from_dingtalk_payload(
        cls,
        *,
        tenant_id: uuid.UUID,
        provider_id: uuid.UUID,
        external_id: str,
        payload: dict,
        source: str,
        observed_at: datetime | None = None,
    ) -> "VerifiedDirectoryClaims":
        return cls(
            tenant_id=tenant_id,
            provider_id=provider_id,
            external_id=str(external_id or "").strip(),
            observed_at=observed_at or datetime.now(timezone.utc),
            raw_email=payload.get("email"),
            raw_org_email=payload.get("org_email"),
            raw_mobile=payload.get("mobile"),
            source=source,
        )

    @property
    def email(self) -> str | None:
        """Return the corporate identity email, or ``None`` when ambiguous."""
        email = normalize_email(self.raw_email)
        org_email = normalize_email(self.raw_org_email)
        if email and org_email and email != org_email:
            return None
        return org_email or email

    @property
    def phone(self) -> str | None:
        return normalize_phone(self.raw_mobile)

    @property
    def has_alternate_email_conflict(self) -> bool:
        email = normalize_email(self.raw_email)
        org_email = normalize_email(self.raw_org_email)
        return bool(email and org_email and email != org_email)

    @property
    def has_identity_evidence(self) -> bool:
        return bool(self.email or self.phone)

    def matches_scope(
        self,
        *,
        tenant_id: uuid.UUID,
        provider_id: uuid.UUID,
        external_id: str,
    ) -> bool:
        return (
            self.tenant_id == tenant_id
            and self.provider_id == provider_id
            and self.external_id == str(external_id or "").strip()
            and bool(self.external_id)
        )
