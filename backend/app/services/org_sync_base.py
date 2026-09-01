"""Shared persistence and identity behavior for organization-sync adapters."""

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import IdentityProvider
from app.models.org import OrgDepartment, OrgMember
from app.models.user import Identity, User
from app.services.directory_identity_claims import VerifiedDirectoryClaims
from app.services.org_sync_lifecycle import OrgSyncLifecycleMixin
from app.services.org_sync_models import (
    ExternalDepartment,
    ExternalUser,
    _normalize_contact,
    _utcnow,
    build_department_path_map,
)

try:
    from anyascii import anyascii as _anyascii
except ImportError:  # pragma: no cover - lightweight fallback for minimal test envs
    def _anyascii(value: str) -> str:
        return value

try:
    from pypinyin import Style, lazy_pinyin, pinyin
except ImportError:  # pragma: no cover - lightweight fallback for minimal test envs
    class Style:
        FIRST_LETTER = "first_letter"

    def lazy_pinyin(value: str, errors: str = "default") -> list[str]:
        ascii_value = _anyascii(value)
        return list(ascii_value) if ascii_value else list(value)

    def pinyin(value: str, style: str | None = None) -> list[list[str]]:
        ascii_value = _anyascii(value) or value
        if style == Style.FIRST_LETTER:
            return [[ch.lower()] for ch in ascii_value if ch.strip()]
        return [[ascii_value]]


class BaseOrgSyncAdapter(OrgSyncLifecycleMixin, ABC):
    """Abstract base class for organization sync adapters."""

    provider_type: str = ""

    def __init__(
        self,
        provider: IdentityProvider | None = None,
        config: dict | None = None,
        tenant_id: uuid.UUID | None = None,
    ):
        """Initialize adapter with provider config.

        Args:
            provider: IdentityProvider model from database
            config: Configuration dict (fallback if no provider record)
            tenant_id: Tenant ID for org sync
        """
        self.provider = provider
        self.config = config or {}
        self.tenant_id = tenant_id
        self._client: httpx.AsyncClient | None = None

        if provider and provider.config:
            self.config = provider.config

    @property
    @abstractmethod
    def api_base_url(self) -> str:
        """Base URL for provider API."""
        pass

    @abstractmethod
    async def get_access_token(self) -> str:
        """Get valid access token for API calls."""
        pass

    @abstractmethod
    async def fetch_departments(self) -> list[ExternalDepartment]:
        """Fetch all departments from provider.

        Returns:
            List of ExternalDepartment
        """
        pass

    @abstractmethod
    async def fetch_users(self, department_external_id: str) -> list[ExternalUser]:
        """Fetch users in a department.

        Args:
            department_external_id: External department ID

        Returns:
            List of ExternalUser
        """
        pass

    async def _upsert_member_in_short_transaction(
        self,
        provider_id: uuid.UUID,
        user: ExternalUser,
        department_external_id: str,
    ) -> dict[str, Any]:
        from app.database import async_session

        async with async_session() as member_db:
            async with member_db.begin():
                provider = await member_db.get(IdentityProvider, provider_id)
                if provider is None or not provider.is_active:
                    raise RuntimeError(
                        "DingTalk provider became unavailable during sync"
                    )
                return await self._upsert_member(
                    member_db,
                    provider,
                    user,
                    department_external_id,
                    transaction_scoped_subject_lock=True,
                )

    def _should_skip_department_user_fetch(self, dept: ExternalDepartment) -> bool:
        return False

    async def _reconcile(self, db: AsyncSession, provider_id: uuid.UUID, sync_start: datetime):
        """Mark records that were not updated in this sync as deleted."""

        # 1. Members reconciled
        await db.execute(
            update(OrgMember)
            .where(OrgMember.provider_id == provider_id)
            .where(OrgMember.synced_at < sync_start)
            .where(OrgMember.status != "deleted")
            .values(status="deleted", synced_at=_utcnow())
            .execution_options(synchronize_session=False)
        )

        # 2. Departments reconciled
        await db.execute(
            update(OrgDepartment)
            .where(OrgDepartment.provider_id == provider_id)
            .where(OrgDepartment.synced_at < sync_start)
            .where(OrgDepartment.status != "deleted")
            .values(status="deleted", synced_at=_utcnow())
            .execution_options(synchronize_session=False)
        )

    async def _update_member_counts(self, db: AsyncSession, provider_id: uuid.UUID):
        """Update member_count for all departments to include all their recursive sub-department members."""
        from sqlalchemy import update, select, func

        # 1. Update all departments to show their DIRECT member counts
        direct_subquery = (
            select(func.count(OrgMember.id))
            .where(OrgMember.department_id == OrgDepartment.id)
            .where(OrgMember.status == "active")
            .scalar_subquery()
        )

        await db.execute(
            update(OrgDepartment)
            .where(OrgDepartment.provider_id == provider_id)
            .where(OrgDepartment.status == "active")
            .values(member_count=direct_subquery)
        )

        # 2. Fetch all active departments to compute recursive aggregated counts
        result = await db.execute(
            select(OrgDepartment.id, OrgDepartment.parent_id, OrgDepartment.member_count)
            .where(OrgDepartment.provider_id == provider_id)
            .where(OrgDepartment.status == "active")
        )
        rows = result.all()

        # Build tree structure and lookup
        dept_map = {row.id: {"parent_id": row.parent_id, "direct": row.member_count, "total": 0, "children": []} for row in rows}
        root_ids = []
        for d_id, d_data in dept_map.items():
            parent_id = d_data["parent_id"]
            if parent_id and parent_id in dept_map:
                dept_map[parent_id]["children"].append(d_id)
            else:
                root_ids.append(d_id)

        # Recursive function to calculate total
        def compute_total(node_id):
            node = dept_map[node_id]
            total = node["direct"]
            for child_id in node["children"]:
                total += compute_total(child_id)
            node["total"] = total
            return total

        for root_id in root_ids:
            compute_total(root_id)

        # 3. Bulk update all departments with their aggregated total counts
        # Skip if no updates needed to avoid unnecessary writes, but usually it's fast enough
        update_mappings = [{"id": d_id, "member_count": d_data["total"]} for d_id, d_data in dept_map.items()]

        if update_mappings:
            # Execute individual UPDATE statements to avoid SQLAlchemy 2.x
            # "Bulk UPDATE by Primary Key" ambiguity when passing a list to execute().
            for m in update_mappings:
                await db.execute(
                    update(OrgDepartment)
                    .where(OrgDepartment.id == m["id"])
                    .values(member_count=m["member_count"])
                )

    async def _ensure_provider(self, db: AsyncSession) -> IdentityProvider:
        """Ensure IdentityProvider record exists."""
        if self.provider:
            return self.provider

        # If we have an ID, look it up
        if hasattr(self, 'provider_id') and self.provider_id:
            result = await db.execute(select(IdentityProvider).where(IdentityProvider.id == self.provider_id))
            self.provider = result.scalar_one_or_none()
            if self.provider:
                return self.provider

        # Fallback by type (scoped by tenant)
        query = select(IdentityProvider).where(IdentityProvider.provider_type == self.provider_type)
        if self.tenant_id:
            query = query.where(IdentityProvider.tenant_id == self.tenant_id)
        else:
            query = query.where(IdentityProvider.tenant_id.is_(None))

        result = await db.execute(query)
        provider = result.scalars().first()

        if not provider:
            provider = IdentityProvider(
                provider_type=self.provider_type,
                name=self.provider_type.capitalize(),
                is_active=True,
                config=self.config,
                tenant_id=self.tenant_id
            )
            db.add(provider)
            await db.flush()

        self.provider = provider
        return provider

    async def _upsert_department(
        self, db: AsyncSession, provider: IdentityProvider, dept: ExternalDepartment
    ):
        """Insert or update a department."""
        # Check if exists by external_id and provider
        result = await db.execute(
            select(OrgDepartment).where(
                OrgDepartment.external_id == dept.external_id,
                OrgDepartment.provider_id == provider.id,
            )
        )
        existing = result.scalars().first()

        now = _utcnow()
        # Path is rebuilt from the internal department tree after sync.
        path = dept.name

        # Resolve parent_id from parent_external_id
        parent_id = None
        if dept.parent_external_id:
            parent_result = await db.execute(
                select(OrgDepartment).where(
                    OrgDepartment.external_id == dept.parent_external_id,
                    OrgDepartment.provider_id == provider.id,
                )
            )
            parent_dept = parent_result.scalars().first()
            if parent_dept:
                parent_id = parent_dept.id

        if existing:
            existing.name = dept.name
            existing.member_count = dept.member_count
            existing.path = path
            existing.external_id = dept.external_id
            existing.provider_id = provider.id
            existing.parent_id = parent_id
            existing.status = "active"
            existing.synced_at = now
        else:
            new_dept = OrgDepartment(
                external_id=dept.external_id,
                provider_id=provider.id,
                name=dept.name,
                parent_id=parent_id,
                path=path,
                member_count=dept.member_count,
                tenant_id=self.tenant_id,
                synced_at=now,
            )
            db.add(new_dept)

        await db.flush()

    async def _rebuild_department_paths(self, db: AsyncSession, provider_id: uuid.UUID) -> dict[uuid.UUID, str]:
        """Normalize OrgDepartment.path using parent_id/name reverse derivation."""
        result = await db.execute(
            select(OrgDepartment).where(OrgDepartment.provider_id == provider_id)
        )
        departments = result.scalars().all()
        path_map = build_department_path_map(departments)

        for dept in departments:
            dept.path = path_map.get(dept.id, (dept.name or "").strip())

        return path_map

    async def _refresh_member_department_paths(self, db: AsyncSession, provider_id: uuid.UUID):
        """Refresh OrgMember.department_path from the normalized department tree."""
        dept_result = await db.execute(
            select(OrgDepartment).where(OrgDepartment.provider_id == provider_id)
        )
        departments = dept_result.scalars().all()
        dept_path_map = build_department_path_map(departments)

        member_result = await db.execute(
            select(OrgMember).where(OrgMember.provider_id == provider_id)
        )
        members = member_result.scalars().all()

        for member in members:
            if member.department_id:
                member.department_path = dept_path_map.get(member.department_id, member.department_path or "")
            else:
                member.department_path = member.department_path or ""

    async def _upsert_member(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        user: ExternalUser,
        department_external_id: str,
        *,
        transaction_scoped_subject_lock: bool = False,
    ) -> dict[str, Any]:
        """Insert/update one member under a short-lived subject lock."""
        self._validate_member_identifiers(provider, user)
        now = _utcnow()
        provider_type = (provider.provider_type or self.provider_type or "").lower()
        member_tenant_id = self.tenant_id or provider.tenant_id
        fresh_claims = None
        if (
            provider_type == "dingtalk"
            and member_tenant_id
            and provider.id
            and user.external_id
        ):
            fresh_claims = VerifiedDirectoryClaims.from_dingtalk_payload(
                tenant_id=member_tenant_id,
                provider_id=provider.id,
                external_id=user.external_id,
                payload=(
                    user.raw_data
                    if user.raw_data
                    else {
                        "email": user.email,
                        "mobile": user.mobile,
                    }
                ),
                source="dingtalk_user_list",
                observed_at=now,
            )
            from app.services.dingtalk_identity_reconciliation import (
                dingtalk_legacy_identity_reconciler,
            )

            if transaction_scoped_subject_lock:
                await dingtalk_legacy_identity_reconciler.acquire_subject_lock(
                    db,
                    tenant_id=member_tenant_id,
                    provider_id=provider.id,
                    external_id=user.external_id,
                )
                return await self._upsert_member_locked(
                    db,
                    provider,
                    user,
                    department_external_id,
                    now=now,
                    provider_type=provider_type,
                    member_tenant_id=member_tenant_id,
                    fresh_claims=fresh_claims,
                )

            # Direct single-member callers may still own a longer transaction.
            # A paired session lock avoids accumulating transaction locks there;
            # full sync uses the top-level transaction path above.
            async with dingtalk_legacy_identity_reconciler.session_subject_lock(
                db,
                tenant_id=member_tenant_id,
                provider_id=provider.id,
                external_id=user.external_id,
            ):
                return await self._upsert_member_locked(
                    db,
                    provider,
                    user,
                    department_external_id,
                    now=now,
                    provider_type=provider_type,
                    member_tenant_id=member_tenant_id,
                    fresh_claims=fresh_claims,
                )
        return await self._upsert_member_locked(
            db,
            provider,
            user,
            department_external_id,
            now=now,
            provider_type=provider_type,
            member_tenant_id=member_tenant_id,
            fresh_claims=None,
        )

    async def _upsert_member_locked(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        user: ExternalUser,
        department_external_id: str,
        *,
        now: datetime,
        provider_type: str,
        member_tenant_id: uuid.UUID | None,
        fresh_claims: VerifiedDirectoryClaims | None,
    ) -> dict[str, Any]:
        stats = {
            "user_created": False,
            "user_linked": False,
            "global_identity_matched_no_tenant_user": False,
            "user_skipped_requires_confirmation": False,
            "user_skipped_no_phone": False,
            "profile_synced": False,
            "identity_conflict": False,
            "legacy_split_repaired": False,
        }

        # Find department using user's actual department list.
        # DingTalk's dept_id_list last item is the most specific (leaf) department.
        # We prefer the last entry that exists in our local DB.
        department = None
        if user.department_ids:
            # Iterate in reverse so we try the most specific dept first
            for dept_ext_id in reversed(user.department_ids):
                dept_result = await db.execute(
                    select(OrgDepartment).where(
                        OrgDepartment.external_id == dept_ext_id,
                        OrgDepartment.provider_id == provider.id,
                    )
                )
                department = dept_result.scalars().first()
                if department:
                    break
        # Fallback: use the department_external_id that was set during fetch_users
        if not department and user.department_external_id:
            dept_result = await db.execute(
                select(OrgDepartment).where(
                    OrgDepartment.external_id == user.department_external_id,
                    OrgDepartment.provider_id == provider.id,
                )
            )
            department = dept_result.scalars().first()

        existing_member = await self._find_existing_member(db, provider, user)

        email = _normalize_contact(user.email)
        mobile = _normalize_contact(user.mobile)

        # Update/Create OrgMember
        if existing_member:
            if existing_member.tenant_id is None and member_tenant_id:
                existing_member.tenant_id = member_tenant_id
            existing_member.name = user.name
            if user.nickname:
                existing_member.nickname = user.nickname
            # Generate transliteration using layered strategy:
            # 1. pypinyin converts CJK characters to pinyin
            # 2. anyascii handles remaining non-ASCII scripts (Korean, Japanese kana, Arabic, etc.)
            existing_member.name_translit_full = _anyascii("".join(lazy_pinyin(user.name, errors="default")))
            existing_member.name_translit_initial = "".join([i[0] for i in pinyin(user.name, style=Style.FIRST_LETTER)])

            if email is not None:
                existing_member.email = email
            existing_member.avatar_url = user.avatar_url
            existing_member.title = user.title
            existing_member.department_id = department.id if department else None
            existing_member.department_path = department.path if department else user.department_path
            # A protected field may be blank because it was not returned under
            # the current app permissions.  Retain the display value, but never
            # reuse it as fresh identity evidence.
            if mobile is not None:
                existing_member.phone = mobile
            existing_member.status = user.status

            # Universal ID fields
            existing_member.external_id = user.external_id
            existing_member.open_id = user.open_id
            existing_member.unionid = user.unionid

            existing_member.provider_id = provider.id
            existing_member.synced_at = now
            stats["profile_synced"] = True
            member = existing_member
        else:
            # Generate transliteration using layered strategy:
            # 1. pypinyin converts CJK characters to pinyin
            # 2. anyascii handles remaining non-ASCII scripts (Korean, Japanese kana, Arabic, etc.)
            translit_full = _anyascii("".join(lazy_pinyin(user.name, errors="default")))
            translit_initial = "".join([i[0] for i in pinyin(user.name, style=Style.FIRST_LETTER)])

            new_member = OrgMember(
                external_id=user.external_id,
                open_id=user.open_id,
                unionid=user.unionid,

                provider_id=provider.id,
                user_id=None,
                name=user.name,
                nickname=user.nickname or None,
                name_translit_full=translit_full,
                name_translit_initial=translit_initial,
                email=email,
                avatar_url=user.avatar_url,
                title=user.title,
                department_id=department.id if department else None,
                department_path=department.path if department else user.department_path,
                phone=mobile,
                status=user.status,
                tenant_id=member_tenant_id,
                synced_at=now,
            )
            db.add(new_member)
            stats["profile_synced"] = True
            member = new_member

        auto_create_users = (provider.config or {}).get("auto_create_users_on_sync")
        if auto_create_users is None:
            auto_create_users = provider_type == "dingtalk"

        if auto_create_users:
            from app.services.contact_provisioning import contact_provisioning
            from app.services.canonical_user_resolver import (
                CanonicalIdentityConflict,
                CanonicalUserConflict,
            )

            # Contact persistence and identity reconciliation intentionally use
            # different savepoints.  A historical identity conflict must not
            # discard newly observed directory profile data.
            try:
                async with db.begin_nested():
                    provisioning = await contact_provisioning.ensure_user_for_org_member(
                        db,
                        member,
                        provider=provider,
                        fresh_claims=fresh_claims,
                        subject_lock_held=fresh_claims is not None,
                    )
                stats["user_created"] = provisioning.user_created
                stats["user_linked"] = provisioning.user_linked
                stats["global_identity_matched_no_tenant_user"] = (
                    provisioning.global_identity_matched_no_tenant_user
                )
                stats["user_skipped_requires_confirmation"] = (
                    provisioning.skipped_requires_confirmation
                )
                stats["user_skipped_no_phone"] = provisioning.skipped_missing_mobile
                stats["legacy_split_repaired"] = provisioning.legacy_split_repaired
            except (CanonicalIdentityConflict, CanonicalUserConflict) as exc:
                stats["identity_conflict"] = True
                logger.warning(
                    "[OrgSync][{}] Fresh directory claims could not be reconciled "
                    "for member_id={}: {}",
                    provider_type,
                    member.id,
                    type(exc).__name__,
                )
        else:
            platform_user = await self._resolve_platform_user(
                db,
                user,
                member_tenant_id,
                allow_email=provider_type != "dingtalk",
            )
            if platform_user and not member.user_id:
                member.user_id = platform_user.id
                stats["user_linked"] = True

        await db.flush()
        return stats

    def _provider_requires_unionid(self, provider: IdentityProvider) -> bool:
        provider_type = (provider.provider_type or self.provider_type or "").lower()
        return provider_type in {"feishu", "dingtalk"}

    def _validate_member_identifiers(self, provider: IdentityProvider, user: ExternalUser) -> None:
        user.unionid = (user.unionid or "").strip()
        user.external_id = (user.external_id or "").strip()
        user.open_id = (user.open_id or "").strip()

        if self._provider_requires_unionid(provider) and not user.unionid:
            raise ValueError(
                f"unionid is required for {provider.provider_type} org sync user {user.external_id or user.name}"
            )

        if user.unionid and user.external_id and user.unionid == user.external_id:
            raise ValueError(
                f"invalid unionid for org sync user {user.external_id or user.name}: unionid must not equal external_id"
            )

    async def _find_existing_member(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        user: ExternalUser,
    ) -> OrgMember | None:
        if user.unionid:
            result = await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider.id,
                    OrgMember.unionid == user.unionid,
                )
            )
            existing_member = result.scalars().first()
            if existing_member:
                return existing_member

        fallback_conditions = []
        if user.external_id:
            fallback_conditions.append(OrgMember.external_id == user.external_id)
        if user.open_id:
            fallback_conditions.append(OrgMember.open_id == user.open_id)

        if not fallback_conditions:
            return None

        fallback_query = select(OrgMember).where(
            OrgMember.provider_id == provider.id,
            or_(*fallback_conditions),
        )

        # When unionid is required, only allow external/open id fallback to attach
        # shell records that do not have a conflicting unionid yet.
        if self._provider_requires_unionid(provider) and user.unionid:
            fallback_query = fallback_query.where(
                or_(
                    OrgMember.unionid.is_(None),
                    OrgMember.unionid == "",
                    OrgMember.unionid == user.unionid,
                )
            )

        result = await db.execute(fallback_query)
        return result.scalars().first()

    async def _resolve_platform_user(
        self,
        db: AsyncSession,
        user: ExternalUser,
        tenant_id: uuid.UUID | None = None,
        allow_email: bool = True,
    ) -> User | None:
        """Resolve platform user from external user info."""
        # 1. Try by Email matching (primary way now)
        email = _normalize_contact(user.email)
        if allow_email and email:
            query = select(User).join(User.identity).where(Identity.email == email)
            if tenant_id:
                query = query.where(User.tenant_id == tenant_id)
            result = await db.execute(query)
            u = result.scalars().first()
            if u:
                return u

        # 2. Try by mobile matching
        mobile = _normalize_contact(user.mobile)
        if mobile:
            query = select(User).join(User.identity).where(Identity.phone == mobile)
            if tenant_id:
                query = query.where(User.tenant_id == tenant_id)
            result = await db.execute(query)
            u = result.scalars().first()
            if u:
                return u

        return None
