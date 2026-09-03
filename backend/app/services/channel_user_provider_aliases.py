"""Provider-scoped channel aliases shared by every transport adapter."""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import IdentityProvider
from app.models.org import ChannelUserBinding
from app.models.user import User


class ChannelUserProviderAliasMethods:
    @staticmethod
    def _provider_alias_scope(provider: IdentityProvider) -> str:
        return f"provider:{provider.id}"

    @staticmethod
    def _provider_alias_conflict_id_type(id_type: str) -> str:
        return f"conflict:{id_type}"

    async def remember_provider_user_alias(
        self,
        db: AsyncSession,
        *,
        provider: IdentityProvider,
        channel_type: str,
        id_type: str,
        subject: str,
        user: User,
    ) -> bool:
        """Attach one provider-level channel identifier to a canonical User."""
        normalized_channel = self._normalize_channel_type(channel_type)
        normalized_id_type = str(id_type or "").strip()
        normalized_subject = str(subject or "").strip()
        provider_type = getattr(
            provider.provider_type, "value", provider.provider_type
        )
        provider_channel = self._normalize_channel_type(str(provider_type or ""))
        if (
            provider.tenant_id is None
            or user.tenant_id != provider.tenant_id
            or provider_channel != normalized_channel
            or not normalized_channel
            or not normalized_id_type
            or len(normalized_id_type) > 40
            or not normalized_subject
        ):
            return False

        scope = self._provider_alias_scope(provider)
        conflict_id_type = self._provider_alias_conflict_id_type(normalized_id_type)
        conflict_query = select(ChannelUserBinding).where(
            ChannelUserBinding.tenant_id == provider.tenant_id,
            ChannelUserBinding.provider_id == provider.id,
            ChannelUserBinding.installation_scope == scope,
            ChannelUserBinding.channel_type == normalized_channel,
            ChannelUserBinding.id_type == conflict_id_type,
            ChannelUserBinding.subject == normalized_subject,
        )
        if (await db.execute(conflict_query)).scalar_one_or_none() is not None:
            return False

        query = select(ChannelUserBinding).where(
            ChannelUserBinding.tenant_id == provider.tenant_id,
            ChannelUserBinding.provider_id == provider.id,
            ChannelUserBinding.installation_scope == scope,
            ChannelUserBinding.channel_type == normalized_channel,
            ChannelUserBinding.id_type == normalized_id_type,
            ChannelUserBinding.subject == normalized_subject,
        )
        existing = (await db.execute(query)).scalar_one_or_none()
        if existing is not None:
            if existing.user_id != user.id:
                existing.id_type = conflict_id_type
                await db.flush()
                logger.error(
                    "[{}] provider alias conflicts with another canonical user; "
                    "persistently disabling resolution",
                    normalized_channel,
                )
                return False
            return True

        try:
            async with db.begin_nested():
                db.add(
                    ChannelUserBinding(
                        tenant_id=provider.tenant_id,
                        provider_id=provider.id,
                        installation_scope=scope,
                        channel_type=normalized_channel,
                        id_type=normalized_id_type,
                        subject=normalized_subject,
                        user_id=user.id,
                    )
                )
                await db.flush()
            return True
        except IntegrityError:
            if (await db.execute(conflict_query)).scalar_one_or_none() is not None:
                return False
            existing = (await db.execute(query)).scalar_one_or_none()
            if existing is not None and existing.user_id == user.id:
                return True
            if existing is not None:
                existing.id_type = conflict_id_type
                await db.flush()
            logger.error(
                "[{}] concurrent provider alias conflict persistently disabled resolution",
                normalized_channel,
            )
            return False

    async def _find_provider_alias_bindings(
        self,
        db: AsyncSession,
        *,
        provider: IdentityProvider,
        channel_type: str,
        subjects: list[tuple[str, str]],
    ) -> list[ChannelUserBinding]:
        """Read provider-stable historical aliases after the exact install misses.

        Installation scopes can change when a bot credential or application ID
        is rotated. The provider remains the upstream identity boundary, so a
        subject that still maps to one canonical user across historical scopes
        is safe to reuse and materialize into the current scope.
        """
        if provider.tenant_id is None or not subjects:
            return []
        return list(
            (
                await db.execute(
                    select(ChannelUserBinding).where(
                        ChannelUserBinding.tenant_id == provider.tenant_id,
                        ChannelUserBinding.provider_id == provider.id,
                        ChannelUserBinding.channel_type
                        == self._normalize_channel_type(channel_type),
                        or_(
                            *(
                                (ChannelUserBinding.id_type == id_type)
                                & (ChannelUserBinding.subject == subject)
                                for id_type, subject in subjects
                            )
                        ),
                    )
                )
            ).scalars().all()
        )

    async def resolve_provider_user_alias(
        self,
        db: AsyncSession,
        *,
        provider: IdentityProvider,
        channel_type: str,
        id_type: str,
        subject: str,
    ) -> User | None:
        """Resolve one provider-level identifier to an active tenant User."""
        normalized_channel = self._normalize_channel_type(channel_type)
        normalized_id_type = str(id_type or "").strip()
        normalized_subject = str(subject or "").strip()
        provider_type = getattr(
            provider.provider_type, "value", provider.provider_type
        )
        provider_channel = self._normalize_channel_type(str(provider_type or ""))
        if (
            provider.tenant_id is None
            or provider_channel != normalized_channel
            or not normalized_channel
            or not normalized_id_type
            or len(normalized_id_type) > 40
            or not normalized_subject
        ):
            return None

        scope = self._provider_alias_scope(provider)
        conflict_id_type = self._provider_alias_conflict_id_type(normalized_id_type)
        conflicted = (
            await db.execute(
                select(ChannelUserBinding.id).where(
                    ChannelUserBinding.tenant_id == provider.tenant_id,
                    ChannelUserBinding.provider_id == provider.id,
                    ChannelUserBinding.installation_scope == scope,
                    ChannelUserBinding.channel_type == normalized_channel,
                    ChannelUserBinding.id_type == conflict_id_type,
                    ChannelUserBinding.subject == normalized_subject,
                )
            )
        ).scalar_one_or_none()
        if conflicted is not None:
            return None

        binding = (
            await db.execute(
                select(ChannelUserBinding).where(
                    ChannelUserBinding.tenant_id == provider.tenant_id,
                    ChannelUserBinding.provider_id == provider.id,
                    ChannelUserBinding.installation_scope == scope,
                    ChannelUserBinding.channel_type == normalized_channel,
                    ChannelUserBinding.id_type == normalized_id_type,
                    ChannelUserBinding.subject == normalized_subject,
                )
            )
        ).scalar_one_or_none()
        if binding is None:
            return None
        user = await self._load_user_with_identity(
            db,
            binding.user_id,
            provider.tenant_id,
        )
        return user if user is not None and user.is_active else None
