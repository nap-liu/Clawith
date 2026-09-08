"""Canonical identity and tenant-user reconciliation.

Contact claims are exact and evaluated in provider-configured order. Channel
identifiers are routing evidence only; names are never identity evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import uuid

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.user import Identity, User
from app.services.provider_identity_policy import (
    DEFAULT_IDENTITY_MATCH_ORDER,
    normalize_identity_match_order,
)


class CanonicalIdentityConflict(ValueError):
    """Trusted identity claims point at different people."""


class CanonicalUserConflict(ValueError):
    """Trusted tenant/channel evidence cannot be reconciled safely."""


def normalize_email(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return normalized or None


def _is_placeholder_email(value: str | None) -> bool:
    return bool(value and normalize_email(value).endswith(".local"))


def normalize_phone(value: str | None) -> str | None:
    normalized = re.sub(r"[\s\-\+]", "", str(value or "")).strip()
    return normalized or None


def phone_match_candidates(value: str | None) -> list[str | None]:
    """Exact mainland domestic/international equivalents for opt-in lookup.

    Other explicit international numbers keep the existing exact normalization.
    This never writes or merges identities; callers must reject multiple hits.
    """
    raw = re.sub(r"[\s\-]", "", str(value or ""))
    normalized = normalize_phone(raw)
    if raw.startswith("+") and not raw.startswith("+86"):
        return [normalized]
    match = re.fullmatch(r"(?:0086|86)?(1[3-9]\d{9})", normalized or "")
    if not match:
        return [normalized]
    national = match.group(1)
    return [national, "86" + national, "0086" + national]


@dataclass(slots=True)
class IdentityClaims:
    identity: Identity | None
    email: str | None
    phone: str | None
    matched_by: str | None = None
    conflicting_fields: tuple[str, ...] = ()
    candidate_identity_ids: dict[str, uuid.UUID] = field(default_factory=dict)


class CanonicalUserResolver:
    async def resolve_identity_claims(
        self,
        db: AsyncSession,
        *,
        email: str | None,
        phone: str | None,
        enrich: bool = True,
        ordered_fields: tuple[str, ...] | list[str] = DEFAULT_IDENTITY_MATCH_ORDER,
        phone_equivalence: bool = False,
    ) -> IdentityClaims:
        """Resolve exact contacts in order; lower-priority conflicts are flagged."""
        fields = normalize_identity_match_order(ordered_fields)
        email = normalize_email(email) if "email" in fields else None
        phone_values = phone_match_candidates(phone) if phone_equivalence else [normalize_phone(phone)]
        phone = normalize_phone(phone) if "phone" in fields else None

        candidates: dict[str, Identity | None] = {"email": None, "phone": None}
        if "email" in fields and email:
            matches = (
                await db.execute(
                    select(Identity)
                    .where(func.lower(func.btrim(Identity.email)) == email)
                    .limit(2)
                )
            ).scalars().all()
            if len(matches) > 1:
                raise CanonicalIdentityConflict("email maps to multiple identities")
            candidates["email"] = matches[0] if matches else None
        if "phone" in fields and phone:
            matches = (
                await db.execute(
                    select(Identity)
                    .where(
                        func.regexp_replace(
                            Identity.phone, r"[[:space:]+-]", "", "g"
                        ).in_(phone_values)
                    )
                    .limit(2)
                )
            ).scalars().all()
            if len(matches) > 1:
                raise CanonicalIdentityConflict("phone maps to multiple identities")
            candidates["phone"] = matches[0] if matches else None

        matched_by = next(
            (field for field in fields if candidates[field] is not None), None
        )
        identity = candidates[matched_by] if matched_by else None
        conflicting_fields = tuple(
            field
            for field in fields
            if identity is not None
            and candidates[field] is not None
            and candidates[field].id != identity.id
        )
        if identity and enrich:
            try:
                async with db.begin_nested():
                    if "email" not in conflicting_fields and email and (
                        not identity.email
                        or _is_placeholder_email(identity.email)
                        or normalize_email(identity.email) == email
                    ):
                        identity.email = email
                    if "phone" not in conflicting_fields and phone and (
                        not identity.phone or normalize_phone(identity.phone) == phone
                    ):
                        identity.phone = phone
                    await db.flush()
            except IntegrityError as exc:
                db.expire(identity)
                reread = await self.resolve_identity_claims(
                    db,
                    email=email,
                    phone=phone,
                    enrich=False,
                    ordered_fields=fields,
                )
                if reread.identity and reread.identity.id == identity.id:
                    identity = reread.identity
                    conflicting_fields = reread.conflicting_fields
                else:
                    raise CanonicalIdentityConflict(
                        "concurrent identity claim resolved to another identity"
                    ) from exc

        return IdentityClaims(
            identity=identity,
            email=email,
            phone=phone,
            matched_by=matched_by,
            conflicting_fields=conflicting_fields,
            candidate_identity_ids={
                name: candidate.id
                for name, candidate in candidates.items()
                if candidate is not None
            },
        )

    async def get_tenant_user(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        identity_id: uuid.UUID,
        lock: bool = False,
    ) -> User | None:
        stmt = (
            select(User)
            .where(User.tenant_id == tenant_id, User.identity_id == identity_id)
            .options(selectinload(User.identity))
        )
        if lock:
            stmt = stmt.with_for_update()
        users = (await db.execute(stmt)).scalars().all()
        if len(users) > 1:
            raise CanonicalUserConflict(
                "identity maps to multiple users in the same tenant; migration_required"
            )
        return users[0] if users else None

    async def find_verified_directory_user(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        email: str | None,
        phone: str | None,
    ) -> User | None:
        """Find one user through verified corporate-directory contact data."""
        email = normalize_email(email)
        phone = normalize_phone(phone)
        if not email and not phone:
            return None

        contact_conditions = []
        if email:
            contact_conditions.append(
                func.lower(func.btrim(OrgMember.email)) == email
            )
        if phone:
            contact_conditions.append(
                func.regexp_replace(
                    OrgMember.phone, r"[[:space:]+-]", "", "g"
                )
                == phone
            )
        contact_condition = contact_conditions[0]
        for condition in contact_conditions[1:]:
            contact_condition = contact_condition | condition

        rows = (
            await db.execute(
                select(OrgMember, IdentityProvider)
                .join(IdentityProvider, IdentityProvider.id == OrgMember.provider_id)
                .where(
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.status == "active",
                    OrgMember.user_id.is_not(None),
                    IdentityProvider.is_active == True,  # noqa: E712
                    contact_condition,
                )
                .with_for_update(of=OrgMember)
            )
        ).all()

        trusted_user_ids: set[uuid.UUID] = set()
        for member, provider in rows:
            config = provider.config or {}
            trusted = provider.provider_type == "dingtalk" or bool(
                config.get("verified_contact_identity")
            )
            if not trusted:
                continue
            member_email = normalize_email(member.email)
            member_phone = normalize_phone(member.phone)
            # OR is used to discover candidates, but all supplied overlapping
            # claims must agree.  A stale contradictory contact fails closed.
            if email and member_email and member_email != email and phone == member_phone:
                raise CanonicalUserConflict(
                    "directory phone match has a conflicting verified email"
                )
            if phone and member_phone and member_phone != phone and email == member_email:
                raise CanonicalUserConflict(
                    "directory email match has a conflicting verified phone"
                )
            trusted_user_ids.add(member.user_id)

        if not trusted_user_ids:
            return None
        if len(trusted_user_ids) != 1:
            raise CanonicalUserConflict(
                "verified directory contact maps to multiple tenant users"
            )
        return (
            await db.execute(
                select(User)
                .where(User.id == next(iter(trusted_user_ids)), User.tenant_id == tenant_id)
                .options(selectinload(User.identity))
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def reconcile_identity_user(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        identity: Identity,
        candidate_user: User | None,
    ) -> User | None:
        """Attach or converge a verified identity with one exact tenant user."""
        if candidate_user is None:
            return await self.get_tenant_user(
                db,
                tenant_id=tenant_id,
                identity_id=identity.id,
                lock=True,
            )
        candidate_user = (
            await db.execute(
                select(User)
                .where(User.id == candidate_user.id)
                .options(selectinload(User.identity))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if candidate_user is None:
            return await self.get_tenant_user(
                db,
                tenant_id=tenant_id,
                identity_id=identity.id,
                lock=True,
            )
        if candidate_user.tenant_id != tenant_id:
            raise CanonicalUserConflict("candidate user belongs to another tenant")
        if candidate_user.identity_id and candidate_user.identity_id != identity.id:
            raise CanonicalUserConflict(
                "exact directory/channel user has a different non-empty identity"
            )
        canonical = await self.get_tenant_user(
            db,
            tenant_id=tenant_id,
            identity_id=identity.id,
            lock=True,
        )
        if canonical and canonical.id != candidate_user.id:
            if candidate_user.identity_id is not None:
                raise CanonicalUserConflict("two identity-backed tenant users cannot be merged")
            return await self.merge_identityless_user(
                db, source=candidate_user, target=canonical
            )
        if candidate_user.identity_id is None:
            try:
                async with db.begin_nested():
                    candidate_user.identity_id = identity.id
                    await db.flush()
            except IntegrityError as exc:
                await db.refresh(candidate_user)
                winner = await self.get_tenant_user(
                    db,
                    tenant_id=tenant_id,
                    identity_id=identity.id,
                    lock=True,
                )
                if winner is None:
                    raise CanonicalUserConflict(
                        "concurrent identity attachment has no canonical winner"
                    ) from exc
                if candidate_user.identity_id is not None:
                    if candidate_user.identity_id == identity.id:
                        return candidate_user
                    raise CanonicalUserConflict(
                        "candidate identity changed during reconciliation"
                    ) from exc
                return await self.merge_identityless_user(
                    db, source=candidate_user, target=winner
                )
            candidate_user.identity = identity
        return candidate_user

    async def get_or_create_tenant_user(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        identity: Identity,
        display_name: str,
        avatar_url: str | None,
        registration_source: str,
    ) -> tuple[User, bool]:
        existing = await self.get_tenant_user(
            db, tenant_id=tenant_id, identity_id=identity.id, lock=True
        )
        if existing:
            return existing, False

        inserted_id = await db.scalar(
            pg_insert(User)
            .values(
                id=uuid.uuid4(),
                identity_id=identity.id,
                tenant_id=tenant_id,
                display_name=display_name or identity.username or "User",
                avatar_url=avatar_url,
                role="member",
                source=registration_source,
                registration_source=registration_source,
                is_active=True,
                quota_message_limit=50,
                quota_message_period="permanent",
                quota_messages_used=0,
                quota_max_agents=2,
                quota_agent_ttl_hours=0,
            )
            .on_conflict_do_nothing()
            .returning(User.id)
        )
        if inserted_id:
            user = (
                await db.execute(
                    select(User)
                    .where(User.id == inserted_id)
                    .options(selectinload(User.identity))
                )
            ).scalar_one()
            return user, True

        winner = await self.get_tenant_user(
            db, tenant_id=tenant_id, identity_id=identity.id, lock=True
        )
        if winner is None:
            raise CanonicalUserConflict(
                "tenant user insert conflicted without a canonical winner"
            )
        return winner, False

    async def merge_identityless_user(
        self,
        db: AsyncSession,
        *,
        source: User,
        target: User,
    ) -> User:
        """Narrow convergence: same-tenant identityless channel user -> canonical."""
        if source.id == target.id:
            return target
        if source.tenant_id != target.tenant_id:
            raise CanonicalUserConflict("cross-tenant user convergence is forbidden")
        if source.identity_id is not None or target.identity_id is None:
            raise CanonicalUserConflict(
                "only identityless users may converge into an identity-backed user"
            )

        locked = (
            await db.execute(
                select(User)
                .where(User.id.in_([source.id, target.id]))
                .order_by(User.id)
                .with_for_update()
            )
        ).scalars().all()
        by_id = {row.id: row for row in locked}
        source = by_id.get(source.id)
        target = by_id.get(target.id)
        if source is None:
            return target
        if target is None or source.identity_id is not None or target.identity_id is None:
            raise CanonicalUserConflict("user convergence preconditions changed")

        # Collapse association rows whose natural key already exists for target.
        dedupe_sql = (
            ("agent_relationships", "agent_id"),
            ("agent_user_onboardings", "agent_id"),
            ("user_tenant_onboardings", "tenant_id"),
        )
        for table_name, key_column in dedupe_sql:
            await db.execute(
                text(
                    f'DELETE FROM "{table_name}" s USING "{table_name}" t '
                    f'WHERE s.user_id = :source AND t.user_id = :target '
                    f'AND s."{key_column}" = t."{key_column}"'
                ),
                {"source": source.id, "target": target.id},
            )
        if await self._table_exists(db, "channel_user_bindings"):
            await db.execute(
                text(
                    """
                    DELETE FROM channel_user_bindings s
                    USING channel_user_bindings t
                    WHERE s.user_id = :source AND t.user_id = :target
                      AND s.tenant_id = t.tenant_id
                      AND s.installation_scope = t.installation_scope
                      AND s.id_type = t.id_type
                      AND s.subject = t.subject
                    """
                ),
                {"source": source.id, "target": target.id},
            )

        # Preserve the strongest explicit per-agent user permission.
        await db.execute(
            text(
                """
                UPDATE agent_permissions t
                SET access_level = CASE
                    WHEN t.access_level = 'manage' OR s.access_level = 'manage'
                    THEN 'manage' ELSE 'use' END
                FROM agent_permissions s
                WHERE t.scope_type = 'user' AND t.scope_id = :target
                  AND s.scope_type = 'user' AND s.scope_id = :source
                  AND t.agent_id = s.agent_id
                """
            ),
            {"source": source.id, "target": target.id},
        )
        await db.execute(
            text(
                """
                DELETE FROM agent_permissions s
                USING agent_permissions t
                WHERE s.scope_type = 'user' AND s.scope_id = :source
                  AND t.scope_type = 'user' AND t.scope_id = :target
                  AND s.agent_id = t.agent_id
                """
            ),
            {"source": source.id, "target": target.id},
        )
        await db.execute(
            text(
                "UPDATE agent_permissions SET scope_id = :target "
                "WHERE scope_type = 'user' AND scope_id = :source"
            ),
            {"source": source.id, "target": target.id},
        )

        # Same person may already have two daily summaries for one day. Keep
        # the canonical row and append non-identical source content before
        # deleting the duplicate, so convergence is lossless and deterministic.
        if await self._table_exists(db, "member_daily_reports"):
            await db.execute(
                text(
                    """
                    UPDATE member_daily_reports t
                    SET content = CASE
                        WHEN btrim(coalesce(s.content, '')) = '' THEN t.content
                        WHEN btrim(coalesce(t.content, '')) = '' THEN s.content
                        WHEN t.content = s.content THEN t.content
                        ELSE t.content || E'\n\n---\n\n' || s.content
                    END,
                    submitted_at = LEAST(t.submitted_at, s.submitted_at),
                    updated_at = GREATEST(t.updated_at, s.updated_at)
                    FROM member_daily_reports s
                    WHERE s.user_id = :source AND t.user_id = :target
                      AND s.tenant_id = t.tenant_id
                      AND s.report_date = t.report_date
                    """
                ),
                {"source": source.id, "target": target.id},
            )
            await db.execute(
                text(
                    """
                    DELETE FROM member_daily_reports s
                    USING member_daily_reports t
                    WHERE s.user_id = :source AND t.user_id = :target
                      AND s.tenant_id = t.tenant_id
                      AND s.report_date = t.report_date
                    """
                ),
                {"source": source.id, "target": target.id},
            )

        # Preserve every session; only one web/H5 primary may exist per agent/user.
        await db.execute(
            text(
                """
                UPDATE chat_sessions s SET is_primary = false
                WHERE s.user_id = :source AND s.is_primary = true
                  AND s.source_channel IN ('web', 'miniprogram', 'wechat_miniprogram')
                  AND EXISTS (
                    SELECT 1 FROM chat_sessions t
                    WHERE t.user_id = :target AND t.agent_id = s.agent_id
                      AND t.is_primary = true
                      AND t.source_channel IN ('web', 'miniprogram', 'wechat_miniprogram')
                  )
                """
            ),
            {"source": source.id, "target": target.id},
        )

        await self._merge_participants(db, source.id, target.id)

        # Update every real FK to users. This deliberately derives the list
        # from PostgreSQL so new business tables cannot silently retain the
        # obsolete canonical ID. Natural-key collisions fail the transaction.
        fk_rows = (
            await db.execute(
                text(
                    """
                    SELECT ns.nspname, c.relname, a.attname
                    FROM pg_constraint con
                    JOIN pg_class c ON c.oid = con.conrelid
                    JOIN pg_namespace ns ON ns.oid = c.relnamespace
                    JOIN pg_class ref ON ref.oid = con.confrelid
                    JOIN unnest(con.conkey) WITH ORDINALITY ck(attnum, ord) ON true
                    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ck.attnum
                    WHERE con.contype = 'f' AND ref.relname = 'users'
                      AND ns.nspname = current_schema()
                    """
                )
            )
        ).all()
        for schema_name, table_name, column_name in fk_rows:
            await db.execute(
                text(
                    f'UPDATE "{schema_name}"."{table_name}" '
                    f'SET "{column_name}" = :target WHERE "{column_name}" = :source'
                ),
                {"source": source.id, "target": target.id},
            )

        # Soft references intentionally kept outside FK constraints.
        await db.execute(
            text("UPDATE org_members SET user_id = :target WHERE user_id = :source"),
            {"source": source.id, "target": target.id},
        )
        for table_name, actor_column in (
            ("agent_agent_relationships", "created_by_user_id"),
            ("agent_agent_relationships", "updated_by_user_id"),
            ("mcp_server_overrides", "last_modified_by_user_id"),
            ("sso_scan_sessions", "user_id"),
        ):
            await self._update_soft_reference_if_present(
                db,
                table_name=table_name,
                column_name=actor_column,
                source=source.id,
                target=target.id,
            )
        if await self._table_exists(db, "workspace_file_revisions"):
            await db.execute(
                text(
                    "UPDATE workspace_file_revisions SET actor_id = :target "
                    "WHERE actor_type = 'user' AND actor_id = :source"
                ),
                {"source": source.id, "target": target.id},
            )
        for table_name, type_column, id_column, user_value in (
            ("okr_objectives", "owner_type", "owner_id", "user"),
            ("work_reports", "author_type", "author_id", "user"),
            ("member_daily_reports", "member_type", "member_id", "user"),
            ("plaza_posts", "author_type", "author_id", "human"),
            ("plaza_comments", "author_type", "author_id", "human"),
            ("plaza_likes", "author_type", "author_id", "human"),
        ):
            if await self._table_exists(db, table_name):
                await db.execute(
                    text(
                        f'UPDATE "{table_name}" SET "{id_column}" = :target '
                        f'WHERE "{type_column}" = :user_value '
                        f'AND "{id_column}" = :source'
                    ),
                    {
                        "source": source.id,
                        "target": target.id,
                        "user_value": user_value,
                    },
                )
        if await self._table_exists(db, "agent_triggers"):
            await db.execute(
                text(
                    """
                    UPDATE agent_triggers
                    SET config = jsonb_set(
                        config::jsonb,
                        '{from_user_id}',
                        to_jsonb(CAST(:target AS text)),
                        false
                    )
                    WHERE config->>'from_user_id' = CAST(:source AS text)
                    """
                ),
                {"source": str(source.id), "target": str(target.id)},
            )
        if await self._table_exists(db, "tasks"):
            await db.execute(
                text(
                    "UPDATE tasks SET assignee = :target "
                    "WHERE assignee = :source"
                ),
                {"source": str(source.id), "target": str(target.id)},
            )
        if await self._table_exists(db, "relationship_suppressions"):
            await db.execute(
                text(
                    """
                    DELETE FROM relationship_suppressions s
                    USING relationship_suppressions t
                    WHERE s.target_type = 'user' AND s.target_id = :source
                      AND t.target_type = 'user' AND t.target_id = :target
                      AND s.agent_id = t.agent_id
                    """
                ),
                {"source": source.id, "target": target.id},
            )
            await db.execute(
                text(
                    "UPDATE relationship_suppressions SET target_id = :target "
                    "WHERE target_type = 'user' AND target_id = :source"
                ),
                {"source": source.id, "target": target.id},
            )

        await db.delete(source)
        await db.flush()
        logger.info(
            "Converged identityless tenant user {} into canonical user {}",
            source.id,
            target.id,
        )
        return target

    async def _table_exists(self, db: AsyncSession, table_name: str) -> bool:
        return bool(
            await db.scalar(
                text("SELECT to_regclass(:table_name) IS NOT NULL"),
                {"table_name": table_name},
            )
        )

    async def _update_soft_reference_if_present(
        self,
        db: AsyncSession,
        *,
        table_name: str,
        column_name: str,
        source: uuid.UUID,
        target: uuid.UUID,
    ) -> None:
        if not await self._table_exists(db, table_name):
            return
        await db.execute(
            text(
                f'UPDATE "{table_name}" SET "{column_name}" = :target '
                f'WHERE "{column_name}" = :source'
            ),
            {"source": source, "target": target},
        )

    async def _merge_participants(
        self,
        db: AsyncSession,
        source_user_id: uuid.UUID,
        target_user_id: uuid.UUID,
    ) -> None:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT id, ref_id FROM participants
                    WHERE type = 'user' AND ref_id IN (:source, :target)
                    FOR UPDATE
                    """
                ),
                {"source": source_user_id, "target": target_user_id},
            )
        ).all()
        source_participant = next((row.id for row in rows if row.ref_id == source_user_id), None)
        target_participant = next((row.id for row in rows if row.ref_id == target_user_id), None)
        if not source_participant:
            return
        if target_participant:
            participant_fks = (
                await db.execute(
                    text(
                        """
                        SELECT ns.nspname, c.relname, a.attname
                        FROM pg_constraint con
                        JOIN pg_class c ON c.oid = con.conrelid
                        JOIN pg_namespace ns ON ns.oid = c.relnamespace
                        JOIN pg_class ref ON ref.oid = con.confrelid
                        JOIN unnest(con.conkey) WITH ORDINALITY ck(attnum, ord) ON true
                        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ck.attnum
                        WHERE con.contype = 'f' AND ref.relname = 'participants'
                          AND ns.nspname = current_schema()
                        """
                    )
                )
            ).all()
            for schema_name, table_name, column_name in participant_fks:
                await db.execute(
                    text(
                        f'UPDATE "{schema_name}"."{table_name}" '
                        f'SET "{column_name}" = :target WHERE "{column_name}" = :source'
                    ),
                    {"source": source_participant, "target": target_participant},
                )
            await db.execute(
                text("DELETE FROM participants WHERE id = :source"),
                {"source": source_participant},
            )
        else:
            await db.execute(
                text("UPDATE participants SET ref_id = :target WHERE id = :participant"),
                {"target": target_user_id, "participant": source_participant},
            )


canonical_user_resolver = CanonicalUserResolver()
