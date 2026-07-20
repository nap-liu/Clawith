"""Strict recovery for legacy DingTalk directory identity splits."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import uuid

import httpx
from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio.engine import AsyncConnection, AsyncEngine
from sqlalchemy.orm import selectinload

from app.models.audit import AuditLog
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.user import Identity, User
from app.services.canonical_user_resolver import (
    CanonicalIdentityConflict,
    CanonicalUserConflict,
    canonical_user_resolver,
    normalize_email,
    normalize_phone,
)
from app.services.directory_identity_claims import VerifiedDirectoryClaims


@dataclass(slots=True)
class LegacySplitReconciliation:
    status: str
    user: User | None = None
    source_user_id: uuid.UUID | None = None
    target_user_id: uuid.UUID | None = None
    reason: str | None = None

    @property
    def repaired(self) -> bool:
        return self.status == "repaired"

    @property
    def candidate(self) -> bool:
        return self.status in {"safe_candidate", "unsafe_candidate"}


class DingTalkLegacyIdentityReconciler:
    """Merge only the exact synthetic-identity shape created by old org sync."""

    _ALLOWED_SOURCE_VALUES = {
        None,
        "dingtalk",
        "dingtalk_channel",
        "dingtalk_org_sync",
    }

    async def reconcile(
        self,
        db: AsyncSession,
        *,
        provider: IdentityProvider,
        org_member: OrgMember,
        claims: VerifiedDirectoryClaims,
        apply: bool,
        lock_already_held: bool = False,
    ) -> LegacySplitReconciliation:
        tenant_id = provider.tenant_id or org_member.tenant_id
        if (
            provider.provider_type != "dingtalk"
            or tenant_id is None
            or org_member.tenant_id != tenant_id
            or not claims.matches_scope(
                tenant_id=tenant_id,
                provider_id=provider.id,
                external_id=org_member.external_id or "",
            )
        ):
            return LegacySplitReconciliation(
                status="not_applicable",
                reason="claims_scope_mismatch",
            )
        if claims.has_alternate_email_conflict:
            raise CanonicalIdentityConflict("DingTalk email and org_email disagree")
        # Automatic legacy merge requires a current corporate mailbox that
        # points to the formal Identity. A phone-only match is not sufficient
        # because mobile numbers can be reassigned.
        if not claims.external_id or not claims.email:
            return LegacySplitReconciliation(
                status="not_applicable",
                reason="missing_fresh_corporate_email",
            )

        if not lock_already_held:
            await self.acquire_subject_lock(
                db,
                tenant_id=tenant_id,
                provider_id=provider.id,
                external_id=claims.external_id,
            )

        member = (
            await db.execute(
                select(OrgMember)
                .where(
                    OrgMember.id == org_member.id,
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == claims.external_id,
                    OrgMember.status == "active",
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if member is None or member.user_id is None:
            return LegacySplitReconciliation(
                status="not_applicable",
                reason="active_exact_member_missing",
            )

        source = await self._load_user(db, member.user_id, tenant_id)
        if source is None:
            return LegacySplitReconciliation(
                status="not_applicable",
                reason="source_user_missing",
            )

        email_identity = await self._find_identity_by_email(db, claims.email)
        phone_identity = await self._find_identity_by_phone(db, claims.phone)
        claim_identities = {
            identity.id: identity
            for identity in (email_identity, phone_identity)
            if identity is not None
        }

        if source.identity_id is None:
            return LegacySplitReconciliation(
                status="not_applicable",
                user=source,
                source_user_id=source.id,
                reason="source_is_identityless",
            )
        target_identities = [
            identity
            for identity in claim_identities.values()
            if identity.id != source.identity_id
        ]
        if not target_identities:
            return LegacySplitReconciliation(
                status="not_split",
                user=source,
                source_user_id=source.id,
                reason="fresh_claims_do_not_point_to_formal_target",
            )
        if len(target_identities) != 1:
            raise CanonicalIdentityConflict(
                "fresh DingTalk claims point to multiple target identities"
            )

        target_identity = target_identities[0]
        if (
            email_identity
            and phone_identity
            and email_identity.id != phone_identity.id
            and source.identity_id not in {email_identity.id, phone_identity.id}
        ):
            raise CanonicalIdentityConflict(
                "fresh DingTalk email and phone point to unrelated identities"
            )

        target_users = (
            await db.execute(
                select(User)
                .where(
                    User.tenant_id == tenant_id,
                    User.identity_id == target_identity.id,
                )
                .options(selectinload(User.identity))
            )
        ).scalars().all()
        if len(target_users) != 1:
            return LegacySplitReconciliation(
                status="unsafe_candidate",
                source_user_id=source.id,
                reason="formal_identity_has_no_unique_tenant_user",
            )
        target = target_users[0]

        await self._lock_rows(
            db,
            user_ids=[source.id, target.id],
            identity_ids=[source.identity_id, target.identity_id],
        )
        source = await self._load_user(db, source.id, tenant_id)
        target = await self._load_user(db, target.id, tenant_id)
        if source is None:
            # A concurrent request completed the exact same merge.
            aligned = await self._load_user(db, member.user_id, tenant_id)
            return LegacySplitReconciliation(
                status="already_repaired",
                user=aligned,
                target_user_id=aligned.id if aligned else None,
            )
        if target is None or source.identity is None or target.identity is None:
            raise CanonicalUserConflict("legacy split rows changed while locked")

        unsafe_reason = await self._unsafe_reason(
            db,
            source=source,
            target=target,
            claims=claims,
        )
        if unsafe_reason:
            return LegacySplitReconciliation(
                status="unsafe_candidate",
                source_user_id=source.id,
                target_user_id=target.id,
                reason=unsafe_reason,
            )
        if not apply:
            return LegacySplitReconciliation(
                status="safe_candidate",
                user=target,
                source_user_id=source.id,
                target_user_id=target.id,
            )

        source_identity = source.identity
        source_user_id = source.id
        target_user_id = target.id

        source.identity_id = None
        source.identity = None
        await db.flush()

        target = await canonical_user_resolver.merge_identityless_user(
            db,
            source=source,
            target=target,
        )

        await db.delete(source_identity)
        await db.flush()

        if claims.email:
            target.identity.email = claims.email
            target.identity.email_verified = True
        if claims.phone:
            target.identity.phone = claims.phone
        await db.flush()

        member.user_id = target.id
        db.add(
            AuditLog(
                user_id=target.id,
                action="dingtalk_legacy_identity_split_repaired",
                details={
                    "tenant_id": str(tenant_id),
                    "provider_id": str(provider.id),
                    "org_member_id": str(member.id),
                    "source_user_id": str(source_user_id),
                    "target_user_id": str(target_user_id),
                    "claims_source": claims.source,
                    "has_email": bool(claims.email),
                    "has_mobile": bool(claims.phone),
                },
            )
        )
        await db.flush()
        logger.info(
            "Repaired legacy DingTalk identity split member_id={} source_user_id={} "
            "target_user_id={}",
            member.id,
            source_user_id,
            target_user_id,
        )
        return LegacySplitReconciliation(
            status="repaired",
            user=target,
            source_user_id=source_user_id,
            target_user_id=target_user_id,
        )

    async def acquire_subject_lock(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider_id: uuid.UUID,
        external_id: str,
    ) -> None:
        # PostgreSQL derives the same signed 64-bit key on every process and
        # replica.  Python's randomized hash() must never be used here.
        stable_key = f"{tenant_id}:{provider_id}:{external_id}"
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": stable_key},
        )

    @asynccontextmanager
    async def session_subject_lock(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider_id: uuid.UUID,
        external_id: str,
    ):
        """Hold one subject lock only for a long-transaction sync member.

        Full directory sync commits once at the end, so transaction-level
        advisory locks would accumulate for every employee. A paired session
        lock is released in ``finally`` after this member, while the outer
        database transaction remains open.
        """
        bind = db.bind
        if isinstance(bind, AsyncConnection):
            lock_engine = bind.engine
        elif isinstance(bind, AsyncEngine):
            lock_engine = bind
        else:
            raise RuntimeError("DingTalk sync requires an async database bind")

        stable_key = f"{tenant_id}:{provider_id}:{external_id}"
        # Use a dedicated connection for the session lock. If a member write
        # aborts the sync savepoint, this connection remains usable and can
        # still execute the matching unlock in ``finally``.
        async with lock_engine.connect() as lock_connection:
            await lock_connection.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"),
                {"key": stable_key},
            )
            try:
                yield
            finally:
                unlocked = await lock_connection.scalar(
                    text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                    {"key": stable_key},
                )
                if not unlocked:
                    logger.error(
                        "Failed to release DingTalk session subject lock "
                        "tenant_id={} provider_id={}",
                        tenant_id,
                        provider_id,
                    )

    async def _lock_rows(
        self,
        db: AsyncSession,
        *,
        user_ids: list[uuid.UUID],
        identity_ids: list[uuid.UUID],
    ) -> None:
        await db.execute(
            select(User)
            .where(User.id.in_(sorted(set(user_ids))))
            .order_by(User.id)
            .with_for_update()
        )
        await db.execute(
            select(Identity)
            .where(Identity.id.in_(sorted(set(identity_ids))))
            .order_by(Identity.id)
            .with_for_update()
        )

    async def _load_user(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> User | None:
        return (
            await db.execute(
                select(User)
                .where(User.id == user_id, User.tenant_id == tenant_id)
                .options(selectinload(User.identity))
            )
        ).scalar_one_or_none()

    async def _find_identity_by_email(
        self,
        db: AsyncSession,
        email: str | None,
    ) -> Identity | None:
        if not email:
            return None
        rows = (
            await db.execute(
                select(Identity)
                .where(func.lower(func.btrim(Identity.email)) == email)
                .limit(2)
            )
        ).scalars().all()
        if len(rows) > 1:
            raise CanonicalIdentityConflict("email maps to multiple identities")
        return rows[0] if rows else None

    async def _find_identity_by_phone(
        self,
        db: AsyncSession,
        phone: str | None,
    ) -> Identity | None:
        if not phone:
            return None
        rows = (
            await db.execute(
                select(Identity)
                .where(
                    func.regexp_replace(
                        Identity.phone,
                        r"[[:space:]+-]",
                        "",
                        "g",
                    )
                    == phone
                )
                .limit(2)
            )
        ).scalars().all()
        if len(rows) > 1:
            raise CanonicalIdentityConflict("phone maps to multiple identities")
        return rows[0] if rows else None

    async def _unsafe_reason(
        self,
        db: AsyncSession,
        *,
        source: User,
        target: User,
        claims: VerifiedDirectoryClaims,
    ) -> str | None:
        source_identity = source.identity
        target_identity = target.identity
        if source_identity is None or target_identity is None:
            return "missing_identity"
        if source.id == target.id or source.tenant_id != target.tenant_id:
            return "invalid_user_pair"
        if not source.is_active or not target.is_active:
            return "inactive_user"
        if source.role != "member":
            return "source_has_privileged_role"
        if source.source not in self._ALLOWED_SOURCE_VALUES:
            return "source_origin_is_not_legacy_dingtalk"
        if source.registration_source not in self._ALLOWED_SOURCE_VALUES:
            return "source_registration_is_not_legacy_dingtalk"
        if not (source_identity.username or "").startswith("dingtalk_"):
            return "source_username_is_not_legacy_dingtalk"
        if source_identity.email and not normalize_email(
            source_identity.email
        ).endswith(".local"):
            return "source_has_formal_email"
        if source_identity.password_hash:
            return "source_has_local_password"
        if source_identity.is_platform_admin:
            return "source_is_platform_admin"
        source_phone = normalize_phone(source_identity.phone)
        if claims.phone and source_phone != claims.phone:
            return "source_mobile_does_not_match_fresh_mobile"
        if (
            source.quota_messages_used
            or source.quota_message_limit != 50
            or source.quota_message_period != "permanent"
            or source.quota_max_agents != 2
            or source.quota_agent_ttl_hours != 0
        ):
            return "source_has_non_default_quota"
        if (target_identity.username or "").startswith("dingtalk_"):
            return "target_is_placeholder"
        target_email = normalize_email(target_identity.email)
        target_phone = normalize_phone(target_identity.phone)
        if not target_email or target_email.endswith(".local"):
            return "target_has_no_formal_email"
        if claims.email and target_email != claims.email:
            return "target_email_conflicts"
        if claims.phone and target_phone and target_phone != claims.phone:
            return "target_phone_conflicts"

        identity_user_count = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.identity_id == source_identity.id)
        )
        if identity_user_count != 1:
            return "source_identity_is_shared"

        has_pat = await db.scalar(
            text(
                "SELECT EXISTS("
                "SELECT 1 FROM personal_access_tokens WHERE user_id = :user_id"
                ")"
            ),
            {"user_id": source.id},
        )
        if has_pat:
            return "source_has_personal_access_token"

        has_interactive_session = await db.scalar(
            text(
                "SELECT EXISTS("
                "SELECT 1 FROM chat_sessions "
                "WHERE user_id = :user_id "
                "AND source_channel IN ('web', 'wechat_miniprogram')"
                ")"
            ),
            {"user_id": source.id},
        )
        if has_interactive_session:
            return "source_has_interactive_session"
        return None


dingtalk_legacy_identity_reconciler = DingTalkLegacyIdentityReconciler()


async def fetch_fresh_dingtalk_claims(
    provider: IdentityProvider,
    external_id: str,
) -> VerifiedDirectoryClaims | None:
    """Fetch current raw DingTalk fields without collapsing email variants."""
    if (
        provider.provider_type != "dingtalk"
        or provider.tenant_id is None
        or not external_id
    ):
        return None
    config = provider.config or {}
    app_key = config.get("app_key") or config.get("appkey") or config.get("app_id")
    app_secret = (
        config.get("app_secret")
        or config.get("appsecret")
        or config.get("app_secret_key")
    )
    if not app_key or not app_secret:
        return None

    from app.services.dingtalk_token import dingtalk_token_manager

    token = await dingtalk_token_manager.get_token(str(app_key), str(app_secret))
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://oapi.dingtalk.com/topapi/v2/user/get",
                params={"access_token": token},
                json={"userid": external_id, "language": "zh_CN"},
            )
            data = response.json()
    except Exception as exc:
        logger.warning(
            "Fresh DingTalk identity lookup failed provider_id={} error_type={}",
            provider.id,
            type(exc).__name__,
        )
        return None
    if data.get("errcode") != 0 or not isinstance(data.get("result"), dict):
        logger.warning(
            "Fresh DingTalk identity lookup rejected provider_id={} errcode={}",
            provider.id,
            data.get("errcode"),
        )
        return None
    return VerifiedDirectoryClaims.from_dingtalk_payload(
        tenant_id=provider.tenant_id,
        provider_id=provider.id,
        external_id=external_id,
        payload=data["result"],
        source="dingtalk_user_get_for_oauth",
    )
