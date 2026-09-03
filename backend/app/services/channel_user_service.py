"""Channel user resolution service for messaging platforms.

This service provides unified user resolution for incoming messages from
external channels (DingTalk, WeCom, Feishu, etc.). It reuses the SSO service
and OrgMember-based identity management.
"""

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
from app.services.directory_identity_claims import VerifiedDirectoryClaims
from app.services.channel_user_errors import ChannelUserResolutionError
from app.services.channel_user_identity_mapping import ChannelUserIdentityMappingMethods
from app.services.channel_user_persistence import ChannelUserPersistenceMethods
from app.services.channel_user_provider_aliases import ChannelUserProviderAliasMethods
from app.services.sso_service import sso_service
from app.services.provider_identity_policy import identity_match_order, record_identity_match_conflict


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

    _load_user_with_identity = staticmethod(_load_user_with_identity)
    _provider_alias_scope = staticmethod(
        ChannelUserProviderAliasMethods._provider_alias_scope
    )
    _provider_alias_conflict_id_type = staticmethod(
        ChannelUserProviderAliasMethods._provider_alias_conflict_id_type
    )
    remember_provider_user_alias = (
        ChannelUserProviderAliasMethods.remember_provider_user_alias
    )
    resolve_provider_user_alias = (
        ChannelUserProviderAliasMethods.resolve_provider_user_alias
    )
    _find_provider_alias_bindings = (
        ChannelUserProviderAliasMethods._find_provider_alias_bindings
    )

    _normalize_channel_type = ChannelUserIdentityMappingMethods._normalize_channel_type
    _legacy_provider_types_for_channel = ChannelUserIdentityMappingMethods._legacy_provider_types_for_channel
    _get_channel_ids = ChannelUserIdentityMappingMethods._get_channel_ids
    _installation_scope = ChannelUserIdentityMappingMethods._installation_scope
    _resolve_installation_scope = ChannelUserIdentityMappingMethods._resolve_installation_scope
    resolve_installation_scope = ChannelUserIdentityMappingMethods.resolve_installation_scope
    _binding_subjects = ChannelUserIdentityMappingMethods._binding_subjects
    _mark_observed_source_account_active = (
        ChannelUserIdentityMappingMethods._mark_observed_source_account_active
    )
    _fresh_dingtalk_claims = ChannelUserIdentityMappingMethods._fresh_dingtalk_claims

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
                    ChannelUserBinding.provider_id == provider.id,
                    ChannelUserBinding.installation_scope == scope,
                    ChannelUserBinding.channel_type == self._normalize_channel_type(channel_type),
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
        if not rows:
            rows = await self._find_provider_alias_bindings(
                db,
                provider=provider,
                channel_type=channel_type,
                subjects=subjects,
            )
        if not rows:
            return None

        normalized = self._normalize_channel_type(channel_type)
        type_priority = (
            {"staff_id": 0, "union_id": 1, "open_id": 2, "sender_id": 3}
            if normalized == "dingtalk"
            else {
                id_type: index
                for index, (id_type, _subject) in enumerate(subjects)
            }
        )
        ordered_subjects = sorted(
            enumerate(subjects),
            key=lambda item: (type_priority.get(item[1][0], 99), item[0]),
        )
        selected_user_id = None
        all_user_ids = {row.user_id for row in rows}
        for _index, (id_type, subject) in ordered_subjects:
            subject_user_ids = {
                row.user_id
                for row in rows
                if row.id_type == id_type and row.subject == subject
            }
            if not subject_user_ids:
                continue
            if len(subject_user_ids) > 1:
                raise ChannelUserResolutionError(
                    "Provider subject maps to multiple canonical users; repair required"
                )
            selected_user_id = next(iter(subject_user_ids))
            break
        if selected_user_id is None:
            return None
        if len(all_user_ids) > 1:
            logger.error(
                "[{}] provider subjects map to multiple users; routing by the "
                "current event's strongest exact subject and scheduling repair",
                channel_type,
            )
        user = await _load_user_with_identity(
            db, selected_user_id, provider.tenant_id
        )
        return await self._validate_bound_channel_user(db, provider, subjects, user)

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
                ChannelUserBinding.provider_id == provider.id,
                ChannelUserBinding.installation_scope == scope,
                ChannelUserBinding.channel_type == self._normalize_channel_type(channel_type),
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
            logger.error(
                "[{}] refusing to rewrite conflicting installation-scoped bindings; "
                "continuing with the already resolved exact sender",
                channel_type,
            )
            return user, False
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
        provider: IdentityProvider | None = None,
    ) -> User:
        """Resolve channel user identity, find or create platform User.

        Priority order:
        1. Existing installation-scoped binding → return canonical User
        2. Exact OrgMember identity → resolve/link canonical User and bind
        3. Provider-policy contact match, then legacy/external-only fallback

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
        normalized_channel = self._normalize_channel_type(channel_type)
        provider, extra_info = await self.resolve_channel_provider(
            db, agent, channel_type, extra_info, provider
        )
        await self._mark_observed_source_account_active(
            db,
            provider,
            channel_type,
            external_user_id,
            extra_info,
        )
        fresh_claims = (
            self._fresh_dingtalk_claims(provider, external_user_id, extra_info)
            if normalized_channel == "dingtalk"
            else None
        )
        if fresh_claims:
            from app.services.dingtalk_identity_reconciliation import (
                dingtalk_legacy_identity_reconciler,
            )

            await dingtalk_legacy_identity_reconciler.acquire_subject_lock(
                db,
                tenant_id=fresh_claims.tenant_id,
                provider_id=fresh_claims.provider_id,
                external_id=fresh_claims.external_id,
            )

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
            if normalized_channel == "dingtalk":
                exact_route_user = canonical_user
                member = await self._find_existing_org_member_for_user(
                    db,
                    canonical_user.id,
                    provider.id,
                    tenant_id,
                )
                if member:
                    try:
                        async with db.begin_nested():
                            self._merge_channel_info_into_member(
                                member, channel_type, extra_info
                            )
                            reconciled = await self._provision_user_from_member(
                                db,
                                member,
                                provider,
                                fresh_claims=fresh_claims,
                                subject_lock_held=fresh_claims is not None,
                            )
                            if reconciled:
                                canonical_user, _ = await self._ensure_bindings(
                                    db,
                                    provider,
                                    channel_type,
                                    external_user_id,
                                    extra_info,
                                    reconciled,
                                )
                    except Exception:
                        canonical_user = exact_route_user
                        logger.exception(
                            "[DingTalk] canonical reconciliation failed; "
                            "continuing with the exact installation binding user {}",
                            canonical_user.id,
                        )
            return canonical_user

        # Step 2: Try to find OrgMember by external identity
        try:
            org_member = await self._find_org_member(
                db, provider.id, channel_type, external_user_id, extra_info
            )
        except ChannelUserResolutionError:
            if normalized_channel != "dingtalk":
                raise
            # Corrupt/ambiguous historical directory rows must not stop an
            # otherwise attributable DingTalk event.  The installation-scoped
            # sender binding below remains exact and can be reconciled later.
            logger.exception(
                "[DingTalk] directory identity lookup is ambiguous; "
                "continuing with exact sender routing"
            )
            org_member = None

        # Step 3: Resolve User from OrgMember or other means
        user = None
        if org_member:
            org_member_id = org_member.id
            if org_member.tenant_id is None and provider.tenant_id:
                org_member.tenant_id = provider.tenant_id
            if normalized_channel == "dingtalk":
                provisioned_user = None
                try:
                    async with db.begin_nested():
                        self._merge_channel_info_into_member(
                            org_member, channel_type, extra_info
                        )
                        provisioned_user = await self._provision_user_from_member(
                            db,
                            org_member,
                            provider,
                            fresh_claims=fresh_claims,
                            subject_lock_held=fresh_claims is not None,
                        )
                except Exception:
                    logger.exception(
                        "[DingTalk] OrgMember reconciliation failed; "
                        "falling back to exact sender routing"
                    )
                    org_member = await db.get(OrgMember, org_member_id)
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
                fallback_user = await self._create_channel_user(
                    db, channel_type, external_user_id, extra_info, tenant_id
                )
                fallback_user, _ = await self._ensure_bindings(
                    db,
                    provider,
                    channel_type,
                    external_user_id,
                    extra_info,
                    fallback_user,
                )
                org_member.user_id = fallback_user.id
                await db.flush()
                return fallback_user

        # Step 4: Try to find User by email/mobile from extra_info
        verified_contact = extra_info.get("identity_verified") is True
        if normalized_channel == "dingtalk":
            email = fresh_claims.email if fresh_claims else None
            mobile = fresh_claims.phone if fresh_claims else None
        else:
            email = extra_info.get("email") if verified_contact else None
            mobile = extra_info.get("mobile") if verified_contact else None

        should_persist_member = True

        if not org_member and should_persist_member and normalized_channel == "dingtalk":
            if verified_contact and (email or mobile):
                from app.services.canonical_user_resolver import (
                    CanonicalIdentityConflict,
                    CanonicalUserConflict,
                    canonical_user_resolver,
                )

                try:
                    async with db.begin_nested():
                        claims = await canonical_user_resolver.resolve_identity_claims(
                            db, email=email, phone=mobile, enrich=True,
                            ordered_fields=identity_match_order(provider),
                        )
                        await record_identity_match_conflict(db, provider=provider, tenant_id=tenant_id, claims=claims, source="im_inbound")
                        if claims.identity:
                            user = await canonical_user_resolver.get_tenant_user(
                                db,
                                tenant_id=tenant_id,
                                identity_id=claims.identity.id,
                                lock=True,
                            )
                            if user is None:
                                user, _ = await canonical_user_resolver.get_or_create_tenant_user(
                                    db,
                                    tenant_id=tenant_id,
                                    identity=claims.identity,
                                    display_name=extra_info.get("name") or claims.identity.username or "User",
                                    avatar_url=extra_info.get("avatar_url"),
                                    registration_source="dingtalk",
                                )
                except (CanonicalIdentityConflict, CanonicalUserConflict):
                    logger.exception(
                        "[DingTalk] verified contact is conflicted; "
                        "creating an identityless exact-route user so delivery continues"
                    )
            # One-release migration bridge for the historical DingTalk login
            # principal. Verified enterprise contact evidence is stronger and
            # has already been checked above. Use this exact legacy principal
            # only when neither an OrgMember nor a verified tenant User exists.
            if not user and external_user_id:
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
                    logger.error(
                        "[DingTalk] legacy principal maps to multiple tenant users; "
                        "ignoring the legacy bridge and continuing with exact sender routing"
                    )
                elif legacy_users:
                    user = legacy_users[0]
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

        for field in identity_match_order(provider):
            value = mobile if field == "phone" else email
            if user or not value:
                continue
            matcher = sso_service.match_user_by_mobile if field == "phone" else sso_service.match_user_by_email
            user = await matcher(db, value, tenant_id)
            if user:
                logger.info("[{}] Matched user by configured {}", channel_type, field)

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

    _merge_channel_info_into_member = (
        ChannelUserIdentityMappingMethods._merge_channel_info_into_member
    )

    async def _provision_user_from_member(
        self,
        db: AsyncSession,
        org_member: OrgMember,
        provider: IdentityProvider,
        *,
        fresh_claims: VerifiedDirectoryClaims | None = None,
        subject_lock_held: bool = False,
    ) -> User | None:
        from app.services.contact_provisioning import contact_provisioning

        from app.services.canonical_user_resolver import (
            CanonicalIdentityConflict,
            CanonicalUserConflict,
        )

        try:
            result = await contact_provisioning.ensure_user_for_org_member(
                db,
                org_member,
                provider=provider,
                fresh_claims=fresh_claims,
                subject_lock_held=subject_lock_held,
            )
        except (CanonicalIdentityConflict, CanonicalUserConflict) as exc:
            raise ChannelUserResolutionError(str(exc)) from exc
        if not result.user:
            return None
        return await _load_user_with_identity(db, result.user.id, org_member.tenant_id)

    resolve_channel_provider = ChannelUserPersistenceMethods.resolve_channel_provider
    _ensure_provider = ChannelUserPersistenceMethods._ensure_provider
    _validate_bound_channel_user = ChannelUserPersistenceMethods._validate_bound_channel_user
    _find_org_member = ChannelUserIdentityMappingMethods._find_org_member
    _create_org_member_shell = ChannelUserPersistenceMethods._create_org_member_shell
    _find_existing_org_member_for_user = (
        ChannelUserPersistenceMethods._find_existing_org_member_for_user
    )
    _create_channel_user = ChannelUserPersistenceMethods._create_channel_user


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
