"""Channel user resolution service for messaging platforms.

This service provides unified user resolution for incoming messages from
external channels (DingTalk, WeCom, Feishu, etc.). It reuses the SSO service
and OrgMember-based identity management.
"""

import hashlib
import uuid
from typing import Any

from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agent import Agent
from app.models.identity import IdentityProvider
from app.models.org import ChannelUserBinding, OrgMember
from app.models.user import Identity, User
from app.services.sso_service import sso_service


class ChannelUserResolutionError(ValueError):
    """Raised when a channel message cannot be safely attributed to a user."""


async def _load_user_with_identity(
    db: AsyncSession,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
) -> User | None:
    query = select(User).where(User.id == user_id).options(selectinload(User.identity))
    if tenant_id:
        query = query.where(User.tenant_id == tenant_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


class ChannelUserService:
    """Service for resolving channel users via OrgMember and SSO patterns."""

    CHANNEL_TYPE_ALIASES = {
        "microsoft_teams": "teams",
    }

    def _normalize_channel_type(self, channel_type: str) -> str:
        raw = (channel_type or "").strip().lower()
        return self.CHANNEL_TYPE_ALIASES.get(raw, raw)

    def _legacy_provider_types_for_channel(self, channel_type: str) -> list[str]:
        normalized = self._normalize_channel_type(channel_type)
        legacy = [normalized]
        if normalized == "teams":
            legacy.append("microsoft_teams")
        elif normalized == "microsoft_teams":
            legacy.append("teams")
        return legacy

    def _get_channel_ids(
        self,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
    ) -> tuple[str | None, str | None, str | None]:
        normalized_channel = self._normalize_channel_type(channel_type)
        unionid = (extra_info.get("unionid") or extra_info.get("union_id") or "").strip() or None
        open_id = (extra_info.get("open_id") or "").strip() or None
        external_id = (extra_info.get("external_id") or external_user_id or "").strip() or None

        if normalized_channel == "feishu":
            # Feishu external_id must remain tenant-stable user_id only.
            # Never backfill it from open_id.
            external_id = (extra_info.get("external_id") or "").strip() or None
        elif normalized_channel == "dingtalk":
            open_id = open_id or None
        elif normalized_channel == "wecom":
            unionid = None
            open_id = open_id or None
        else:
            unionid = None
            open_id = None

        return unionid, open_id, external_id

    def _installation_scope(
        self,
        provider: IdentityProvider,
        extra_info: dict[str, Any] | None = None,
    ) -> str:
        """Return the non-secret installation namespace for channel subjects."""
        explicit = str((extra_info or {}).get("_installation_scope") or "").strip()
        if explicit:
            return explicit
        configured = str((provider.config or {}).get("installation_scope") or "").strip()
        return configured or f"provider:{provider.id}"

    async def _resolve_installation_scope(
        self,
        db: AsyncSession,
        agent: Agent,
        channel_type: str,
        extra_info: dict[str, Any],
    ) -> str:
        """Build a stable non-secret namespace for the receiving bot installation."""
        explicit = str(extra_info.get("_installation_scope") or "").strip()
        if explicit:
            return explicit

        from app.models.channel_config import ChannelConfig

        normalized = self._normalize_channel_type(channel_type)
        config_type = "microsoft_teams" if normalized == "teams" else normalized
        config = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent.id,
                    ChannelConfig.channel_type == config_type,
                )
            )
        ).scalar_one_or_none()
        if config is None:
            return f"agent:{agent.id}:channel:{normalized}"

        extra_config = config.extra_config if isinstance(config.extra_config, dict) else {}
        issuer = str(
            extra_info.get("_issuer")
            or config.app_id
            or extra_config.get("app_id")
            or extra_config.get("workspace_id")
            or extra_config.get("team_id")
            or extra_config.get("corp_id")
            or extra_config.get("robot_code")
            or extra_config.get("phone_number_id")
            or ""
        ).strip()
        if issuer:
            issuer_hash = hashlib.sha256(issuer.encode("utf-8")).hexdigest()[:32]
            return f"agent:{agent.id}:channel:{normalized}:issuer:{issuer_hash}"
        return f"channel-config:{config.id}"

    async def resolve_installation_scope(
        self,
        db: AsyncSession,
        agent: Agent,
        channel_type: str,
    ) -> str:
        """Return the same installation namespace used by inbound bindings."""
        return await self._resolve_installation_scope(db, agent, channel_type, {})

    def _binding_subjects(
        self,
        provider: IdentityProvider,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
    ) -> list[tuple[str, str]]:
        unionid, open_id, external_id = self._get_channel_ids(
            channel_type, external_user_id, extra_info
        )
        normalized = self._normalize_channel_type(channel_type)
        external_type = {
            "feishu": "user_id",
            "dingtalk": "staff_id",
            "wecom": "user_id",
        }.get(normalized, "external_id")
        candidates = [
            ("union_id", unionid),
            ("open_id", open_id),
            (external_type, external_id),
        ]
        seen: set[tuple[str, str]] = set()
        subjects: list[tuple[str, str]] = []
        for id_type, subject in candidates:
            full_subject = str(subject or "").strip()
            key = (id_type, full_subject)
            if full_subject and key not in seen:
                seen.add(key)
                subjects.append(key)
        return subjects

    async def _find_bound_user(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
    ) -> User | None:
        if provider.tenant_id is None:
            raise ChannelUserResolutionError("Channel provider has no tenant scope")
        subjects = self._binding_subjects(provider, channel_type, external_user_id, extra_info)
        if not subjects:
            return None
        scope = self._installation_scope(provider, extra_info)
        rows = (
            await db.execute(
                select(ChannelUserBinding).where(
                    ChannelUserBinding.tenant_id == provider.tenant_id,
                    ChannelUserBinding.installation_scope == scope,
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
        user_ids = {row.user_id for row in rows}
        if len(user_ids) > 1:
            raise ChannelUserResolutionError(
                "Channel subjects resolve to multiple users; refusing ambiguous attribution"
            )
        if not user_ids:
            return None
        user = await _load_user_with_identity(db, next(iter(user_ids)), provider.tenant_id)
        if not user or not user.is_active:
            raise ChannelUserResolutionError("Channel binding points to an unavailable user")
        return user

    async def _ensure_bindings(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
        user: User,
    ) -> tuple[User, bool]:
        if provider.tenant_id is None or user.tenant_id != provider.tenant_id:
            raise ChannelUserResolutionError("Channel binding tenant does not match user tenant")
        subjects = self._binding_subjects(provider, channel_type, external_user_id, extra_info)
        if not subjects:
            raise ChannelUserResolutionError("Channel sender has no scoped subject identifier")
        scope = self._installation_scope(provider, extra_info)
        pending: list[tuple[str, str]] = []
        saw_existing = False
        existing_user_ids: set[uuid.UUID] = set()
        for id_type, subject in subjects:
            query = select(ChannelUserBinding).where(
                ChannelUserBinding.tenant_id == provider.tenant_id,
                ChannelUserBinding.installation_scope == scope,
                ChannelUserBinding.id_type == id_type,
                ChannelUserBinding.subject == subject,
            )
            binding = (await db.execute(query)).scalar_one_or_none()
            if binding:
                saw_existing = True
                existing_user_ids.add(binding.user_id)
                continue
            pending.append((id_type, subject))

        if len(existing_user_ids) > 1:
            raise ChannelUserResolutionError(
                "Channel subjects resolve to multiple users; refusing ambiguous attribution"
            )
        if existing_user_ids:
            canonical = await _load_user_with_identity(
                db, next(iter(existing_user_ids)), provider.tenant_id
            )
            if canonical is None or not canonical.is_active:
                raise ChannelUserResolutionError(
                    "Channel binding points to an unavailable user"
                )
            # The scoped binding is authoritative. This branch is also the
            # normal loser path when another replica commits the same ingress
            # between the initial lookup and this transactional insert.
            user = canonical

        if not pending:
            return user, False

        try:
            # Save the complete identifier set atomically. A conflict must not
            # leave a partial union/open/external mapping in this transaction.
            async with db.begin_nested():
                for id_type, subject in pending:
                    db.add(
                        ChannelUserBinding(
                            tenant_id=provider.tenant_id,
                            provider_id=provider.id,
                            installation_scope=scope,
                            channel_type=self._normalize_channel_type(channel_type),
                            id_type=id_type,
                            subject=subject,
                            user_id=user.id,
                        )
                    )
                await db.flush()
        except IntegrityError:
            canonical = await self._find_bound_user(
                db, provider, channel_type, external_user_id, extra_info
            )
            if canonical and (not saw_existing or canonical.id == user.id):
                return canonical, False
            raise ChannelUserResolutionError(
                "Concurrent channel binding could not be resolved to one canonical user"
            )
        # Only the transaction that established the first binding owns lazy
        # OrgMember-shell creation. Transactions that merely complete another
        # subject for an already-bound person must not create duplicate shells.
        return user, not saw_existing

    async def resolve_channel_user(
        self,
        db: AsyncSession,
        agent: Agent,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any] | None = None,
    ) -> User:
        """Resolve channel user identity, find or create platform User.

        Priority order:
        1. OrgMember already linked to User → return existing User
        2. OrgMember exists but not linked → create User and link
        3. User matched by email/mobile → return User and link OrgMember
        4. No match → create new User and OrgMember (lazy registration)

        Args:
            db: Database session
            agent: Agent receiving the message (for tenant_id)
            channel_type: "dingtalk" | "wecom" | "wechat" | "feishu"
            external_user_id: User ID from external platform. For Feishu this must be user_id, not open_id.
            extra_info: Optional name/avatar/mobile/email from platform API

        Returns:
            Resolved User instance
        """
        tenant_id = agent.tenant_id
        extra_info = dict(extra_info or {})
        extra_info["_installation_scope"] = await self._resolve_installation_scope(
            db, agent, channel_type, extra_info
        )

        # Step 1: Ensure IdentityProvider exists
        provider = await self._ensure_provider(db, channel_type, tenant_id)

        bound_user = await self._find_bound_user(
            db, provider, channel_type, external_user_id, extra_info
        )
        if bound_user:
            # A provider may reveal additional exact subjects over time (for
            # example open_id first, then union_id). Complete those scoped
            # bindings transactionally without changing the canonical user.
            canonical_user, _ = await self._ensure_bindings(
                db,
                provider,
                channel_type,
                external_user_id,
                extra_info,
                bound_user,
            )
            return canonical_user

        # One-release migration bridge for the historical DingTalk login
        # principal. It is exact, tenant-scoped, unique and active; no display
        # name/contact guessing is involved. The scoped binding becomes the
        # only lookup path after this first successful message.
        normalized_channel = self._normalize_channel_type(channel_type)
        if normalized_channel == "dingtalk" and external_user_id:
            legacy_username = f"dingtalk_{external_user_id}"
            legacy_users = (
                await db.execute(
                    select(User)
                    .join(User.identity)
                    .where(
                        User.tenant_id == tenant_id,
                        User.is_active == True,  # noqa: E712
                        Identity.username == legacy_username,
                    )
                    .options(selectinload(User.identity))
                )
            ).scalars().all()
            if len(legacy_users) > 1:
                raise ChannelUserResolutionError(
                    "Legacy DingTalk subject maps to multiple tenant users; migration_required"
                )
            if legacy_users:
                legacy_user = legacy_users[0]
                canonical_user, binding_created = await self._ensure_bindings(
                    db, provider, channel_type, external_user_id, extra_info, legacy_user
                )
                if binding_created:
                    legacy_member = await self._create_org_member_shell(
                        db,
                        provider,
                        channel_type,
                        external_user_id,
                        extra_info,
                        linked_user_id=canonical_user.id,
                    )
                    from app.services.contact_provisioning import contact_provisioning

                    await contact_provisioning.ensure_user_for_org_member(
                        db, legacy_member, provider=provider
                    )
                return canonical_user

        # Step 2: Try to find OrgMember by external identity
        org_member = await self._find_org_member(
            db, provider.id, channel_type, external_user_id, extra_info
        )

        # Step 3: Resolve User from OrgMember or other means
        user = None
        if org_member:
            if org_member.tenant_id is None and provider.tenant_id:
                org_member.tenant_id = provider.tenant_id
            if normalized_channel == "dingtalk":
                self._merge_channel_info_into_member(org_member, channel_type, extra_info)
                provisioned_user = await self._provision_user_from_member(
                    db,
                    org_member,
                    provider,
                )
                if provisioned_user:
                    provisioned_user, _binding_created = await self._ensure_bindings(
                        db, provider, channel_type, external_user_id, extra_info, provisioned_user
                    )
                    org_member.user_id = provisioned_user.id
                    logger.info(
                        f"[{channel_type}] Provisioned user via OrgMember {org_member.id}: {provisioned_user.id}"
                    )
                    return provisioned_user

            if org_member.user_id:
                user = await _load_user_with_identity(db, org_member.user_id, tenant_id)
                if user:
                    user, _binding_created = await self._ensure_bindings(
                        db, provider, channel_type, external_user_id, extra_info, user
                    )
                    org_member.user_id = user.id
                    logger.debug(
                        f"[{channel_type}] Found user via linked OrgMember: {user.id}"
                    )
                    return user

            if normalized_channel == "dingtalk":
                raise ChannelUserResolutionError(
                    f"DingTalk OrgMember {org_member.id} could not be provisioned safely by mobile"
                )

        # Step 4: Try to find User by email/mobile from extra_info
        verified_contact = extra_info.get("identity_verified") is True
        email = extra_info.get("email") if verified_contact else None
        mobile = extra_info.get("mobile") if verified_contact else None

        should_persist_member = True

        if not org_member and should_persist_member and normalized_channel == "dingtalk":
            if mobile:
                user = await sso_service.match_user_by_mobile(db, mobile, tenant_id)
            proposed_user = None
            if not user:
                proposed_user = await self._create_channel_user(
                    db, channel_type, external_user_id, extra_info, tenant_id
                )
                user = proposed_user
            user, binding_created = await self._ensure_bindings(
                db, provider, channel_type, external_user_id, extra_info, user
            )
            if proposed_user is not None and user.id != proposed_user.id:
                await db.delete(proposed_user)
            if binding_created:
                await self._create_org_member_shell(
                    db,
                    provider,
                    channel_type,
                    external_user_id,
                    extra_info,
                    linked_user_id=user.id,
                )
            return user

        if not user and email:
            user = await sso_service.match_user_by_email(db, email, tenant_id)
            if user:
                logger.info(
                    f"[{channel_type}] Matched user by email: {user.id}"
                )

        if not user and mobile:
            user = await sso_service.match_user_by_mobile(db, mobile, tenant_id)
            if user:
                logger.info(
                    f"[{channel_type}] Matched user by mobile: {user.id}"
                )

        # If found User by email/mobile, link OrgMember if exists
        if user:
            user, binding_created = await self._ensure_bindings(
                db, provider, channel_type, external_user_id, extra_info, user
            )
            if org_member:
                org_member.user_id = user.id
            elif should_persist_member and binding_created:
                await self._create_org_member_shell(
                    db, provider, channel_type, external_user_id, extra_info,
                    linked_user_id=user.id
                )
            await db.flush()
            return user

        if not self._binding_subjects(provider, channel_type, external_user_id, extra_info):
            raise ChannelUserResolutionError("Channel sender has no scoped subject identifier")

        # Step 5: Create new User (lazy registration)
        user = await self._create_channel_user(
            db, channel_type, external_user_id, extra_info, tenant_id
        )
        proposed_user = user
        user, binding_created = await self._ensure_bindings(
            db, provider, channel_type, external_user_id, extra_info, user
        )
        if user.id != proposed_user.id:
            await db.delete(proposed_user)

        # Step 6: Link or create OrgMember
        if should_persist_member:
            if org_member:
                org_member.user_id = user.id
            elif binding_created:
                await self._create_org_member_shell(
                    db, provider, channel_type, external_user_id, extra_info,
                    linked_user_id=user.id
                )
            await db.flush()
        logger.info(
            f"[{channel_type}] Created new user: {user.id} for external_id: {external_user_id}"
        )

        return user

    def _merge_channel_info_into_member(
        self,
        org_member: OrgMember,
        channel_type: str,
        extra_info: dict[str, Any],
    ) -> None:
        identity_seed = org_member.external_id or org_member.open_id or org_member.id.hex
        generated_name = f"{channel_type.capitalize()} User {identity_seed[:8]}"
        incoming_name = (extra_info.get("name") or "").strip()
        if incoming_name and (not org_member.name or org_member.name == generated_name):
            org_member.name = incoming_name
        contact_verified = extra_info.get("identity_verified") is True
        if contact_verified and extra_info.get("email") and not org_member.email:
            org_member.email = extra_info["email"]
        if contact_verified and extra_info.get("mobile") and not org_member.phone:
            org_member.phone = extra_info["mobile"]
        if extra_info.get("avatar_url") and not org_member.avatar_url:
            org_member.avatar_url = extra_info["avatar_url"]
        if extra_info.get("title") and not org_member.title:
            org_member.title = extra_info["title"]

    async def _provision_user_from_member(
        self,
        db: AsyncSession,
        org_member: OrgMember,
        provider: IdentityProvider,
    ) -> User | None:
        from app.services.contact_provisioning import contact_provisioning

        result = await contact_provisioning.ensure_user_for_org_member(
            db,
            org_member,
            provider=provider,
        )
        if not result.user:
            return None
        return await _load_user_with_identity(db, result.user.id, org_member.tenant_id)

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

    async def _find_org_member(
        self,
        db: AsyncSession,
        provider_id: uuid.UUID,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any] | None = None,
    ) -> OrgMember | None:
        """Find OrgMember by external identity.

        For Feishu: try unionid first, then open_id, then external_id
        For DingTalk: try unionid first, then external_id
        For WeCom: try external_id (userid)
        For WeChat: try external_id (from_user_id)

        Returns None if OrgMember not found or org sync is not enabled for this channel.
        """
        try:
            extra_info = extra_info or {}
            unionid, open_id, external_id = self._get_channel_ids(
                channel_type, external_user_id, extra_info
            )

            # Build OR conditions for matching
            conditions = [OrgMember.provider_id == provider_id, OrgMember.status == "active"]

            # Channel-specific matching priority
            normalized_channel = self._normalize_channel_type(channel_type)
            if normalized_channel == "feishu":
                # Feishu identifiers have distinct semantics:
                # unionid/open_id come from extra_info; external_id is user_id only.
                lookup_conditions = []
                if unionid:
                    lookup_conditions.append(OrgMember.unionid == unionid)
                if open_id:
                    lookup_conditions.append(OrgMember.open_id == open_id)
                if external_id:
                    lookup_conditions.append(OrgMember.external_id == external_id)
                if not lookup_conditions:
                    return None
                conditions.append(lookup_conditions[0])
                for cond in lookup_conditions[1:]:
                    conditions[-1] = conditions[-1] | cond
            elif normalized_channel == "dingtalk":
                # DingTalk: unionid is stable across apps, then external_id
                lookup_conditions = []
                if unionid:
                    lookup_conditions.append(OrgMember.unionid == unionid)
                if external_id:
                    lookup_conditions.append(OrgMember.external_id == external_id)
                if not lookup_conditions:
                    return None
                conditions.append(lookup_conditions[0])
                for cond in lookup_conditions[1:]:
                    conditions[-1] = conditions[-1] | cond
            elif normalized_channel == "wecom":
                # WeCom: external_id (userid) is the primary identifier
                if not external_id:
                    return None
                conditions.append(OrgMember.external_id == external_id)
            else:
                # Generic channels: provider is already channel-scoped, so external_id
                # can be used directly without namespacing.
                if not external_id:
                    return None
                conditions.append(OrgMember.external_id == external_id)

            query = (
                select(OrgMember)
                .where(*conditions)
                .order_by(
                    OrgMember.synced_at.asc(),
                    OrgMember.id.asc(),
                )
            )
            result = await db.execute(query)
            rows = result.scalars().all()
            if not rows:
                return None
            if normalized_channel not in {"feishu", "dingtalk", "wecom"}:
                # Historical generic-channel OrgMembers were tenant/provider
                # scoped, not installation scoped. They are an exact bridge
                # only when that tenant has one installation for the channel;
                # with multiple workspaces/bots the same external subject may
                # refer to different people, so fail closed for repair instead
                # of merging or creating a silent duplicate.
                from app.models.channel_config import ChannelConfig

                config_types = [normalized_channel]
                if normalized_channel == "teams":
                    config_types.append("microsoft_teams")
                installation_count = (
                    await db.execute(
                        select(func.count(ChannelConfig.id))
                        .select_from(ChannelConfig)
                        .join(Agent, Agent.id == ChannelConfig.agent_id)
                        .where(
                            Agent.tenant_id == rows[0].tenant_id,
                            ChannelConfig.channel_type.in_(config_types),
                        )
                    )
                ).scalar_one()
                if installation_count != 1:
                    row_user_ids = {row.user_id for row in rows if row.user_id is not None}
                    bound_user_ids = set(
                        (
                            await db.execute(
                                select(ChannelUserBinding.user_id).where(
                                    ChannelUserBinding.provider_id == provider_id,
                                    ChannelUserBinding.user_id.in_(row_user_ids),
                                    ChannelUserBinding.channel_type.in_(config_types),
                                    ChannelUserBinding.id_type == "external_id",
                                    ChannelUserBinding.subject == external_id,
                                )
                            )
                        ).scalars().all()
                    ) if row_user_ids else set()
                    if row_user_ids and row_user_ids.issubset(bound_user_ids):
                        # These are modern shells owned by other installation
                        # scopes, not unresolved legacy data. Ignore them so
                        # the current installation can create its own isolated
                        # canonical user and binding for the same subject.
                        return None
                    raise ChannelUserResolutionError(
                        "Legacy channel subject has no unique installation scope; migration_required"
                    )
            if len(rows) > 1:
                user_ids = {row.user_id for row in rows}
                if None in user_ids or len(user_ids) != 1:
                    raise ChannelUserResolutionError(
                        "Directory subject maps to multiple OrgMember records; migration_required"
                    )
            return rows[0]
        except ChannelUserResolutionError:
            raise
        except Exception as e:
            raise ChannelUserResolutionError(
                f"Directory identity lookup failed closed for {channel_type}"
            ) from e

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
        name = extra_info.get("name") or f"{channel_type.capitalize()} User {identity_seed[:8]}"
        unionid, open_id, external_id = self._get_channel_ids(channel_type, external_user_id, extra_info)

        member = OrgMember(
            name=name,
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
        name = extra_info.get("name") or f"{channel_type.capitalize()} {identity_seed[:8]}"

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


# Global service instance
channel_user_service = ChannelUserService()


async def get_platform_user_by_org_member(
    db: AsyncSession,
    org_member: OrgMember,
    agent_tenant_id: uuid.UUID | None = None,
) -> User:
    """Get or create platform User from an existing OrgMember.

    This is used by agent_tools.py when sending proactive messages:
    - OrgMember already exists (from AgentRelationship)
    - But user_id may be NULL (not yet linked to platform User)
    - We need to get or create the User and link it

    Args:
        db: Database session
        org_member: Existing OrgMember instance
        agent_tenant_id: Optional tenant ID for scoping

    Returns:
        Linked/created User instance
    """
    from app.models.identity import IdentityProvider

    if agent_tenant_id:
        if org_member.tenant_id and org_member.tenant_id != agent_tenant_id:
            raise ChannelUserResolutionError(
                f"OrgMember {org_member.id} belongs to tenant {org_member.tenant_id}, "
                f"not agent tenant {agent_tenant_id}"
            )
        if org_member.tenant_id is None:
            org_member.tenant_id = agent_tenant_id

    provider = await db.get(IdentityProvider, org_member.provider_id)
    if org_member.user_id:
        linked_user = await _load_user_with_identity(db, org_member.user_id, agent_tenant_id)
        if linked_user:
            return linked_user

    from app.services.contact_provisioning import contact_provisioning

    provisioning = await contact_provisioning.ensure_user_for_org_member(
        db,
        org_member,
        provider=provider,
    )
    if provisioning.user:
        user = await _load_user_with_identity(db, provisioning.user.id, agent_tenant_id)
        if user:
            return user

    raise ChannelUserResolutionError(
        f"OrgMember {org_member.id} cannot be provisioned without a tenant-scoped active user "
        "or a safe external-only user"
    )
