"""Shared contact-to-platform-user provisioning.

This service is intentionally directory/contact scoped. Login registration and
SSO identity binding remain owned by registration_service and sso_service.
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.participant import Participant
from app.models.user import Identity, User
from app.services.canonical_user_resolver import normalize_email, normalize_phone
from app.services.directory_identity_claims import VerifiedDirectoryClaims


_LOCAL_EMAIL_SUFFIX = ".local"


@dataclass(slots=True)
class ContactProvisioningResult:
    user: User | None = None
    user_created: bool = False
    user_linked: bool = False
    tenant_user_matched: bool = False
    global_identity_matched_no_tenant_user: bool = False
    external_only_user_created: bool = False
    legacy_split_repaired: bool = False
    skipped_reason: str | None = None

    @property
    def created_identity_and_user(self) -> bool:
        return (
            self.user_created
            and not self.external_only_user_created
            and not self.global_identity_matched_no_tenant_user
        )

    @property
    def skipped_missing_mobile(self) -> bool:
        return self.skipped_reason == "missing_mobile"

    @property
    def skipped_requires_confirmation(self) -> bool:
        return self.skipped_reason == "skipped_requires_confirmation"


def normalize_mobile(value: str | None) -> str | None:
    """Normalize phone numbers using the project's registration semantics."""
    return normalize_phone(value)


def _is_local_email(value: str | None) -> bool:
    return bool(value and value.lower().endswith(_LOCAL_EMAIL_SUFFIX))


def _clean_email(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    return value or None


class ContactProvisioningService:
    """Provision tenant users from org directory/contact records."""

    async def ensure_user_for_org_member(
        self,
        db: AsyncSession,
        org_member: OrgMember,
        *,
        provider: IdentityProvider | None = None,
        fresh_claims: VerifiedDirectoryClaims | None = None,
        subject_lock_held: bool = False,
    ) -> ContactProvisioningResult:
        if fresh_claims is not None:
            from app.services.dingtalk_identity_reconciliation import (
                dingtalk_legacy_identity_reconciler,
            )

        if fresh_claims is not None and not subject_lock_held:
            # Every DingTalk entry path uses advisory -> member/User/Identity.
            # Taking this before flush is essential for newly synced members.
            await dingtalk_legacy_identity_reconciler.acquire_subject_lock(
                db,
                tenant_id=fresh_claims.tenant_id,
                provider_id=fresh_claims.provider_id,
                external_id=fresh_claims.external_id,
            )
        # Directory sync and inbound IM can provision the same member at the
        # same time. Serialize on the shared directory row and overwrite any
        # stale ORM state so only one identityless tenant User can be minted.
        await db.flush()
        locked_member = (
            await db.execute(
                select(OrgMember)
                .where(OrgMember.id == org_member.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if locked_member is None:
            return ContactProvisioningResult(skipped_reason="missing_org_member")
        org_member = locked_member

        tenant_id = org_member.tenant_id
        if tenant_id is None:
            return ContactProvisioningResult(skipped_reason="missing_tenant")

        provider = provider or await self._get_provider(db, org_member.provider_id)
        provider_type = self._provider_type(provider)
        # Contact fields are identity evidence only when the provider contract
        # says they came from an authenticated corporate directory. Other
        # channel payloads remain profile data and must not merge people.
        verified_contact = provider_type == "dingtalk" or bool(
            (getattr(provider, "config", None) or {}).get("verified_contact_identity")
        )
        if provider_type == "dingtalk":
            claims_in_scope = bool(
                fresh_claims
                and provider
                and fresh_claims.matches_scope(
                    tenant_id=tenant_id,
                    provider_id=provider.id,
                    external_id=org_member.external_id or "",
                )
            )
            if fresh_claims and fresh_claims.has_alternate_email_conflict:
                from app.services.canonical_user_resolver import CanonicalIdentityConflict

                raise CanonicalIdentityConflict(
                    "DingTalk email and org_email disagree"
                )
            email = fresh_claims.email if claims_in_scope and fresh_claims else None
            mobile = fresh_claims.phone if claims_in_scope and fresh_claims else None
        else:
            mobile = normalize_mobile(org_member.phone) if verified_contact else None
            email = _clean_email(org_member.email) if verified_contact else None

        linked_user = (
            await self._get_tenant_user(db, org_member.user_id, tenant_id)
            if org_member.user_id
            else None
        )

        if linked_user and not linked_user.is_active:
            return ContactProvisioningResult(
                skipped_reason="skipped_requires_confirmation"
            )

        if provider_type == "dingtalk" and fresh_claims and linked_user:
            legacy = await dingtalk_legacy_identity_reconciler.reconcile(
                db,
                provider=provider,
                org_member=org_member,
                claims=fresh_claims,
                apply=bool(
                    (provider.config or {}).get(
                        "auto_repair_legacy_identity_split",
                        False,
                    )
                ),
                lock_already_held=subject_lock_held,
            )
            if legacy.repaired and legacy.user:
                await self._sync_user_profile(
                    db,
                    legacy.user,
                    org_member,
                    provider,
                    mobile,
                    email,
                    verified_contact,
                )
                await self._ensure_participant(db, legacy.user)
                return ContactProvisioningResult(
                    user=legacy.user,
                    user_linked=True,
                    tenant_user_matched=True,
                    legacy_split_repaired=True,
                )
            if legacy.candidate:
                from app.services.canonical_user_resolver import CanonicalUserConflict

                raise CanonicalUserConflict(
                    f"legacy DingTalk identity split requires repair: {legacy.reason or legacy.status}"
                )

        from app.services.canonical_user_resolver import canonical_user_resolver

        claims = await canonical_user_resolver.resolve_identity_claims(
            db,
            email=email,
            phone=mobile,
            enrich=True,
        )
        identity = claims.identity

        if identity:
            identity_user = await canonical_user_resolver.get_tenant_user(
                db,
                tenant_id=tenant_id,
                identity_id=identity.id,
                lock=True,
            )
            if identity_user and not identity_user.is_active:
                return ContactProvisioningResult(
                    skipped_reason="skipped_requires_confirmation"
                )
            resolved_user = await canonical_user_resolver.reconcile_identity_user(
                db,
                tenant_id=tenant_id,
                identity=identity,
                candidate_user=linked_user,
            )
            user_created = False
            if resolved_user is None:
                resolved_user, user_created = (
                    await canonical_user_resolver.get_or_create_tenant_user(
                        db,
                        tenant_id=tenant_id,
                        identity=identity,
                        display_name=org_member.name or identity.username or "User",
                        avatar_url=org_member.avatar_url,
                        registration_source=f"{provider_type}_org_sync",
                    )
                )
            if not resolved_user.is_active:
                return ContactProvisioningResult(
                    skipped_reason="skipped_requires_confirmation"
                )
            org_member.user_id = resolved_user.id
            await self._sync_user_profile(
                db,
                resolved_user,
                org_member,
                provider,
                mobile,
                email,
                verified_contact,
            )
            await self._ensure_participant(db, resolved_user)
            await db.flush()
            return ContactProvisioningResult(
                user=resolved_user,
                user_created=user_created,
                user_linked=True,
                tenant_user_matched=not user_created,
                global_identity_matched_no_tenant_user=user_created,
            )

        if linked_user and linked_user.is_active:
            await self._sync_user_profile(
                db,
                linked_user,
                org_member,
                provider,
                mobile,
                email,
                verified_contact,
            )
            await self._ensure_participant(db, linked_user)
            await db.flush()
            return ContactProvisioningResult(user=linked_user, user_linked=True)

        user = User(
            # Directory/channel-only people are real tenant Users but are not
            # login principals.  Only reuse a pre-existing, verified login
            # Identity; never mint a synthetic username/email/password here.
            identity=None,
            tenant_id=tenant_id,
            display_name=org_member.name or "User",
            avatar_url=org_member.avatar_url,
            title=org_member.title or None,
            role="member",
            source=provider_type,
            registration_source=f"{provider_type}_org_sync",
            is_active=True,
        )
        db.add(user)
        await db.flush()

        org_member.user_id = user.id
        await self._ensure_participant(db, user)
        await db.flush()

        return ContactProvisioningResult(
            user=user,
            user_created=True,
            external_only_user_created=True,
        )

    async def sync_linked_user_profile(
        self,
        db: AsyncSession,
        user: User,
        org_member: OrgMember,
        *,
        provider: IdentityProvider | None = None,
        fresh_claims: VerifiedDirectoryClaims | None = None,
    ) -> None:
        """Refresh one already-resolved canonical User from its directory profile."""
        provider = provider or await self._get_provider(db, org_member.provider_id)
        verified_contact = self._provider_type(provider) == "dingtalk" or bool(
            (getattr(provider, "config", None) or {}).get("verified_contact_identity")
        )
        provider_type = self._provider_type(provider)
        if provider_type == "dingtalk":
            claims_in_scope = bool(
                fresh_claims
                and provider
                and org_member.tenant_id
                and fresh_claims.matches_scope(
                    tenant_id=org_member.tenant_id,
                    provider_id=provider.id,
                    external_id=org_member.external_id or "",
                )
            )
            email = fresh_claims.email if claims_in_scope and fresh_claims else None
            mobile = fresh_claims.phone if claims_in_scope and fresh_claims else None
        else:
            email = _clean_email(org_member.email) if verified_contact else None
            mobile = normalize_mobile(org_member.phone) if verified_contact else None
        await self._sync_user_profile(
            db,
            user,
            org_member,
            provider,
            mobile,
            email,
            verified_contact,
        )
        await self._ensure_participant(db, user)
        await db.flush()

    async def _get_provider(
        self,
        db: AsyncSession,
        provider_id: uuid.UUID | None,
    ) -> IdentityProvider | None:
        if not provider_id:
            return None
        return await db.get(IdentityProvider, provider_id)

    def _provider_type(self, provider: IdentityProvider | None) -> str:
        provider_type = (getattr(provider, "provider_type", None) or "contact").lower()
        return "teams" if provider_type == "microsoft_teams" else provider_type

    async def _get_tenant_user(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> User | None:
        result = await db.execute(
            select(User)
            .where(User.id == user_id, User.tenant_id == tenant_id)
            .options(selectinload(User.identity))
        )
        return result.scalar_one_or_none()

    async def _find_active_tenant_user_by_phone(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        mobile: str,
    ) -> User | None:
        result = await db.execute(
            select(User)
            .join(User.identity)
            .where(
                User.tenant_id == tenant_id,
                User.is_active == True,  # noqa: E712
                Identity.phone == mobile,
            )
            .options(selectinload(User.identity))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _find_inactive_tenant_user_by_phone(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        mobile: str,
    ) -> User | None:
        result = await db.execute(
            select(User)
            .join(User.identity)
            .where(
                User.tenant_id == tenant_id,
                User.is_active == False,  # noqa: E712
                Identity.phone == mobile,
            )
            .options(selectinload(User.identity))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _find_identity_by_phone(
        self,
        db: AsyncSession,
        mobile: str,
    ) -> Identity | None:
        result = await db.execute(select(Identity).where(Identity.phone == mobile).limit(1))
        return result.scalar_one_or_none()

    async def _sync_user_profile(
        self,
        db: AsyncSession,
        user: User,
        org_member: OrgMember,
        provider: IdentityProvider | None,
        mobile: str | None,
        email: str | None,
        verified_contact: bool,
    ) -> None:
        if org_member.name and user.display_name != org_member.name:
            user.display_name = org_member.name
        if org_member.avatar_url and user.avatar_url != org_member.avatar_url:
            user.avatar_url = org_member.avatar_url
        if org_member.title and user.title != org_member.title:
            user.title = org_member.title
        if not user.source or user.source == "web":
            user.source = self._provider_type(provider)
        if not user.registration_source or user.registration_source == "web":
            user.registration_source = f"{self._provider_type(provider)}_org_sync"
        if user.identity is not None:
            await self._sync_identity_contact(
                db,
                user.identity,
                org_member,
                provider,
                mobile,
                email,
                verified_contact,
            )
        await db.flush()

    async def _sync_identity_contact(
        self,
        db: AsyncSession,
        identity: Identity,
        org_member: OrgMember,
        provider: IdentityProvider | None,
        mobile: str | None,
        email: str | None,
        verified_contact: bool,
    ) -> None:
        if mobile and identity.phone != mobile and await self._phone_available_for_identity(db, mobile, identity.id):
            identity.phone = mobile

        incoming_email = normalize_email(email) if verified_contact else None
        if (
            incoming_email
            and (not identity.email or _is_local_email(identity.email))
            and await self._email_available_for_identity(db, incoming_email, identity.id)
        ):
            identity.email = incoming_email
            identity.email_verified = True
    async def _email_available_for_identity(
        self,
        db: AsyncSession,
        email: str,
        identity_id: uuid.UUID,
    ) -> bool:
        result = await db.execute(
            select(Identity.id).where(Identity.email == email, Identity.id != identity_id).limit(1)
        )
        return result.scalar_one_or_none() is None

    async def _phone_available_for_identity(
        self,
        db: AsyncSession,
        mobile: str,
        identity_id: uuid.UUID,
    ) -> bool:
        result = await db.execute(
            select(Identity.id).where(Identity.phone == mobile, Identity.id != identity_id).limit(1)
        )
        return result.scalar_one_or_none() is None

    async def _ensure_participant(self, db: AsyncSession, user: User) -> Participant:
        result = await db.execute(
            select(Participant).where(Participant.type == "user", Participant.ref_id == user.id).limit(1)
        )
        participant = result.scalar_one_or_none()
        if participant:
            if user.display_name and participant.display_name != user.display_name:
                participant.display_name = user.display_name
            if user.avatar_url and participant.avatar_url != user.avatar_url:
                participant.avatar_url = user.avatar_url
            return participant

        display_name = user.display_name or user.username or "User"
        inserted_id = await db.scalar(
            pg_insert(Participant)
            .values(
                id=uuid.uuid4(),
                type="user",
                ref_id=user.id,
                display_name=display_name,
                avatar_url=user.avatar_url,
            )
            .on_conflict_do_nothing(index_elements=["type", "ref_id"])
            .returning(Participant.id)
        )
        if inserted_id is not None:
            participant = await db.get(Participant, inserted_id)
            if participant is None:  # pragma: no cover - insert RETURNING guarantees the row
                raise RuntimeError("Inserted participant could not be reloaded")
            return participant

        # Another request won the exact same canonical participant race.
        participant = (
            await db.execute(
                select(Participant).where(
                    Participant.type == "user",
                    Participant.ref_id == user.id,
                )
            )
        ).scalar_one()
        if user.display_name and participant.display_name != user.display_name:
            participant.display_name = user.display_name
        if user.avatar_url and participant.avatar_url != user.avatar_url:
            participant.avatar_url = user.avatar_url
        return participant


contact_provisioning = ContactProvisioningService()
