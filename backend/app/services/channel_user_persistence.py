"""Channel user provider and shell persistence methods."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.user import User


class ChannelUserPersistenceMethods:
    async def _ensure_provider(
        self, db: AsyncSession, provider_type: str, tenant_id: uuid.UUID | None
    ) -> IdentityProvider:
        """Get or create IdentityProvider record."""
        canonical_type = self._normalize_channel_type(provider_type)

        query = select(IdentityProvider).where(
            IdentityProvider.provider_type == canonical_type
        )
        if tenant_id:
            query = query.where(IdentityProvider.tenant_id == tenant_id)

        result = await db.execute(query)
        provider = result.scalar_one_or_none()
        if provider:
            return provider

        for legacy_type in self._legacy_provider_types_for_channel(provider_type):
            if legacy_type == canonical_type:
                continue
            legacy_query = select(IdentityProvider).where(
                IdentityProvider.provider_type == legacy_type
            )
            if tenant_id:
                legacy_query = legacy_query.where(IdentityProvider.tenant_id == tenant_id)
            legacy_result = await db.execute(legacy_query)
            legacy_provider = legacy_result.scalar_one_or_none()
            if legacy_provider:
                return legacy_provider

        provider = IdentityProvider(
            provider_type=canonical_type,
            name=canonical_type.capitalize(),
            is_active=True,
            config={},
            tenant_id=tenant_id,
        )
        db.add(provider)
        await db.flush()

        return provider

    async def _create_org_member_shell(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
        linked_user_id: uuid.UUID | None = None,
    ) -> OrgMember:
        """Create a shell OrgMember record for this identity."""
        identity_seed = (
            external_user_id
            or (extra_info.get("open_id") or "").strip()
            or uuid.uuid4().hex
        )
        name = (
            extra_info.get("name")
            or extra_info.get("nickname")
            or f"{channel_type.capitalize()} User {identity_seed[:8]}"
        )
        unionid, open_id, external_id = self._get_channel_ids(channel_type, external_user_id, extra_info)

        member = OrgMember(
            name=name,
            nickname=(extra_info.get("nickname") or "").strip() or None,
            email=extra_info.get("email") if extra_info.get("identity_verified") is True else None,
            provider_id=provider.id,
            user_id=linked_user_id,
            tenant_id=provider.tenant_id,
            external_id=external_id,
            unionid=unionid,
            open_id=open_id,
            avatar_url=extra_info.get("avatar_url"),
            phone=extra_info.get("mobile") if extra_info.get("identity_verified") is True else None,
            title=extra_info.get("title", ""),
            status="active",
        )
        db.add(member)
        await db.flush()
        return member

    async def _find_existing_org_member_for_user(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        provider_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
    ) -> OrgMember | None:
        """Find an existing OrgMember already linked to the given platform User.

        Used before creating a shell record to avoid duplicate OrgMember entries
        when an org-sync-sourced record already exists for the same user.
        """
        query = select(OrgMember).where(
            OrgMember.user_id == user_id,
            OrgMember.provider_id == provider_id,
            OrgMember.status == "active",
        )
        if tenant_id:
            query = query.where(OrgMember.tenant_id == tenant_id)
        result = await db.execute(query.limit(1))
        return result.scalar_one_or_none()

    async def _create_channel_user(
        self,
        db: AsyncSession,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
        tenant_id: uuid.UUID | None,
    ) -> User:
        """Create a non-login tenant User for a channel-only identity."""
        identity_seed = (
            external_user_id
            or (extra_info.get("open_id") or "").strip()
            or uuid.uuid4().hex
        )
        name = (
            extra_info.get("name")
            or extra_info.get("nickname")
            or f"{channel_type.capitalize()} {identity_seed[:8]}"
        )

        user = User(
            identity_id=None,
            display_name=name,
            avatar_url=extra_info.get("avatar_url"),
            role="member",
            source=self._normalize_channel_type(channel_type),
            registration_source=f"{self._normalize_channel_type(channel_type)}_channel",
            tenant_id=tenant_id,
            is_active=True,
        )
        db.add(user)
        await db.flush()
        return user

