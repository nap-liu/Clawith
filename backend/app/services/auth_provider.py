"""Generic OAuth/SSO authentication provider framework.

This module provides a base class for all identity providers (Feishu, DingTalk, WeCom, etc.)
and concrete implementations for each supported provider.
"""

from urllib.parse import quote, urlencode

import httpx
from abc import ABC, abstractmethod
from fastapi import HTTPException
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import create_access_token, hash_password
from app.models.identity import IdentityProvider
from app.models.user import User, Identity
from app.services.google_workspace_oauth import GOOGLE_HTTP_PROXY
from app.services.identity_provider_lookup import get_preferred_identity_provider
from app.services.provider_identity_policy import identity_match_order
from app.services.auth_provider_models import ExternalUserInfo
from loguru import logger

ExternalUserInfo.__module__ = __name__


class BaseAuthProvider(ABC):
    """Abstract base class for all authentication providers."""

    provider_type: str = ""

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        """Initialize provider with optional config from database.

        Args:
            provider: IdentityProvider model instance from database
            config: Configuration dict (fallback if no provider record)
        """
        self.provider = provider
        self.config = config or {}
        if provider and provider.config:
            self.config = provider.config

    @abstractmethod
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Generate OAuth authorization URL.

        Args:
            redirect_uri: Callback URL after authorization
            state: CSRF state parameter

        Returns:
            Authorization URL to redirect user to
        """
        pass

    @abstractmethod
    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        """Exchange authorization code for access token.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            Dict containing access_token and optionally refresh_token
        """
        pass

    @abstractmethod
    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        """Fetch user profile from provider API.

        Args:
            access_token: Valid access token

        Returns:
            ExternalUserInfo instance with user data
        """
        pass

    async def find_or_create_user(
        self, db: AsyncSession, user_info: ExternalUserInfo, tenant_id: str | None = None
    ) -> tuple[User, bool]:
        """Find existing user or create new one via Identity/OrgMember."""
        from app.services.platform_auth_policy import enforce_sso_login_policy
        from app.services.sso_service import sso_service

        # Ensure provider exists
        await self._ensure_provider(db, tenant_id)

        # Enterprise login has a trusted tenant boundary.  Resolve all strong
        # claims through the shared canonical path so directory/OAuth/IM order
        # cannot create a second tenant User.
        if tenant_id:
            result = await self._find_or_create_enterprise_user(
                db, user_info, tenant_id
            )
            await enforce_sso_login_policy(db, result[0], result[1])
            return result

        provider_user_id = user_info.provider_user_id
        user = await sso_service.resolve_user_identity(
            db,
            provider_user_id,
            self.provider_type,
            tenant_id=tenant_id,
            identity_data=user_info.raw_data,
        )

        is_new = False
        if not user:
            for field in identity_match_order(self.provider or self.config):
                value = user_info.mobile if field == "phone" else user_info.email
                if not value:
                    continue
                matcher = (
                    sso_service.match_user_by_mobile
                    if field == "phone"
                    else sso_service.match_user_by_email
                )
                user = await matcher(db, value, tenant_id)
                if user:
                    break
            if user:
                # If we found a user via email/mobile matching, it might be in a different tenant
                if tenant_id and str(user.tenant_id) != tenant_id:
                    # Identity exists but no user in this tenant
                    user = None 
        # 5. 通过 provider_user_id 匹配现有用户 username（同租户下唯一登录凭证）
        if not user and user_info.provider_user_id:
            from sqlalchemy import and_
            from app.models.user import Identity as IdentityModel
            from sqlalchemy.orm import selectinload

            # 构建查询条件：tenant_id 有值时匹配该租户或无租户的用户，为 None 时不限制租户
            if tenant_id:
                from sqlalchemy import or_
                where_clause = and_(
                    IdentityModel.username == user_info.provider_user_id,
                    or_(User.tenant_id == tenant_id, User.tenant_id.is_(None)),
                )
            else:
                where_clause = IdentityModel.username == user_info.provider_user_id

            result = await db.execute(
                select(User).join(User.identity).where(where_clause).options(selectinload(User.identity))
            )
            candidate = result.scalar_one_or_none()
            if candidate:
                user = candidate
                await sso_service.link_identity(
                    db,
                    str(user.id),
                    self.provider_type,
                    user_info.provider_user_id,
                    user_info.raw_data,
                    tenant_id=tenant_id,
                )
                logger.info(f"[SSO] Matched existing user by username: {user.username} (tenant_id={tenant_id})")


        # 匹配到用户后，检查是否需要自动绑定租户
        if user and tenant_id and not user.tenant_id:
            user.tenant_id = tenant_id
            logger.info(f"[SSO] Auto-bound user {user.username} to tenant {tenant_id}")

        if user:
            # Update user info and ensure identity is loaded
            if not user.identity_id:
                 from app.services.registration_service import registration_service
                 identity = await registration_service.find_or_create_identity(
                     db, email=user_info.email, phone=user_info.mobile, provider=self.provider
                 )
                 user.identity_id = identity.id

            # Ensure identity is loaded for proxy field access
            if not hasattr(user, "identity") or user.identity is None:
                from sqlalchemy.orm import selectinload as _sil
                refreshed = await db.execute(
                    select(User).where(User.id == user.id).options(_sil(User.identity))
                )
                user = refreshed.scalar_one()

            await self._update_existing_user(db, user, user_info)
        else:
            # 3. Create new user (and Identity if needed)
            user = await self._create_new_user(db, user_info, tenant_id)
            is_new = True
            
        # Ensure OrgMember linkage
        await sso_service.link_identity(
            db,
            str(user.id),
            self.provider_type,
            provider_user_id,
            user_info.raw_data,
            tenant_id=tenant_id,
        )

        # SSO users should also appear as Web members for tenant-side user management.
        from app.services.registration_service import registration_service
        await registration_service.ensure_web_org_member(db, user)
        await enforce_sso_login_policy(db, user, is_new)
        return user, is_new

    async def _find_or_create_enterprise_user(
        self,
        db: AsyncSession,
        user_info: ExternalUserInfo,
        tenant_id: str,
    ) -> tuple[User, bool]:
        import uuid

        from app.services.canonical_user_resolver import (
            CanonicalIdentityConflict,
            CanonicalUserConflict,
            canonical_user_resolver,
        )
        from app.services.registration_service import registration_service
        from app.services.sso_service import sso_service

        tenant_uuid = uuid.UUID(str(tenant_id))
        try:
            provider_model = await self._ensure_provider(db, tenant_id)
            exact_provider_model = provider_model if provider_model.tenant_id == tenant_uuid else None
            oauth_subject = None
            if self.provider_type == "oauth2" and exact_provider_model is not None:
                from app.services.oauth_identity import (
                    normalize_oauth_email,
                    oauth_identity_service,
                )

                oauth_subject = await oauth_identity_service.acquire_subject_lock(
                    db,
                    tenant_id=tenant_uuid,
                    provider=exact_provider_model,
                    subject=user_info.provider_user_id,
                )
                trusted_user = await oauth_identity_service.resolve_bound_user(
                    db,
                    tenant_id=tenant_uuid,
                    provider=exact_provider_model,
                    subject=oauth_subject,
                )
                if trusted_user is not None:
                    if not trusted_user.is_active:
                        raise HTTPException(status_code=403, detail="Account is disabled")
                    # Validate the claim before any profile or projection write.
                    normalize_oauth_email(user_info.email)
                    trusted_user, _ = await oauth_identity_service.refresh_authoritative_contacts(
                        db,
                        tenant_id=tenant_uuid,
                        provider=exact_provider_model,
                        subject=oauth_subject,
                        user=trusted_user,
                        email=user_info.email,
                        phone=user_info.mobile,
                    )
                    await sso_service.link_identity(
                        db,
                        str(trusted_user.id),
                        self.provider_type,
                        oauth_subject,
                        user_info.raw_data,
                        tenant_id=str(tenant_uuid),
                        provider_model=exact_provider_model,
                    )
                    await self._update_existing_user(
                        db, trusted_user, user_info, refresh_contacts=False
                    )
                    await registration_service.ensure_web_org_member(db, trusted_user)
                    return trusted_user, False

            repaired_user = await self._repair_legacy_dingtalk_oauth_user(
                db,
                tenant_id=tenant_uuid,
                user_info=user_info,
            )
            if repaired_user is not None:
                if not repaired_user.is_active:
                    raise HTTPException(
                        status_code=403,
                        detail="User account is disabled",
                    )
                await sso_service.link_identity(
                    db,
                    str(repaired_user.id),
                    self.provider_type,
                    user_info.provider_user_id,
                    user_info.raw_data,
                    tenant_id=str(tenant_uuid),
                    provider_model=exact_provider_model,
                )
                await registration_service.ensure_web_org_member(db, repaired_user)
                if oauth_subject is not None:
                    await oauth_identity_service.ensure_binding(
                        db,
                        tenant_id=tenant_uuid,
                        provider=exact_provider_model,
                        subject=oauth_subject,
                        user=repaired_user,
                    )
                return repaired_user, False

            exact_user = await sso_service.resolve_user_identity(
                db,
                user_info.provider_user_id,
                self.provider_type,
                tenant_id=str(tenant_uuid),
                identity_data=user_info.raw_data,
                provider_model=exact_provider_model,
            )
            if exact_user is not None:
                if not exact_user.is_active:
                    raise HTTPException(
                        status_code=403, detail="User account is disabled"
                    )
                exact_member = await sso_service.link_identity(
                    db,
                    str(exact_user.id),
                    self.provider_type,
                    user_info.provider_user_id,
                    user_info.raw_data,
                    tenant_id=str(tenant_uuid),
                    provider_model=exact_provider_model,
                )
                await self._update_existing_user(
                    db,
                    exact_user,
                    user_info,
                    authoritative_contacts=True,
                    source_member_id=exact_member.id,
                )
                await registration_service.ensure_web_org_member(db, exact_user)
                if oauth_subject is not None:
                    await oauth_identity_service.ensure_binding(
                        db,
                        tenant_id=tenant_uuid,
                        provider=exact_provider_model,
                        subject=oauth_subject,
                        user=exact_user,
                    )
                return exact_user, False

            identity = await registration_service.find_or_create_identity(
                db,
                email=user_info.email,
                phone=user_info.mobile,
                username=user_info.email.split("@")[0] if user_info.email else None,
                password=None,
                provider=provider_model,
            )

            user = None
            # Persisted OrgMember contact fields are profile data and may be
            # stale.  Cross-provider login reconciliation uses an exact scoped
            # subject binding or the fresh DingTalk repair path above.
            for candidate in (exact_user,):
                if candidate is None or (user is not None and candidate.id == user.id):
                    continue
                user = await canonical_user_resolver.reconcile_identity_user(
                    db,
                    tenant_id=tenant_uuid,
                    identity=identity,
                    candidate_user=candidate,
                )

            created = False
            if user is None:
                user, created = await canonical_user_resolver.get_or_create_tenant_user(
                    db,
                    tenant_id=tenant_uuid,
                    identity=identity,
                    display_name=user_info.name or identity.username or "User",
                    avatar_url=user_info.avatar_url or None,
                    registration_source=self.provider_type,
                )
            if not user.is_active:
                raise HTTPException(status_code=403, detail="User account is disabled")

            # Re-load after a possible SQL-level convergence, then update only
            # non-identity profile fields. Identity claims were already checked
            # together by find_or_create_identity.
            user = (
                await db.execute(
                    select(User)
                    .where(User.id == user.id)
                    .options(selectinload(User.identity))
                )
            ).scalar_one()
            await self._update_existing_user(db, user, user_info)

            await sso_service.link_identity(
                db,
                str(user.id),
                self.provider_type,
                user_info.provider_user_id,
                user_info.raw_data,
                tenant_id=str(tenant_uuid),
                provider_model=exact_provider_model,
            )
            await registration_service.ensure_web_org_member(db, user)
            if oauth_subject is not None:
                await oauth_identity_service.ensure_binding(
                    db,
                    tenant_id=tenant_uuid,
                    provider=exact_provider_model,
                    subject=oauth_subject,
                    user=user,
                )
            return user, created
        except (CanonicalIdentityConflict, CanonicalUserConflict) as exc:
            logger.warning("Enterprise identity reconciliation failed: {}", exc)
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def _repair_legacy_dingtalk_oauth_user(
        self,
        db: AsyncSession,
        *,
        tenant_id,
        user_info: ExternalUserInfo,
    ) -> User | None:
        """Resolve an explicitly scoped OAuth principal to one DingTalk member."""
        if self.provider_type not in {"dingtalk", "oauth2"}:
            return None

        from app.models.org import OrgMember
        from app.services.canonical_user_resolver import CanonicalUserConflict
        from app.services.dingtalk_identity_reconciliation import (
            dingtalk_legacy_identity_reconciler,
            fetch_fresh_dingtalk_claims,
        )

        auth_provider = self.provider
        if auth_provider is None or auth_provider.tenant_id != tenant_id:
            raise CanonicalUserConflict("OAuth provider has no exact tenant scope")

        if self.provider_type == "dingtalk":
            if (
                auth_provider.provider_type != "dingtalk"
                or not auth_provider.is_active
            ):
                return None
            provider = auth_provider
            directory_subject = (
                user_info.provider_union_id
                or user_info.raw_data.get("unionId")
                or user_info.raw_data.get("unionid")
            )
            if not directory_subject:
                return None
            # Keep the direct DingTalk OAuth binding provider-scoped by unionId.
            if not user_info.provider_user_id:
                user_info.provider_user_id = str(directory_subject)
            member_subject_clause = OrgMember.unionid == str(directory_subject)
        else:
            # A generic OAuth subject has no inherent relationship to DingTalk.
            # Cross-provider routing is permitted only by an explicit provider
            # configuration that names the exact directory provider.
            configured_directory_id = (auth_provider.config or {}).get(
                "directory_provider_id"
            )
            if not configured_directory_id or not user_info.provider_user_id:
                return None
            try:
                import uuid

                directory_provider_id = uuid.UUID(str(configured_directory_id))
            except (TypeError, ValueError) as exc:
                raise CanonicalUserConflict(
                    "OAuth directory_provider_id is invalid"
                ) from exc
            provider = await db.get(IdentityProvider, directory_provider_id)
            if (
                provider is None
                or provider.tenant_id != tenant_id
                or provider.provider_type != "dingtalk"
                or not provider.is_active
            ):
                raise CanonicalUserConflict(
                    "OAuth directory_provider_id is not an active tenant DingTalk provider"
                )
            member_subject_clause = (
                OrgMember.external_id == user_info.provider_user_id
            )

        members = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.provider_id == provider.id,
                    member_subject_clause,
                    OrgMember.status == "active",
                )
            )
        ).scalars().all()
        if not members:
            return None
        if len(members) != 1:
            raise CanonicalUserConflict(
                "OAuth subject maps to multiple DingTalk directory members"
            )
        member = members[0]
        if member.user_id is None:
            return None
        linked_user = (
            await db.execute(
                select(User)
                .where(User.id == member.user_id, User.tenant_id == tenant_id)
                .options(selectinload(User.identity))
            )
        ).scalar_one_or_none()
        if linked_user is None:
            return None

        from app.services.canonical_user_resolver import (
            canonical_user_resolver,
        )
        from app.services.registration_service import registration_service

        is_legacy_placeholder = bool(
            linked_user.identity
            and (linked_user.identity.username or "").startswith("dingtalk_")
        )
        if linked_user.identity is not None and not is_legacy_placeholder:
            # The explicit provider-scoped directory route already identifies
            # this formal account. Do not make healthy logins depend on a second
            # DingTalk API call.
            return linked_user

        if is_legacy_placeholder and not bool(
            (provider.config or {}).get(
                "auto_repair_legacy_identity_split",
                False,
            )
        ):
            raise CanonicalUserConflict(
                "legacy DingTalk OAuth principal requires an approved repair"
            )

        if not member.external_id:
            raise CanonicalUserConflict(
                "exact DingTalk directory member has no staff identifier"
            )
        fresh_claims = await fetch_fresh_dingtalk_claims(
            provider,
            member.external_id,
        )
        if fresh_claims is None:
            raise CanonicalUserConflict(
                "fresh DingTalk directory claims are required for legacy OAuth repair"
            )
        if fresh_claims.has_alternate_email_conflict:
            from app.services.canonical_user_resolver import CanonicalIdentityConflict

            raise CanonicalIdentityConflict("DingTalk email and org_email disagree")
        user_info.email = fresh_claims.email or user_info.email
        user_info.mobile = fresh_claims.phone or user_info.mobile

        if linked_user.identity is None:
            await dingtalk_legacy_identity_reconciler.acquire_subject_lock(
                db,
                tenant_id=tenant_id,
                provider_id=provider.id,
                external_id=member.external_id,
            )
            # Re-read after acquiring the shared subject lock. Another entry
            # path may have attached or repaired the member while claims were
            # fetched.
            member = (
                await db.execute(
                    select(OrgMember).where(
                        OrgMember.id == member.id,
                        OrgMember.tenant_id == tenant_id,
                        OrgMember.provider_id == provider.id,
                        OrgMember.external_id == fresh_claims.external_id,
                        OrgMember.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if member is None or member.user_id is None:
                raise CanonicalUserConflict(
                    "exact DingTalk directory member changed during OAuth login"
                )
            linked_user = (
                await db.execute(
                    select(User)
                    .where(
                        User.id == member.user_id,
                        User.tenant_id == tenant_id,
                    )
                    .options(selectinload(User.identity))
                )
            ).scalar_one_or_none()
            if linked_user is None:
                raise CanonicalUserConflict(
                    "exact DingTalk directory user changed during OAuth login"
                )
            if linked_user.identity is not None:
                if (linked_user.identity.username or "").startswith("dingtalk_"):
                    is_legacy_placeholder = True
                else:
                    return linked_user

        if linked_user.identity is None:
            identity = await registration_service.find_or_create_identity(
                db,
                email=fresh_claims.email,
                phone=fresh_claims.phone,
                username=(
                    fresh_claims.email.split("@", 1)[0]
                    if fresh_claims.email
                    else None
                ),
                password=None,
                provider=provider,
            )
            resolved = await canonical_user_resolver.reconcile_identity_user(
                db,
                tenant_id=tenant_id,
                identity=identity,
                candidate_user=linked_user,
            )
            return resolved or linked_user

        outcome = await dingtalk_legacy_identity_reconciler.reconcile(
            db,
            provider=provider,
            org_member=member,
            claims=fresh_claims,
            apply=True,
        )
        if outcome.repaired and outcome.user:
            return outcome.user
        if (
            outcome.status in {"already_repaired", "not_split"}
            and outcome.user
            and outcome.user.identity
            and not (outcome.user.identity.username or "").startswith("dingtalk_")
        ):
            return outcome.user
        raise CanonicalUserConflict(
            f"legacy DingTalk OAuth principal cannot be safely repaired: "
            f"{outcome.reason or outcome.status}"
        )

    async def _ensure_provider(self, db: AsyncSession, tenant_id: str | None = None) -> IdentityProvider:
        """Get or create IdentityProvider record."""
        if self.provider:
            return self.provider

        provider = await get_preferred_identity_provider(
            db,
            self.provider_type,
            tenant_id,
        )

        if not provider:
            provider = IdentityProvider(
                provider_type=self.provider_type,
                name=self.provider_type.capitalize(),
                is_active=True,
                config=self.config,
                tenant_id=tenant_id,
            )
            db.add(provider)
            await db.flush()

        self.provider = provider
        return provider

    async def _find_user_by_legacy_fields(self, db: AsyncSession, user_info: ExternalUserInfo) -> User | None:
        """Find user by legacy provider-specific fields (if any)."""
        return None  # Override in subclasses for backward compatibility

    async def _update_existing_user(
        self,
        db: AsyncSession,
        user: User,
        user_info: ExternalUserInfo,
        *,
        authoritative_contacts: bool = False,
        refresh_contacts: bool = True,
        source_member_id=None,
    ):
        """Update existing user with new info from provider."""
        from app.services.provider_contact_refresh import refresh_provider_user_profile

        await refresh_provider_user_profile(
            db,
            user=user,
            user_info=user_info,
            provider=self.provider,
            authoritative_contacts=authoritative_contacts,
            refresh_contacts=refresh_contacts,
            source_member_id=source_member_id,
        )
        await self._update_legacy_user_fields(user, user_info)

    async def _create_new_user(
        self, db: AsyncSession, user_info: ExternalUserInfo, tenant_id: str | None
    ) -> User:
        """Create new user from external identity."""
        from app.services.registration_service import registration_service
        import uuid
        
        # 1. Prepare user fields and resolve global identity
        effective_id = user_info.provider_user_id or user_info.provider_union_id or "unknown"
        
        identity = await registration_service.find_or_create_identity(
            db,
            email=user_info.email,
            phone=user_info.mobile,
            username=user_info.email.split("@")[0] if user_info.email else None,
            password=None,
            provider=self.provider,
        )

        # 2. Prepare Tenant user fields
        username = user_info.email.split("@")[0] if user_info.email else f"{self.provider_type}_{effective_id[:8]}"

        # Ensure unique username within tenant
        query = (
            select(User)
            .join(User.identity)
            .where(Identity.username == username)
        )
        if tenant_id:
            query = query.where(User.tenant_id == tenant_id)
        existing = await db.execute(query)
        if existing.scalar_one_or_none():
            username = f"{username}_{uuid.uuid4().hex[:6]}"

        # 3. Create TenantUser record
        user = User(
            identity_id=identity.id,
            display_name=user_info.name or username,
            avatar_url=user_info.avatar_url,
            registration_source=self.provider_type,
            tenant_id=tenant_id,
            is_active=True,
        )

        # Set legacy fields if needed
        await self._set_legacy_user_fields(user, user_info)

        db.add(user)
        await db.flush()

        # Preload identity for downstream access
        user.identity = identity
        return user

    async def _update_legacy_user_fields(self, user: User, user_info: ExternalUserInfo):
        """Override in subclass to update provider-specific legacy fields."""
        pass

    async def _set_legacy_user_fields(self, user: User, user_info: ExternalUserInfo):
        """Override in subclass to set provider-specific legacy fields on new user."""
        pass
from app.services.auth_provider_enterprise import (
    DingTalkAuthProvider,
    FeishuAuthProvider,
    WeComAuthProvider,
)
from app.services.auth_provider_oauth import (
    GitHubAuthProvider,
    GoogleAuthProvider,
    GoogleWorkspaceAuthProvider,
    MicrosoftTeamsAuthProvider,
    OAuth2AuthProvider,
)

for _auth_provider_class in (
    FeishuAuthProvider,
    DingTalkAuthProvider,
    WeComAuthProvider,
    OAuth2AuthProvider,
    GoogleWorkspaceAuthProvider,
    MicrosoftTeamsAuthProvider,
    GoogleAuthProvider,
    GitHubAuthProvider,
):
    _auth_provider_class.__module__ = __name__

# Provider class mapping
PROVIDER_CLASSES = {
    "feishu": FeishuAuthProvider,
    "dingtalk": DingTalkAuthProvider,
    "wecom": WeComAuthProvider,
    "oauth2": OAuth2AuthProvider,
    "google_workspace": GoogleWorkspaceAuthProvider,
    "microsoft_teams": MicrosoftTeamsAuthProvider,
    "google": GoogleAuthProvider,
    "github": GitHubAuthProvider,
}
