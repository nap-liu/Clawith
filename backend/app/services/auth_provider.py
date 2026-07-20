"""Generic OAuth/SSO authentication provider framework.

This module provides a base class for all identity providers (Feishu, DingTalk, WeCom, etc.)
and concrete implementations for each supported provider.
"""

from urllib.parse import quote, urlencode

import httpx
from abc import ABC, abstractmethod
from fastapi import HTTPException
from dataclasses import dataclass
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
from loguru import logger


@dataclass
class ExternalUserInfo:
    """Standardized user info from external identity providers."""

    provider_type: str
    provider_union_id: str | None = None
    provider_user_id: str | None = None
    name: str = ""
    email: str = ""
    avatar_url: str = ""
    mobile: str = ""
    raw_data: dict = None

    def __post_init__(self):
        if self.raw_data is None:
            self.raw_data = {}


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
        """Find existing user or create new one via Identity/OrgMember.

        Args:
            db: Database session
            user_info: User info from provider
            tenant_id: Optional tenant ID for association
        """
        from app.services.sso_service import sso_service

        # Ensure provider exists
        await self._ensure_provider(db, tenant_id)

        # Enterprise login has a trusted tenant boundary.  Resolve all strong
        # claims through the shared canonical path so directory/OAuth/IM order
        # cannot create a second tenant User.
        if tenant_id:
            return await self._find_or_create_enterprise_user(
                db, user_info, tenant_id
            )

        # 1. Try lookup via sso_service (which now uses OrgMember)
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
            # 2. Try matching by email/mobile (which now checks Identity too)
            if user_info.email:
                user = await sso_service.match_user_by_email(db, user_info.email, tenant_id)
            if not user and user_info.mobile:
                user = await sso_service.match_user_by_mobile(db, user_info.mobile, tenant_id)
            
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
                 identity = await registration_service.find_or_create_identity(db, email=user_info.email, phone=user_info.mobile)
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
                )
                await registration_service.ensure_web_org_member(db, repaired_user)
                return repaired_user, False

            exact_user = await sso_service.resolve_user_identity(
                db,
                user_info.provider_user_id,
                self.provider_type,
                tenant_id=str(tenant_uuid),
                identity_data=user_info.raw_data,
            )
            if exact_user is not None and not (user_info.email or user_info.mobile):
                if not exact_user.is_active:
                    raise HTTPException(
                        status_code=403, detail="User account is disabled"
                    )
                await self._update_existing_user(db, exact_user, user_info)
                await sso_service.link_identity(
                    db,
                    str(exact_user.id),
                    self.provider_type,
                    user_info.provider_user_id,
                    user_info.raw_data,
                    tenant_id=str(tenant_uuid),
                )
                await registration_service.ensure_web_org_member(db, exact_user)
                return exact_user, False

            identity = await registration_service.find_or_create_identity(
                db,
                email=user_info.email,
                phone=user_info.mobile,
                username=user_info.email.split("@")[0] if user_info.email else None,
                password=None,
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
            )
            await registration_service.ensure_web_org_member(db, user)
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
        self, db: AsyncSession, user: User, user_info: ExternalUserInfo
    ):
        """Update existing user with new info from provider."""
        if user_info.name and not user.display_name:
            user.display_name = user_info.name
        if user_info.avatar_url and not user.avatar_url:
            user.avatar_url = user_info.avatar_url

        # Identity fields are authoritative claims and must be checked together.
        identity = user.identity
        if identity:
            from app.services.canonical_user_resolver import canonical_user_resolver

            claims = await canonical_user_resolver.resolve_identity_claims(
                db,
                email=user_info.email,
                phone=user_info.mobile,
                enrich=True,
            )
            if claims.identity and claims.identity.id != identity.id:
                from app.services.canonical_user_resolver import CanonicalIdentityConflict

                raise CanonicalIdentityConflict(
                    "OAuth claims resolve to another identity"
                )
        # Update legacy fields if applicable
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


class FeishuAuthProvider(BaseAuthProvider):
    """Feishu (Lark) OAuth provider implementation."""

    provider_type = "feishu"

    FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
    FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
    FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.app_id = self.config.get("app_id")
        self.app_secret = self.config.get("app_secret")
        self._app_access_token: str | None = None

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        app_id = self.app_id or ""
        base_url = "https://open.feishu.cn/open-apis/authen/v1/authorize"
        params = f"app_id={app_id}&redirect_uri={redirect_uri}&state={state}"
        return f"{base_url}?{params}"

    async def get_app_access_token(self) -> str:
        """Get or refresh the Feishu app access token.

        Cached in Redis (preferred) with in-memory fallback.
        Key: clawith:token:feishu_tenant:{app_id}
        TTL: 6900s (7200s validity - 5 min early refresh)
        """
        from app.core.token_cache import get_cached_token, set_cached_token

        cache_key = f"clawith:token:feishu_tenant:{self.app_id}"
        cached = await get_cached_token(cache_key)
        if cached:
            self._app_access_token = cached
            return cached

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.FEISHU_APP_TOKEN_URL,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = resp.json()
            token = data.get("app_access_token", "") or data.get("tenant_access_token", "")
            expire = data.get("expire", 7200)
            if token:
                ttl = max(expire - 300, 60)
                await set_cached_token(cache_key, token, ttl)
            self._app_access_token = token
            return token

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        app_token = await self.get_app_access_token()

        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                self.FEISHU_TOKEN_URL,
                json={"grant_type": "authorization_code", "code": code},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            token_data = token_resp.json()
            return token_data.get("data", {})

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient() as client:
            info_resp = await client.get(
                self.FEISHU_USER_INFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            info_data = info_resp.json().get("data", {})
            open_id = info_data.get("open_id", "")
            provider_user_id = ""

            # Resolve stable employee user_id via contact API (open_id is per-app
            # and changes across apps; user_id is org-stable). Without this,
            # cross-app/SSO identity mapping breaks.
            if open_id:
                try:
                    app_token = await self.get_app_access_token()
                    if app_token:
                        contact_resp = await client.get(
                            f"https://open.feishu.cn/open-apis/contact/v3/users/{open_id}",
                            params={"user_id_type": "open_id"},
                            headers={"Authorization": f"Bearer {app_token}"},
                        )
                        contact_data = contact_resp.json()
                        if contact_data.get("code") == 0:
                            contact_user = contact_data.get("data", {}).get("user", {})
                            provider_user_id = contact_user.get("user_id", "")
                            if provider_user_id:
                                info_data["user_id"] = provider_user_id
                            if not info_data.get("email"):
                                info_data["email"] = (
                                    contact_user.get("email")
                                    or contact_user.get("enterprise_email")
                                    or ""
                                )
                            if not info_data.get("mobile"):
                                info_data["mobile"] = contact_user.get("mobile", "")
                except Exception as e:
                    logger.warning(f"Feishu contact lookup failed during SSO user info fetch: {e}")

            provider_user_id = provider_user_id or info_data.get("user_id", "") or open_id
            logger.info(f"Feishu user info: {info_data}")

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=provider_user_id,
                provider_union_id=info_data.get("union_id"),
                name=info_data.get("name", ""),
                email=info_data.get("email", ""),
                avatar_url=info_data.get("avatar_url", ""),
                mobile=info_data.get("mobile", ""),
                raw_data=info_data,
            )

    async def _find_user_by_legacy_fields(self, db: AsyncSession, user_info: ExternalUserInfo) -> User | None:
        """Feishu legacy lookup removed (open_id/union_id no longer stored on User)."""
        return None

    async def _update_legacy_user_fields(self, user: User, user_info: ExternalUserInfo):
        """No-op: legacy Feishu fields removed from User."""
        return

    async def _set_legacy_user_fields(self, user: User, user_info: ExternalUserInfo):
        """No-op: legacy Feishu fields removed from User."""
        return


class DingTalkAuthProvider(BaseAuthProvider):
    """DingTalk OAuth provider implementation."""

    provider_type = "dingtalk"

    DINGTALK_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/userAccessToken"
    DINGTALK_USER_INFO_URL = "https://api.dingtalk.com/v1.0/contact/users/me"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.app_key = self.config.get("app_key")
        self.app_secret = self.config.get("app_secret")
        self.corp_id = self.config.get("corp_id")

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        app_id = self.app_key or ""
        base_url = "https://login.dingtalk.com/oauth2/auth"
        from urllib.parse import quote
        # Contact.User.Read is required for GET /v1.0/contact/users/me (user info on callback)
        # contact.user.mobile requires the fieldMobile permission in DingTalk console
        # fieldEmail requires the fieldEmail permission in DingTalk console
        scope = "openid corpid Contact.User.Read fieldEmail contact.user.mobile"
        params = (
            f"client_id={app_id}&redirect_uri={quote(redirect_uri)}&"
            f"state={state}&response_type=code&scope={quote(scope)}&prompt=consent"
        )
        # corp_id is optional: restricts the login page to a specific enterprise.
        # If not configured, DingTalk shows a company picker (still works for SSO).
        if self.corp_id:
            params = f"corpId={self.corp_id}&" + params
        return f"{base_url}?{params}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.DINGTALK_TOKEN_URL,
                json={
                    "clientId": self.app_key,
                    "clientSecret": self.app_secret,
                    "code": code,
                    "grantType": "authorization_code",
                },
            )
            resp_data = resp.json()
            if resp.status_code != 200:
                logger.error(f"DingTalk token exchange failed (HTTP {resp.status_code}): {resp_data}")
                return {}

            # New DingTalk OAuth2 returns flat JSON with camelCase fields
            return {
                "access_token": resp_data.get("accessToken"),
                "refresh_token": resp_data.get("refreshToken"),
                "expires_in": resp_data.get("expireIn"),
            }

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient() as client:
            headers = {"x-acs-dingtalk-access-token": access_token}
            info_resp = await client.get(self.DINGTALK_USER_INFO_URL, headers=headers)
            info_data = info_resp.json()
            if info_resp.status_code != 200:
                # Common error: errCode=403 means Contact.User.Read scope not granted.
                # Ensure 'Contact.User.Read' is included in the OAuth scope AND
                # that the app has been authorized by the employee in the login flow.
                err_msg = info_data.get('message') or info_data.get('errmsg') or str(info_data)
                logger.error(
                    f"DingTalk user info fetch failed (HTTP {info_resp.status_code}): {info_data}. "
                    "This usually means the 'Contact.User.Read' OAuth scope is missing from "
                    "the authorization URL, or the app lacks the corresponding permission."
                )
                raise Exception(f"Failed to fetch user info: {err_msg}")

            # DingTalk new OAuth2 returns openId, unionId, nick, avatarUrl, mobile, email
            logger.info(f"DingTalk user info: {info_data}")
            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=info_data.get("unionId"),
                provider_union_id=info_data.get("unionId"),
                name=info_data.get("nick", ""),
                email=info_data.get("email", ""),
                avatar_url=info_data.get("avatarUrl", ""),
                mobile=info_data.get("mobile", ""),
                raw_data=info_data,
            )


class WeComAuthProvider(BaseAuthProvider):
    """WeCom (Enterprise WeChat) OAuth provider implementation.

    Authentication flow:
    1. gettoken (corp_id + secret) -> access_token
    2. auth/getuserinfo (access_token + OAuth code) -> userid + user_ticket
    3. auth/getuserdetail (access_token + user_ticket) -> avatar, email, mobile
    4. user/get (access_token + userid) -> name, position (non-sensitive fields)

    Note: Steps 3 and 4 require the calling server IP to be whitelisted in the
    WeCom self-built app settings. This is a one-time setup per tenant.
    (Contrast with getuserinfo in step 2, which only requires trusted domain,
    not IP whitelist.)
    """

    provider_type = "wecom"

    # All WeCom self-built app API calls go to qyapi.weixin.qq.com
    # The old api.weixin.qq.com endpoints are legacy WeCom Public Account APIs
    # and no longer work for self-built apps.
    WECOM_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    WECOM_USER_INFO_URL = "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo"
    WECOM_USER_DETAIL_URL = "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserdetail"
    WECOM_USER_GET_URL = "https://qyapi.weixin.qq.com/cgi-bin/user/get"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        # corp_id and agent_id are used for the OAuth redirect URL
        self.corp_id = self.config.get("corp_id") or self.config.get("app_id")
        # secret is the self-built app's AgentSecret (not the contact-sync secret)
        self.secret = self.config.get("secret") or self.config.get("app_secret")
        self.agent_id = self.config.get("agent_id")

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Construct the WeCom web-login SSO redirect URL.

        Uses the 'Scan QR Code to Login' flow (CorpPinCorp), which redirects users
        to authenticate with their WeCom account then returns them to redirect_uri
        with a code parameter.
        """
        from urllib.parse import quote
        base_url = "https://open.work.weixin.qq.com/wwlogin/sso/login"
        params = (
            f"loginType=CorpPinCorp"
            f"&appid={self.corp_id}"
            f"&agentid={self.agent_id}"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&state={state}"
        )
        return f"{base_url}?{params}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        """Exchange OAuth code for a packed token string containing all user data.

        Three sequential API calls:
          1. gettoken -> access_token
          2. auth/getuserinfo (code) -> userid + user_ticket
          3a. auth/getuserdetail (user_ticket) -> avatar, email, mobile [sensitive]
          3b. user/get (userid) -> name, position [non-sensitive, best-effort]

        Returns a packed JSON dict disguised as the access_token field so
        the existing BaseAuthProvider interface (get_user_info) can consume it.
        """
        import json

        async with httpx.AsyncClient(timeout=10) as client:
            # Step 1: Get app-level access token using corp credentials
            token_resp = await client.get(
                self.WECOM_TOKEN_URL,
                params={"corpid": self.corp_id, "corpsecret": self.secret},
            )
            token_data = token_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                logger.error(f"[WeCom SSO] gettoken failed: {token_data}")
                return {}

            # Step 2: Exchange OAuth code for userid + user_ticket
            # auth/getuserinfo returns userid (lowercase 'u') for internal employees.
            # user_ticket is a temporary credential (valid ~1800s) representing
            # the employee's own OAuth authorization, required for sensitive fields.
            info_resp = await client.get(
                self.WECOM_USER_INFO_URL,
                params={"access_token": access_token, "code": code},
            )
            info_data = info_resp.json()
            # The key is lowercase 'userid' in the new auth endpoint (not 'UserId')
            userid = info_data.get("userid") or info_data.get("UserId", "")
            user_ticket = info_data.get("user_ticket", "")
            if not userid:
                logger.error(f"[WeCom SSO] getuserinfo missing userid: {info_data}")
                return {}

            # Step 3a: Fetch sensitive profile fields using user_ticket.
            # Since June 2022, new self-built apps cannot get avatar/email/mobile
            # from user/get directly. The user_ticket (from OAuth consent) unlocks them.
            # Returns: userid, gender, avatar, qr_code, mobile, email, biz_mail, address
            sensitive_data: dict = {}
            if user_ticket:
                try:
                    detail_resp = await client.post(
                        self.WECOM_USER_DETAIL_URL,
                        params={"access_token": access_token},
                        json={"user_ticket": user_ticket},
                    )
                    detail_json = detail_resp.json()
                    if detail_json.get("errcode") == 0:
                        sensitive_data = detail_json
                        logger.info(f"[WeCom SSO] getuserdetail succeeded for {userid}")
                    else:
                        logger.warning(f"[WeCom SSO] getuserdetail failed: {detail_json}")
                except Exception as e:
                    logger.warning(f"[WeCom SSO] getuserdetail error: {e}")
            else:
                logger.info(
                    f"[WeCom SSO] No user_ticket for {userid}; "
                    "sensitive fields (avatar/email/mobile) will be unavailable. "
                    "Ensure the WeCom app has 'snsapi_privateinfo' scope."
                )

            # Step 3b: Fetch non-sensitive profile fields from user/get (name, position).
            # These fields are NOT restricted by the June 2022 policy and are available
            # via the standard app access token (IP whitelist required).
            basic_data: dict = {}
            try:
                get_resp = await client.get(
                    self.WECOM_USER_GET_URL,
                    params={"access_token": access_token, "userid": userid},
                )
                get_json = get_resp.json()
                if get_json.get("errcode") == 0:
                    basic_data = get_json
                    logger.info(f"[WeCom SSO] user/get succeeded for {userid}")
                else:
                    logger.warning(f"[WeCom SSO] user/get failed: {get_json}")
            except Exception as e:
                logger.warning(f"[WeCom SSO] user/get error: {e}")

            # Pack all data for get_user_info() to consume
            packed_token = json.dumps({
                "userid": userid,
                "sensitive": sensitive_data,  # from getuserdetail (avatar, email, mobile)
                "basic": basic_data,           # from user/get (name, position)
            })
            return {"access_token": packed_token}

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        """Parse the packed token into a standardized ExternalUserInfo.

        Priority for each field:
          - email: sensitive_data (getuserdetail) > biz_mail > basic_data (user/get)
          - avatar: sensitive_data > basic_data
          - mobile: sensitive_data only (restricted post-2022 in user/get)
          - name: basic_data (non-sensitive, from user/get)
        """
        import json
        try:
            data = json.loads(access_token)
            userid = data.get("userid", "")
            sensitive = data.get("sensitive", {})
            basic = data.get("basic", {})

            # Name from user/get (non-sensitive, always available when IP is whitelisted)
            name = basic.get("name") or f"WeCom {userid}"

            # Email: prefer personal email from getuserdetail, fall back to biz_mail
            email = (
                sensitive.get("email")
                or sensitive.get("biz_mail")
                or basic.get("email")
                or basic.get("biz_mail")
                or ""
            )

            # Avatar from getuserdetail (restricted post-2022 in user/get)
            avatar_url = sensitive.get("avatar") or basic.get("avatar") or ""

            # Mobile only from getuserdetail (restricted post-2022 in user/get)
            mobile = sensitive.get("mobile") or ""

            # Merge raw_data so OrgMember has full context
            raw = {**basic, **sensitive, "userid": userid}

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=userid,
                name=name,
                email=email,
                avatar_url=avatar_url,
                mobile=mobile,
                raw_data=raw,
            )
        except Exception as e:
            logger.error(f"[WeCom SSO] get_user_info parse error: {e}")
            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id="",
                name="",
                raw_data={"error": str(e)},
            )



class OAuth2AuthProvider(BaseAuthProvider):
    """Generic OAuth2 provider implementation (RFC 6749 Authorization Code flow)."""

    provider_type = "oauth2"

    def __init__(self, provider=None, config=None):
        super().__init__(provider, config)
        self.client_id = self.config.get("client_id") or self.config.get("app_id", "")
        self.client_secret = self.config.get("client_secret") or self.config.get("app_secret", "")
        self.authorize_url = self.config.get("authorize_url", "")
        self.scope = self.config.get("scope", "")
        
        # 自动推导 token_url 和 user_info_url（如果为空）
        base = self.authorize_url.rsplit("/", 1)[0] if self.authorize_url else ""
        self.token_url = self.config.get("token_url") or f"{base}/token"
        self.user_info_url = self.config.get("user_info_url") or f"{base}/userinfo"

        # 字段映射配置（用户自定义）
        self.field_mapping = self.config.get("field_mapping") or {}

        # 标准 OIDC 字段 fallback 顺序
        self.FIELD_DEFAULTS = {
            "user_id": ["sub", "userId", "id"],
            "name": ["name", "userName", "preferred_username", "nickname"],
            "email": ["email"],
            "mobile": ["phone_number", "mobile", "phone"],
            "avatar": ["picture", "avatar_url", "avatar"],
        }

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        from urllib.parse import quote
        params = (
            f"response_type=code"
            f"&client_id={quote(self.client_id)}"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&scope={quote(self.scope)}"
            f"&state={state}"
        )
        return f"{self.authorize_url}?{params}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        import base64
        credentials = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        data = {
            "grant_type": "authorization_code",
            "code": code,
        }
        if redirect_uri and self.config.get("token_exchange_redirect_uri", True) is not False:
            data["redirect_uri"] = redirect_uri
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.token_url,
                headers={
                    "Authorization": f"Basic {credentials}",
                },
                data=data,
            )
            if resp.status_code != 200:
                logger.error("OAuth2 token exchange failed (HTTP {})", resp.status_code)
                return {}
            return resp.json()

    def _get_field(self, data: dict, field_key: str) -> str:
        """Get a field value using user-defined mapping first, then standard OIDC fallbacks."""
        # 1. 优先用用户配置的映射字段
        custom_key = self.field_mapping.get(field_key)
        if custom_key and data.get(custom_key):
            return str(data[custom_key])
        # 2. 依次尝试标准 fallback 字段
        for std_key in self.FIELD_DEFAULTS.get(field_key, []):
            if data.get(std_key):
                return str(data[std_key])
        return ""

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.user_info_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp_data = resp.json()
            
            # Handle case where userinfo returns None or empty
            if not resp_data:
                logger.warning(f"OAuth2 userinfo returned empty/null for {self.provider_type}")
                info = {}
            elif "data" in resp_data and isinstance(resp_data["data"], dict):
                info = resp_data["data"]
            else:
                info = resp_data
            
            logger.info(f"OAuth2 user info: {info}")
            
            # 通用字段解析（优先用户自定义映射，再 fallback 到标准字段）
            user_id = self._get_field(info, "user_id")
            name = self._get_field(info, "name")
            email = self._get_field(info, "email")
            mobile = self._get_field(info, "mobile")
            
            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=str(user_id),
                name=name,
                email=email,
                mobile=mobile,
                raw_data=info,
            )

    async def get_user_info_from_token_data(self, token_data: dict) -> ExternalUserInfo:
        """Extract user info from token exchange response (fallback when userinfo endpoint fails)."""
        info = token_data.copy()
        if "userInfo" in info and isinstance(info["userInfo"], dict):
            info = {**info, **info["userInfo"]}
        logger.info(f"OAuth2 user info from token_data: {info}")
        user_id = self._get_field(info, "user_id") or info.get("openid", "")
        name = self._get_field(info, "name")
        email = self._get_field(info, "email")
        mobile = self._get_field(info, "mobile")
        return ExternalUserInfo(
            provider_type=self.provider_type,
            provider_user_id=str(user_id),
            name=name,
            email=email,
            mobile=mobile,
            raw_data=info,
        )

    async def _create_new_user(self, db, user_info, tenant_id):
        """Override to use provider_user_id as username for OAuth2."""
        from app.services.registration_service import registration_service
        from app.models.user import Identity as IdentityModel

        # 优先用 provider_user_id（如 userId="zhangsan"），再用 email 前缀，最后 fallback
        username = (
            user_info.provider_user_id
            or (user_info.email.split("@")[0] if user_info.email else None)
            or f"oauth2_user"
        )

        # Check username uniqueness via Identity
        existing = await db.execute(
            select(User).join(User.identity).where(
                Identity.username == username,
            )
        )
        if existing.scalar_one_or_none():
            logger.error(
                f"[OAuth2] Username conflict detected: {username} already exists in tenant {tenant_id}. "
                f"This should not happen if find_or_create_user match chain is working correctly."
            )
            raise HTTPException(
                status_code=409,
                detail=f"Username '{username}' already exists. Please contact administrator."
            )

        email = user_info.email or f"{username}@oauth2.local"

        # Create or find Identity
        identity = await registration_service.find_or_create_identity(
            db,
            email=email,
            phone=user_info.mobile,
            username=username,
            password=None,
        )

        # Create tenant-scoped User
        user = User(
            identity_id=identity.id,
            display_name=user_info.name or username,
            avatar_url=user_info.avatar_url,
            registration_source=self.provider_type,
            tenant_id=tenant_id,
            is_active=True,
        )

        db.add(user)
        await db.flush()

        # Preload identity
        user.identity = identity
        return user

class GoogleWorkspaceAuthProvider(BaseAuthProvider):
    """Google Workspace OAuth provider implementation for SSO login."""

    provider_type = "google_workspace"

    GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_USER_INFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
    GOOGLE_SSO_SCOPE = "openid email profile"
    GOOGLE_ADMIN_SCOPES = [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/admin.directory.user.readonly",
        "https://www.googleapis.com/auth/admin.directory.orgunit.readonly",
    ]

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.client_id = self.config.get("client_id") or self.config.get("sso_client_id") or self.config.get("app_id")
        self.client_secret = self.config.get("client_secret") or self.config.get("sso_client_secret") or self.config.get("app_secret")
        self.scope = self.config.get("sso_scope") or self.config.get("scope") or self.GOOGLE_SSO_SCOPE

    def _build_authorization_url(
        self,
        redirect_uri: str,
        state: str,
        *,
        scopes: str | list[str] | None = None,
        access_type: str = "online",
        prompt: str = "select_account",
    ) -> str:
        from urllib.parse import quote

        scope_value = scopes or self.scope
        if isinstance(scope_value, list):
            scope_value = " ".join(scope_value)

        self.config["redirect_uri"] = redirect_uri
        params = (
            f"client_id={quote(self.client_id or '')}"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&response_type=code"
            f"&scope={quote(scope_value)}"
            f"&state={quote(state or '')}"
            f"&access_type={quote(access_type)}"
            f"&include_granted_scopes=true"
            f"&prompt={quote(prompt)}"
        )
        return f"{self.GOOGLE_AUTHORIZE_URL}?{params}"

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        return self._build_authorization_url(
            redirect_uri,
            state,
            scopes=self.scope,
            access_type="online",
            prompt="select_account",
        )

    async def get_admin_authorization_url(self, redirect_uri: str, state: str) -> str:
        return self._build_authorization_url(
            redirect_uri,
            state,
            scopes=self.GOOGLE_ADMIN_SCOPES,
            access_type="offline",
            prompt="consent",
        )

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                self.GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri or self.config.get("redirect_uri"),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()

    async def refresh_access_token(self, refresh_token: str) -> dict:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                self.GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()

    async def fetch_openid_profile(self, access_token: str) -> dict:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.get(
                self.GOOGLE_USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        info = await self.fetch_openid_profile(access_token)
        return ExternalUserInfo(
            provider_type=self.provider_type,
            provider_user_id=info.get("sub", ""),
            name=info.get("name", "") or info.get("email", ""),
            email=info.get("email", ""),
            avatar_url=info.get("picture", ""),
            raw_data=info,
        )


class MicrosoftTeamsAuthProvider(BaseAuthProvider):
    """Microsoft Teams OAuth provider implementation."""

    provider_type = "microsoft_teams"

    # Will be implemented when needed
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        raise NotImplementedError("Microsoft Teams OAuth not yet implemented")

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        raise NotImplementedError("Microsoft Teams OAuth not yet implemented")

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        raise NotImplementedError("Microsoft Teams OAuth not yet implemented")


class GoogleAuthProvider(BaseAuthProvider):
    """Google OAuth provider implementation."""

    provider_type = "google"

    GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_USER_INFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.client_id = self.config.get("client_id") or self.config.get("app_id")
        self.client_secret = self.config.get("client_secret") or self.config.get("app_secret")
        self.scope = self.config.get("scope") or "openid profile email"

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self.client_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "access_type": "offline",
            "prompt": "consent",
        }
        if state:
            params["state"] = state
        return f"{self.GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                self.GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri or "",
                    "grant_type": "authorization_code",
                },
            )
            data = resp.json()
            if resp.status_code != 200:
                logger.error(f"Google token exchange failed (HTTP {resp.status_code}): {data}")
                return {}
            return data

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.get(
                self.GOOGLE_USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = resp.json()
            if resp.status_code != 200:
                raise Exception(data.get("error_description") or data.get("error") or "Failed to fetch Google user info")

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=data.get("sub", ""),
                name=data.get("name", ""),
                email=data.get("email", ""),
                avatar_url=data.get("picture", ""),
                raw_data=data,
            )


class GitHubAuthProvider(BaseAuthProvider):
    """GitHub OAuth provider implementation."""

    provider_type = "github"

    GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
    GITHUB_USER_INFO_URL = "https://api.github.com/user"
    GITHUB_EMAILS_URL = "https://api.github.com/user/emails"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.client_id = self.config.get("client_id") or self.config.get("app_id")
        self.client_secret = self.config.get("client_secret") or self.config.get("app_secret")
        self.scope = self.config.get("scope") or "read:user user:email"

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self.client_id or "",
            "redirect_uri": redirect_uri,
            "scope": self.scope,
        }
        if state:
            params["state"] = state
        return f"{self.GITHUB_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                self.GITHUB_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                },
            )
            data = resp.json()
            if resp.status_code != 200:
                logger.error(f"GitHub token exchange failed (HTTP {resp.status_code}): {data}")
                return {}
            return data

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            user_resp = await client.get(self.GITHUB_USER_INFO_URL, headers=headers)
            user_data = user_resp.json()
            if user_resp.status_code != 200:
                raise Exception(user_data.get("message") or "Failed to fetch GitHub user info")

            email = user_data.get("email") or ""
            if not email:
                emails_resp = await client.get(self.GITHUB_EMAILS_URL, headers=headers)
                emails_data = emails_resp.json()
                if emails_resp.status_code == 200 and isinstance(emails_data, list):
                    primary = next((item for item in emails_data if item.get("primary")), None)
                    verified = next((item for item in emails_data if item.get("verified")), None)
                    fallback = primary or verified or (emails_data[0] if emails_data else {})
                    email = fallback.get("email", "")

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=str(user_data.get("id", "")),
                name=user_data.get("name") or user_data.get("login") or "",
                email=email,
                avatar_url=user_data.get("avatar_url", ""),
                raw_data=user_data,
            )


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
