"""Exact OAuth subject bindings and authoritative email refreshes."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

from fastapi import HTTPException
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models.audit import AuditLog
from app.models.identity import IdentityProvider
from app.models.org import ChannelUserBinding, OrgMember
from app.models.user import User
from app.services.canonical_user_resolver import (
    CanonicalUserConflict,
    normalize_email,
    normalize_phone,
)


OAUTH_BINDING_CHANNEL = "oauth2"
OAUTH_BINDING_ID_TYPE = "subject"
OAUTH_AUTHORITY_VERSION = "v1"
OAUTH_SUBJECT_POLICY = "custom-user-id-or-sub-userId-id:v1"
OAUTH_EMAIL_POLICY = "custom-email-or-email:v1"
OAUTH_SSO_STATE_KIND = "oauth2_sso"

_EMAIL_ADAPTER = TypeAdapter(EmailStr)


def _authority_payload(provider: IdentityProvider) -> dict[str, str]:
    config = provider.config if isinstance(provider.config, dict) else {}
    field_mapping = config.get("field_mapping") if isinstance(config.get("field_mapping"), dict) else {}
    return {
        "authorize_url": str(config.get("authorize_url") or ""),
        "client_id": str(config.get("client_id") or config.get("app_id") or ""),
        "email_selector": str(field_mapping.get("email") or OAUTH_EMAIL_POLICY),
        "issuer": str(config.get("issuer") or ""),
        "provider_id": str(provider.id),
        "subject_selector": str(field_mapping.get("user_id") or OAUTH_SUBJECT_POLICY),
        "token_url": str(config.get("token_url") or ""),
        "user_info_url": str(config.get("user_info_url") or ""),
        "version": OAUTH_AUTHORITY_VERSION,
    }


def oauth_authority_scope(provider: IdentityProvider) -> str:
    """Return the stable, non-secret namespace for one OAuth identity source."""

    payload = json.dumps(_authority_payload(provider), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"oauth2-authority:{OAUTH_AUTHORITY_VERSION}:{digest}"


def normalize_oauth_subject(subject: str | None) -> str:
    normalized = str(subject or "").strip()
    if not normalized:
        raise HTTPException(status_code=502, detail="OAuth provider returned an invalid subject claim")
    return normalized


def normalize_oauth_email(email: str | None) -> str | None:
    raw = str(email or "").strip()
    if not raw:
        return None
    try:
        validated = str(_EMAIL_ADAPTER.validate_python(raw))
    except ValidationError as exc:
        raise HTTPException(status_code=502, detail="OAuth provider returned an invalid email claim") from exc
    return normalize_email(validated)


def sign_oauth2_sso_state(session_id: uuid.UUID, provider_id: uuid.UUID) -> str:
    payload = f"{OAUTH_SSO_STATE_KIND}:{session_id}:{provider_id}"
    signature = hmac.new(get_settings().SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def parse_oauth2_sso_state(state: str | None) -> tuple[uuid.UUID, uuid.UUID] | None:
    parts = str(state or "").split(":")
    if len(parts) != 4 or parts[0] != OAUTH_SSO_STATE_KIND:
        return None
    payload = ":".join(parts[:-1])
    expected = hmac.new(get_settings().SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(parts[-1], expected):
        return None
    try:
        return uuid.UUID(parts[1]), uuid.UUID(parts[2])
    except ValueError:
        return None


class OAuthIdentityService:
    def validate_enterprise_provider(self, provider: IdentityProvider, tenant_id: uuid.UUID) -> None:
        if (
            provider.provider_type != "oauth2"
            or provider.tenant_id != tenant_id
            or not provider.is_active
            or not provider.sso_login_enabled
        ):
            raise CanonicalUserConflict("OAuth provider is not an active tenant-scoped login provider")

    async def acquire_subject_lock(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider: IdentityProvider,
        subject: str,
    ) -> str:
        self.validate_enterprise_provider(provider, tenant_id)
        normalized_subject = normalize_oauth_subject(subject)
        stable_key = f"oauth2:{tenant_id}:{provider.id}:{oauth_authority_scope(provider)}:{normalized_subject}"
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": stable_key},
        )
        return normalized_subject

    async def resolve_bound_user(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider: IdentityProvider,
        subject: str,
    ) -> User | None:
        self.validate_enterprise_provider(provider, tenant_id)
        normalized_subject = normalize_oauth_subject(subject)
        scope = oauth_authority_scope(provider)
        bindings = (
            await db.execute(
                select(ChannelUserBinding).where(
                    ChannelUserBinding.tenant_id == tenant_id,
                    ChannelUserBinding.provider_id == provider.id,
                    ChannelUserBinding.channel_type == OAUTH_BINDING_CHANNEL,
                    ChannelUserBinding.id_type == OAUTH_BINDING_ID_TYPE,
                    ChannelUserBinding.subject == normalized_subject,
                )
                .order_by(
                    (ChannelUserBinding.installation_scope == scope).desc(),
                    ChannelUserBinding.created_at,
                )
                .with_for_update()
            )
        ).scalars().all()
        if not bindings:
            return None
        user_ids = {binding.user_id for binding in bindings}
        if len(user_ids) != 1:
            raise CanonicalUserConflict(
                "OAuth subject is bound to multiple users for this provider"
            )
        binding = bindings[0]
        user = (
            await db.execute(
                select(User)
                .where(User.id == binding.user_id, User.tenant_id == tenant_id)
                .options(selectinload(User.identity))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if user is None or user.identity_id is None:
            raise CanonicalUserConflict("OAuth subject binding points to an unavailable canonical user")
        return user

    async def ensure_binding(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider: IdentityProvider,
        subject: str,
        user: User,
    ) -> bool:
        self.validate_enterprise_provider(provider, tenant_id)
        normalized_subject = normalize_oauth_subject(subject)
        if user.tenant_id != tenant_id or user.identity_id is None:
            raise CanonicalUserConflict("OAuth subject cannot bind to a non-canonical tenant user")
        scope = oauth_authority_scope(provider)
        existing = await self.resolve_bound_user(
            db,
            tenant_id=tenant_id,
            provider=provider,
            subject=normalized_subject,
        )
        if existing is not None:
            if existing.id != user.id:
                raise CanonicalUserConflict("OAuth subject is already bound to another user")
            return False
        try:
            async with db.begin_nested():
                db.add(
                    ChannelUserBinding(
                        tenant_id=tenant_id,
                        provider_id=provider.id,
                        installation_scope=scope,
                        channel_type=OAUTH_BINDING_CHANNEL,
                        id_type=OAUTH_BINDING_ID_TYPE,
                        subject=normalized_subject,
                        user_id=user.id,
                    )
                )
                await db.flush()
        except IntegrityError as exc:
            winner = await self.resolve_bound_user(
                db,
                tenant_id=tenant_id,
                provider=provider,
                subject=normalized_subject,
            )
            if winner is None or winner.id != user.id:
                raise CanonicalUserConflict("Concurrent OAuth subject binding resolved to another user") from exc
            return False
        return True

    async def refresh_authoritative_contacts(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider: IdentityProvider,
        subject: str,
        user: User,
        email: str | None,
        phone: str | None,
    ) -> tuple[User, bool]:
        """Refresh mutable contacts for one exact OAuth subject binding."""

        self.validate_enterprise_provider(provider, tenant_id)
        normalized_subject = normalize_oauth_subject(subject)
        normalized_email = normalize_oauth_email(email)
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account is disabled")

        bound_user = await self.resolve_bound_user(
            db,
            tenant_id=tenant_id,
            provider=provider,
            subject=normalized_subject,
        )
        if bound_user is None or bound_user.id != user.id:
            raise CanonicalUserConflict("OAuth subject is not bound to this canonical user")

        locked_user = (
            await db.execute(
                select(User)
                .where(User.id == user.id, User.tenant_id == tenant_id)
                .options(selectinload(User.identity))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_user is None or locked_user.identity_id is None:
            raise CanonicalUserConflict("OAuth subject binding lost its canonical user")
        oauth_members = (
            await db.execute(
                select(OrgMember)
                .where(
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == normalized_subject,
                    OrgMember.status == "active",
                )
                .with_for_update()
            )
        ).scalars().all()
        conflicting_user_ids = {
            member.user_id for member in oauth_members if member.user_id is not None and member.user_id != locked_user.id
        }
        if conflicting_user_ids:
            raise CanonicalUserConflict("OAuth subject projection points to another tenant user")
        for member in oauth_members:
            if member.user_id is None:
                member.user_id = locked_user.id

        old_email = normalize_email(locked_user.identity.email)
        old_verified = bool(locked_user.identity.email_verified)
        old_phone = locked_user.identity.phone
        from app.services.provider_contact_refresh import (
            refresh_provider_bound_contacts,
        )

        refreshed = await refresh_provider_bound_contacts(
            db,
            user=locked_user,
            provider=provider,
            email=normalized_email,
            phone=phone,
            source="sso_login",
            source_member_id=oauth_members[0].id if oauth_members else None,
            verify_email=True,
        )
        for member in oauth_members:
            if member.user_id != locked_user.id:
                continue
            if normalized_email and "email" not in refreshed.conflicting_fields:
                member.email = normalized_email
            normalized_phone = normalize_phone(phone)
            if normalized_phone and "phone" not in refreshed.conflicting_fields:
                member.phone = normalized_phone

        from app.services.registration_service import registration_service

        await registration_service.ensure_web_org_member(db, locked_user)
        if "email" in refreshed.changed_fields:
            db.add(
                AuditLog(
                    user_id=locked_user.id,
                    action="oauth_identity_email_refreshed",
                    details={
                        "tenant_id": str(tenant_id),
                        "provider_id": str(provider.id),
                        "identity_id": str(locked_user.identity_id),
                        "subject_sha256": hashlib.sha256(normalized_subject.encode()).hexdigest(),
                        "old_email": old_email,
                        "new_email": normalized_email,
                        "old_email_verified": old_verified,
                        "new_email_verified": True,
                    },
                )
            )
        if "phone" in refreshed.changed_fields:
            db.add(
                AuditLog(
                    user_id=locked_user.id,
                    action="oauth_identity_phone_refreshed",
                    details={
                        "tenant_id": str(tenant_id),
                        "provider_id": str(provider.id),
                        "identity_id": str(locked_user.identity_id),
                        "subject_sha256": hashlib.sha256(normalized_subject.encode()).hexdigest(),
                        "old_phone": old_phone,
                        "new_phone": normalize_phone(phone),
                    },
                )
            )
        await db.flush()
        return locked_user, bool(refreshed.changed_fields)

    async def refresh_authoritative_email(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider: IdentityProvider,
        subject: str,
        user: User,
        email: str | None,
    ) -> tuple[User, bool]:
        """Compatibility wrapper for callers that only refresh OAuth email."""
        return await self.refresh_authoritative_contacts(
            db,
            tenant_id=tenant_id,
            provider=provider,
            subject=subject,
            user=user,
            email=email,
            phone=None,
        )


oauth_identity_service = OAuthIdentityService()
