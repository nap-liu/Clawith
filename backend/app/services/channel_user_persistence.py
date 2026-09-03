"""Channel user provider and shell persistence methods."""

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.user import User
from app.services.channel_user_errors import ChannelUserResolutionError
from app.services.identity_provider_lookup import choose_preferred_identity_provider


class ChannelUserPersistenceMethods:
    async def resolve_channel_provider(
        self,
        db: AsyncSession,
        agent: Any,
        channel_type: str,
        extra_info: dict[str, Any] | None = None,
        provider: IdentityProvider | None = None,
    ) -> tuple[IdentityProvider, dict[str, Any]]:
        """Resolve the exact provider connection configured for an Agent channel."""
        info = dict(extra_info or {})
        scope = await self._resolve_installation_scope(db, agent, channel_type, info)
        info["_installation_scope"] = scope
        if provider is None:
            provider = await self._ensure_provider(
                db,
                channel_type,
                agent.tenant_id,
                provider_id=info.get("_identity_provider_id") or info.get("identity_provider_id"),
                installation_scope=scope,
            )
        if provider.tenant_id != agent.tenant_id or not getattr(
            provider, "is_active", True
        ):
            raise ChannelUserResolutionError(
                "Channel provider is not active in agent tenant"
            )
        return provider, info

    async def _ensure_provider(
        self,
        db: AsyncSession,
        provider_type: str,
        tenant_id: uuid.UUID | None,
        *,
        provider_id: str | uuid.UUID | None = None,
        installation_scope: str | None = None,
    ) -> IdentityProvider:
        """Get or create IdentityProvider record."""
        canonical_type = self._normalize_channel_type(provider_type)
        allowed_types = set(self._legacy_provider_types_for_channel(provider_type))
        if provider_id:
            try:
                exact = await db.get(IdentityProvider, uuid.UUID(str(provider_id)))
            except ValueError as exc:
                raise ChannelUserResolutionError("Invalid channel identity provider") from exc
            exact_type = getattr(getattr(exact, "provider_type", None), "value", getattr(exact, "provider_type", None))
            if (
                exact is None
                or exact.tenant_id != tenant_id
                or str(exact_type) not in allowed_types
                or not exact.is_active
            ):
                raise ChannelUserResolutionError(
                    "Channel identity provider is not active in agent tenant"
                )
            return exact

        query = select(IdentityProvider).where(
            IdentityProvider.provider_type == canonical_type,
            IdentityProvider.is_active.is_(True),
        )
        if tenant_id:
            query = query.where(IdentityProvider.tenant_id == tenant_id)

        result = await db.execute(query.with_for_update())
        candidates = result.scalars().all()
        shared_directory_types = {"dingtalk", "feishu", "wecom"}
        if canonical_type in shared_directory_types:
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise ChannelUserResolutionError(
                    "Multiple enterprise directory providers require an explicit "
                    "channel provider selection"
                )
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
        scoped = [
            candidate
            for candidate in candidates
            if installation_scope
            and str((candidate.config or {}).get("installation_scope") or "").strip()
            == installation_scope
        ]
        if installation_scope and not scoped:
            unscoped = [
                candidate
                for candidate in candidates
                if not str(
                    (candidate.config or {}).get("installation_scope") or ""
                ).strip()
            ]
            if len(unscoped) == 1:
                provider = unscoped[0]
                provider.config = {
                    **(provider.config or {}),
                    "installation_scope": installation_scope,
                }
                await db.flush()
                return provider
            scope_prefix = installation_scope.rsplit(":issuer:", 1)[0]
            rotated = [
                candidate
                for candidate in candidates
                if str(
                    (candidate.config or {}).get("installation_scope") or ""
                ).startswith(f"{scope_prefix}:issuer:")
            ]
            if len(rotated) == 1:
                return rotated[0]
            candidates = []
        provider = choose_preferred_identity_provider(
            scoped or candidates,
            provider_type=canonical_type,
            tenant_id=str(tenant_id) if tenant_id else None,
        )
        if provider:
            return provider

        for legacy_type in self._legacy_provider_types_for_channel(provider_type):
            if legacy_type == canonical_type:
                continue
            legacy_query = select(IdentityProvider).where(
                IdentityProvider.provider_type == legacy_type,
                IdentityProvider.is_active.is_(True),
            )
            if tenant_id:
                legacy_query = legacy_query.where(IdentityProvider.tenant_id == tenant_id)
            legacy_result = await db.execute(legacy_query.with_for_update())
            legacy_candidates = legacy_result.scalars().all()
            legacy_scoped = [
                candidate
                for candidate in legacy_candidates
                if installation_scope
                and str(
                    (candidate.config or {}).get("installation_scope") or ""
                ).strip()
                == installation_scope
            ]
            if installation_scope and not legacy_scoped:
                legacy_unscoped = [
                    candidate
                    for candidate in legacy_candidates
                    if not str(
                        (candidate.config or {}).get("installation_scope") or ""
                    ).strip()
                ]
                if len(legacy_unscoped) == 1:
                    legacy_provider = legacy_unscoped[0]
                    legacy_provider.config = {
                        **(legacy_provider.config or {}),
                        "installation_scope": installation_scope,
                    }
                    await db.flush()
                    return legacy_provider
                scope_prefix = installation_scope.rsplit(":issuer:", 1)[0]
                rotated_legacy = [
                    candidate
                    for candidate in legacy_candidates
                    if str(
                        (candidate.config or {}).get("installation_scope") or ""
                    ).startswith(f"{scope_prefix}:issuer:")
                ]
                if len(rotated_legacy) == 1:
                    return rotated_legacy[0]
                legacy_candidates = []
            legacy_provider = choose_preferred_identity_provider(
                legacy_scoped or legacy_candidates,
                provider_type=legacy_type,
                tenant_id=str(tenant_id) if tenant_id else None,
            )
            if legacy_provider:
                return legacy_provider

        provider = IdentityProvider(
            provider_type=canonical_type,
            name=canonical_type.capitalize(),
            is_active=True,
            config=(
                {"installation_scope": installation_scope}
                if installation_scope
                else {}
            ),
            tenant_id=tenant_id,
        )
        db.add(provider)
        await db.flush()

        return provider

    async def _validate_bound_channel_user(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        subjects: list[tuple[str, str]],
        user: User | None,
    ) -> User:
        """Require an active principal and, when present, active source account."""
        if not user or not user.is_active:
            raise ChannelUserResolutionError(
                "Channel binding points to an unavailable user"
            )
        if user.identity_id and (
            user.identity is None or not user.identity.is_active
        ):
            raise ChannelUserResolutionError(
                "Channel binding points to an unavailable identity"
            )

        source_columns = {
            "union_id": OrgMember.unionid,
            "open_id": OrgMember.open_id,
            "staff_id": OrgMember.external_id,
            "user_id": OrgMember.external_id,
            "external_id": OrgMember.external_id,
        }
        source_matches = [
            source_columns[id_type] == subject
            for id_type, subject in subjects
            if id_type in source_columns
        ]
        if not source_matches:
            return user
        statuses = (
            await db.execute(
                select(OrgMember.status).where(
                    OrgMember.tenant_id == provider.tenant_id,
                    OrgMember.provider_id == provider.id,
                    or_(*source_matches),
                )
            )
        ).scalars().all()
        if statuses and "active" not in statuses:
            raise ChannelUserResolutionError(
                "Channel binding source account is inactive"
            )
        return user

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
            or (extra_info.get("sender_id") or "").strip()
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
            or (extra_info.get("sender_id") or "").strip()
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
