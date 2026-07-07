"""Shared contact-to-platform-user provisioning.

This service is intentionally directory/contact scoped. Login registration and
SSO identity binding remain owned by registration_service and sso_service.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.participant import Participant
from app.models.user import Identity, User


_LOCAL_EMAIL_SUFFIX = ".local"


@dataclass(slots=True)
class ContactProvisioningResult:
    user: User | None = None
    user_created: bool = False
    user_linked: bool = False
    tenant_user_matched: bool = False
    global_identity_matched_no_tenant_user: bool = False
    skipped_reason: str | None = None

    @property
    def created_identity_and_user(self) -> bool:
        return self.user_created and not self.global_identity_matched_no_tenant_user

    @property
    def skipped_missing_mobile(self) -> bool:
        return self.skipped_reason == "missing_mobile"

    @property
    def skipped_requires_confirmation(self) -> bool:
        return self.skipped_reason == "skipped_requires_confirmation"


def normalize_mobile(value: str | None) -> str | None:
    """Normalize phone numbers using the project's registration semantics."""
    if not value:
        return None
    normalized = re.sub(r"[\s\-\+]", "", str(value)).strip()
    return normalized or None


def _is_local_email(value: str | None) -> bool:
    return bool(value and value.lower().endswith(_LOCAL_EMAIL_SUFFIX))


def _clean_email(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    return value or None


def _safe_token(value: str | None, fallback: str) -> str:
    raw = (value or fallback or uuid.uuid4().hex).strip()
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", raw)
    return safe[:64] or uuid.uuid4().hex[:12]


class ContactProvisioningService:
    """Provision tenant users from org directory/contact records."""

    async def ensure_user_for_org_member(
        self,
        db: AsyncSession,
        org_member: OrgMember,
        *,
        provider: IdentityProvider | None = None,
    ) -> ContactProvisioningResult:
        tenant_id = org_member.tenant_id
        if tenant_id is None:
            return ContactProvisioningResult(skipped_reason="missing_tenant")

        provider = provider or await self._get_provider(db, org_member.provider_id)
        provider_type = self._provider_type(provider)
        mobile = normalize_mobile(org_member.phone)
        if not mobile:
            return ContactProvisioningResult(skipped_reason="missing_mobile")

        active_user = await self._find_active_tenant_user_by_phone(db, tenant_id, mobile)
        if active_user:
            org_member.user_id = active_user.id
            await self._sync_user_profile(db, active_user, org_member, provider, mobile)
            await self._ensure_participant(db, active_user)
            await db.flush()
            return ContactProvisioningResult(
                user=active_user,
                user_linked=True,
                tenant_user_matched=True,
            )

        if org_member.user_id:
            linked_user = await self._get_tenant_user(db, org_member.user_id, tenant_id)
            if linked_user and linked_user.is_active:
                await self._sync_user_profile(db, linked_user, org_member, provider, mobile)
                await self._ensure_participant(db, linked_user)
                await db.flush()
                return ContactProvisioningResult(user=linked_user, user_linked=True)

        inactive_user = await self._find_inactive_tenant_user_by_phone(db, tenant_id, mobile)
        if inactive_user:
            return ContactProvisioningResult(skipped_reason="skipped_requires_confirmation")

        identity = await self._find_identity_by_phone(db, mobile)
        reused_global_identity = identity is not None
        if not identity:
            identity = await self._create_identity(db, org_member, provider, provider_type, mobile)
        else:
            await self._sync_identity_contact(db, identity, org_member, provider, mobile)

        user = User(
            identity=identity,
            tenant_id=tenant_id,
            display_name=org_member.name or identity.username or "User",
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
            global_identity_matched_no_tenant_user=reused_global_identity,
        )

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

    async def _create_identity(
        self,
        db: AsyncSession,
        org_member: OrgMember,
        provider: IdentityProvider | None,
        provider_type: str,
        mobile: str,
    ) -> Identity:
        email = await self._usable_email_for_new_identity(db, org_member, provider, provider_type)
        username = await self._unique_username(db, self._username_seed(org_member, provider_type))
        identity = Identity(
            email=email,
            phone=mobile,
            username=username,
            password_hash=None,
            is_active=True,
            email_verified=_clean_email(org_member.email) is not None,
        )
        db.add(identity)
        await db.flush()
        return identity

    async def _sync_user_profile(
        self,
        db: AsyncSession,
        user: User,
        org_member: OrgMember,
        provider: IdentityProvider | None,
        mobile: str,
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
        await self._sync_identity_contact(db, user.identity, org_member, provider, mobile)
        await db.flush()

    async def _sync_identity_contact(
        self,
        db: AsyncSession,
        identity: Identity,
        org_member: OrgMember,
        provider: IdentityProvider | None,
        mobile: str,
    ) -> None:
        if identity.phone != mobile and await self._phone_available_for_identity(db, mobile, identity.id):
            identity.phone = mobile

        incoming_email = _clean_email(org_member.email)
        if (
            incoming_email
            and (not identity.email or _is_local_email(identity.email))
            and await self._email_available_for_identity(db, incoming_email, identity.id)
        ):
            identity.email = incoming_email
            identity.email_verified = True
        elif not identity.email:
            identity.email = self._synthetic_email(org_member, provider, self._provider_type(provider))

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

    async def _usable_email_for_new_identity(
        self,
        db: AsyncSession,
        org_member: OrgMember,
        provider: IdentityProvider | None,
        provider_type: str,
    ) -> str:
        incoming_email = _clean_email(org_member.email)
        if incoming_email:
            existing = await db.execute(select(Identity.id).where(Identity.email == incoming_email).limit(1))
            if existing.scalar_one_or_none() is None:
                return incoming_email
        return self._synthetic_email(org_member, provider, provider_type)

    def _synthetic_email(
        self,
        org_member: OrgMember,
        provider: IdentityProvider | None,
        provider_type: str,
    ) -> str:
        provider_token = provider.id.hex if provider and provider.id else "provider"
        external_token = _safe_token(org_member.external_id, org_member.id.hex if org_member.id else uuid.uuid4().hex)
        return f"{provider_type}_{provider_token}_{external_token}@{provider_type}.local"

    def _username_seed(self, org_member: OrgMember, provider_type: str) -> str:
        if org_member.email and "@" in org_member.email:
            return org_member.email.split("@", 1)[0]
        seed = org_member.external_id or org_member.unionid or (org_member.id.hex if org_member.id else uuid.uuid4().hex)
        return f"{provider_type}_{_safe_token(seed, uuid.uuid4().hex)[:24]}"

    async def _unique_username(self, db: AsyncSession, seed: str) -> str:
        username = _safe_token(seed, uuid.uuid4().hex)
        result = await db.execute(select(Identity.id).where(Identity.username == username).limit(1))
        if result.scalar_one_or_none() is None:
            return username
        return f"{username[:80]}_{uuid.uuid4().hex[:6]}"

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

        participant = Participant(
            type="user",
            ref_id=user.id,
            display_name=user.display_name or user.username or "User",
            avatar_url=user.avatar_url,
        )
        db.add(participant)
        await db.flush()
        return participant


contact_provisioning = ContactProvisioningService()
