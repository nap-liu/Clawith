"""Organization-sync lifecycle orchestration shared by provider adapters."""

from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.directory_user_status import sync_tenant_user_statuses
from app.services.org_sync_models import (
    PLATFORM_ENTERPRISE_ROOT_EXTERNAL_ID,
    _utcnow,
    configured_enterprise_root_name,
    normalize_enterprise_root,
)
from app.services.vendor_directory_snapshot import build_vendor_scim_snapshot


class OrgSyncLifecycleMixin:
    @staticmethod
    def _merge_snapshot_user(existing, incoming, department_external_id: str) -> None:
        """Merge repeated department listings into one provider account fact."""
        memberships = [
            *existing.department_ids,
            *incoming.department_ids,
            existing.department_external_id,
            incoming.department_external_id,
            department_external_id,
        ]
        existing.department_ids = list(dict.fromkeys(value for value in memberships if value))
        if not existing.department_external_id and existing.department_ids:
            existing.department_external_id = existing.department_ids[0]
        for field in (
            "name", "nickname", "open_id", "unionid", "email", "avatar_url",
            "title", "mobile",
        ):
            if not getattr(existing, field, None) and getattr(incoming, field, None):
                setattr(existing, field, getattr(incoming, field))
        existing.raw_data = {**(existing.raw_data or {}), **(incoming.raw_data or {})}

    async def _emit_sync_progress(
        self,
        *,
        stage: str,
        processed: int,
        total: int | None,
        percent: int,
    ) -> None:
        callback = getattr(self, "progress_callback", None)
        if callback is not None:
            await callback(stage, processed, total, percent)

    async def sync_org_structure(self, db: AsyncSession) -> dict[str, Any]:
        """Main sync function - syncs departments and members.

        Args:
            db: Database session

        Returns:
            Dict with sync results: {"departments": count, "members": count, "users_created": count, "profiles_synced": count, "errors": []}
        """
        errors = []
        dept_count = 0
        member_count = 0
        user_count = 0
        user_linked_count = 0
        global_identity_reused_count = 0
        user_skipped_no_phone_count = 0
        user_skipped_confirmation_count = 0
        user_fetch_skipped_dept_count = 0
        profile_count = 0
        identity_conflict_count = 0
        legacy_split_repaired_count = 0
        tenant_users_enabled_count = 0
        tenant_users_disabled_count = 0
        sync_start = _utcnow()
        partial_failure = False

        # Ensure provider exists
        provider = await self._ensure_provider(db)
        # Provider I/O must never hold a database transaction open.
        await db.commit()

        try:
            # Fetch a complete provider snapshot before mutating directory facts.
            departments = normalize_enterprise_root(
                await self.fetch_departments(),
                root_name=configured_enterprise_root_name(provider.config),
            )
            # Fetch one normalized account snapshot before applying it. Provider
            # APIs commonly list the same person once per department; merging
            # here preserves every Group membership and avoids repeated writes.
            users_by_external_id = {}
            await self._emit_sync_progress(
                stage="fetching_accounts",
                processed=0,
                total=len(departments),
                percent=10,
            )
            for dept_index, dept in enumerate(departments, start=1):
                if (
                    dept.external_id == PLATFORM_ENTERPRISE_ROOT_EXTERNAL_ID
                    or self._should_skip_department_user_fetch(dept)
                ):
                    user_fetch_skipped_dept_count += 1
                    logger.info(
                        f"[OrgSync] Skipping user fetch for department {dept.external_id} ({dept.name})"
                    )
                    continue

                try:
                    users = await self.fetch_users(dept.external_id)
                except Exception as e:
                    partial_failure = True
                    logger.error(f"[OrgSync] Failed to fetch users in department {dept.external_id}: {e}")
                    errors.append(f"Fetch users in dept {dept.external_id}: {str(e)}")
                    await self._emit_sync_progress(
                        stage="fetching_accounts",
                        processed=dept_index,
                        total=len(departments),
                        percent=10 + int(25 * dept_index / max(len(departments), 1)),
                    )
                    continue

                for user in users:
                    memberships = [
                        *user.department_ids,
                        user.department_external_id,
                        dept.external_id,
                    ]
                    user.department_ids = list(
                        dict.fromkeys(value for value in memberships if value)
                    )
                    if not user.department_external_id and user.department_ids:
                        user.department_external_id = user.department_ids[0]
                    existing_user = users_by_external_id.get(user.external_id)
                    if existing_user is None:
                        users_by_external_id[user.external_id] = user
                    else:
                        self._merge_snapshot_user(existing_user, user, dept.external_id)

                await self._emit_sync_progress(
                    stage="fetching_accounts",
                    processed=dept_index,
                    total=len(departments),
                    percent=10 + int(25 * dept_index / max(len(departments), 1)),
                )

            snapshot_users = list(users_by_external_id.values())
            if partial_failure or user_fetch_skipped_dept_count:
                raise RuntimeError("Provider snapshot fetch was incomplete")
            snapshot = build_vendor_scim_snapshot(departments, snapshot_users)
            departments_by_id = {
                str(department.external_id): department for department in departments
            }
            departments = [
                departments_by_id[group.id]
                for group in snapshot.topological_groups()
            ]

            await self._emit_sync_progress(
                stage="applying_groups",
                processed=0,
                total=len(departments),
                percent=35,
            )
            for dept_index, dept in enumerate(departments, start=1):
                try:
                    async with db.begin_nested():
                        await self._upsert_department(db, provider, dept)
                    dept_count += 1
                except Exception as e:
                    partial_failure = True
                    errors.append(f"Department {dept.external_id}: {str(e)}")
                    logger.error(f"[OrgSync] Failed to sync department {dept.external_id}: {e}")
                await self._emit_sync_progress(
                    stage="applying_groups",
                    processed=dept_index,
                    total=len(departments),
                    percent=35 + int(25 * dept_index / max(len(departments), 1)),
                )

            await self._rebuild_department_paths(db, provider.id)
            await db.flush()
            await db.commit()
            per_member_transactions = (
                (
                    getattr(provider, "provider_type", None)
                    or self.provider_type
                    or ""
                ).lower()
                == "dingtalk"
                and isinstance(db, AsyncSession)
            )
            for user_index, user in enumerate(snapshot_users, start=1):
                try:
                    primary_department = (
                        user.department_external_id
                        or (user.department_ids[0] if user.department_ids else "")
                    )
                    if per_member_transactions:
                        stats = await self._upsert_member_in_short_transaction(
                            provider.id, user, primary_department
                        )
                    else:
                        async with db.begin_nested():
                            stats = await self._upsert_member(
                                db, provider, user, primary_department
                            )
                    user_count += int(bool(stats.get("user_created")))
                    user_linked_count += int(bool(stats.get("user_linked")))
                    global_identity_reused_count += int(
                        bool(stats.get("global_identity_matched_no_tenant_user"))
                    )
                    user_skipped_no_phone_count += int(
                        bool(stats.get("user_skipped_no_phone"))
                    )
                    user_skipped_confirmation_count += int(
                        bool(stats.get("user_skipped_requires_confirmation"))
                    )
                    profile_count += int(bool(stats.get("profile_synced")))
                    identity_conflict_count += int(bool(stats.get("identity_conflict")))
                    legacy_split_repaired_count += int(
                        bool(stats.get("legacy_split_repaired"))
                    )
                    member_count += 1
                except Exception as e:
                    partial_failure = True
                    logger.error(f"[OrgSync] Failed to sync member {user.external_id} ({user.name}): {e}")
                    errors.append(f"Member {user.external_id}: {str(e)}")
                await self._emit_sync_progress(
                    stage="applying_accounts",
                    processed=user_index,
                    total=len(snapshot_users),
                    percent=60 + int(25 * user_index / max(len(snapshot_users), 1)),
                )

            await self._emit_sync_progress(
                stage="reconciling",
                processed=len(departments),
                total=len(departments),
                percent=90,
            )
            await self._refresh_member_department_paths(db, provider.id)
            await db.flush()

            # Only a complete, conflict-free snapshot can advance success metadata
            # or tombstone records absent from the provider response.
            if self.provider:
                if partial_failure:
                    logger.warning(
                        f"[OrgSync] Skipping reconcile for provider {provider.id} because this sync had partial failures"
                    )
                    errors.append("Reconcile skipped due to partial sync failures")
                elif user_fetch_skipped_dept_count:
                    logger.warning(
                        f"[OrgSync] Skipping reconcile for provider {provider.id} because "
                        f"{user_fetch_skipped_dept_count} department user fetch(es) were skipped"
                    )
                    errors.append("Reconcile skipped because department user fetch was intentionally skipped")
                elif identity_conflict_count:
                    logger.warning(
                        f"[OrgSync] Skipping reconcile for provider {provider.id} because "
                        f"{identity_conflict_count} identity match(es) require review"
                    )
                else:
                    # Reconciliation: mark records not updated in this sync as deleted
                    await self._reconcile(db, provider.id, sync_start)
                    config = (self.provider.config or {}).copy()
                    config["last_synced_at"] = _utcnow().isoformat()
                    self.provider.config = config
                    await db.flush()

                # Recalculate member counts for all departments (crucial for DingTalk/WeCom)
                await self._update_member_counts(db, provider.id)
                if provider_tenant_id := getattr(provider, "tenant_id", None):
                    status_counts = await sync_tenant_user_statuses(
                        db,
                        tenant_id=provider_tenant_id,
                        changed_provider_id=provider.id,
                    )
                    tenant_users_enabled_count = status_counts["enabled"]
                    tenant_users_disabled_count = status_counts["disabled"]
                await db.flush()

        except Exception as e:
            import traceback
            logger.error(f"[OrgSync] Critical error during sync: {e}\n{traceback.format_exc()}")
            errors.append(f"Critical: {str(e)}")

        return {
            "departments": dept_count,
            "members": member_count,
            "users_created": user_count,
            "users_linked": user_linked_count,
            "global_identities_reused": global_identity_reused_count,
            "users_skipped_no_phone": user_skipped_no_phone_count,
            "users_skipped_requires_confirmation": user_skipped_confirmation_count,
            "user_fetch_skipped_departments": user_fetch_skipped_dept_count,
            "profiles_synced": profile_count,
            "identity_conflicts": identity_conflict_count,
            "legacy_splits_repaired": legacy_split_repaired_count,
            "tenant_users_enabled": tenant_users_enabled_count,
            "tenant_users_disabled": tenant_users_disabled_count,
            "errors": errors,
            "provider": self.provider_type,
            "synced_at": _utcnow().isoformat()
        }
