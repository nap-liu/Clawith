"""Organization-sync lifecycle orchestration shared by provider adapters."""

from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.org_sync_models import _utcnow


class OrgSyncLifecycleMixin:
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
        sync_start = _utcnow()
        partial_failure = False

        # Ensure provider exists
        provider = await self._ensure_provider(db)

        try:
            # Fetch and sync departments
            departments = await self.fetch_departments()
            for dept in departments:
                try:
                    async with db.begin_nested():
                        await self._upsert_department(db, provider, dept)
                    dept_count += 1
                except Exception as e:
                    partial_failure = True
                    errors.append(f"Department {dept.external_id}: {str(e)}")
                    logger.error(f"[OrgSync] Failed to sync department {dept.external_id}: {e}")

            await self._rebuild_department_paths(db, provider.id)
            await db.flush()
            per_member_transactions = (
                (
                    getattr(provider, "provider_type", None)
                    or self.provider_type
                    or ""
                ).lower()
                == "dingtalk"
                and isinstance(db, AsyncSession)
            )
            if per_member_transactions:
                # Make the normalized department tree visible, then give each
                # DingTalk employee a real top-level transaction. This releases
                # advisory plus member/User/Identity row locks together instead
                # of retaining them for the entire full sync.
                await db.commit()

            # Fetch and sync users (from all departments)
            for dept in departments:
                if self._should_skip_department_user_fetch(dept):
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
                    continue

                for user in users:
                    try:
                        if per_member_transactions:
                            stats = await self._upsert_member_in_short_transaction(
                                provider.id,
                                user,
                                dept.external_id,
                            )
                        else:
                            async with db.begin_nested():
                                stats = await self._upsert_member(
                                    db,
                                    provider,
                                    user,
                                    dept.external_id,
                                )
                        if stats.get("user_created"):
                            user_count += 1
                        if stats.get("user_linked"):
                            user_linked_count += 1
                        if stats.get("global_identity_matched_no_tenant_user"):
                            global_identity_reused_count += 1
                        if stats.get("user_skipped_no_phone"):
                            user_skipped_no_phone_count += 1
                        if stats.get("user_skipped_requires_confirmation"):
                            user_skipped_confirmation_count += 1
                        if stats.get("profile_synced"):
                            profile_count += 1
                        if stats.get("identity_conflict"):
                            identity_conflict_count += 1
                        if stats.get("legacy_split_repaired"):
                            legacy_split_repaired_count += 1
                        member_count += 1
                    except Exception as e:
                        partial_failure = True
                        logger.error(f"[OrgSync] Failed to sync member {user.external_id} ({user.name}): {e}")
                        errors.append(f"Member {user.external_id}: {str(e)}")

            await self._refresh_member_department_paths(db, provider.id)
            await db.flush()

            # Update provider metadata if possible
            if self.provider:
                config = (self.provider.config or {}).copy()
                config["last_synced_at"] = _utcnow().isoformat()
                self.provider.config = config
                await db.flush()

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
                else:
                    # Reconciliation: mark records not updated in this sync as deleted
                    await self._reconcile(db, provider.id, sync_start)
                    await db.flush()

                # Recalculate member counts for all departments (crucial for DingTalk/WeCom)
                await self._update_member_counts(db, provider.id)
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
            "errors": errors,
            "provider": self.provider_type,
            "synced_at": _utcnow().isoformat()
        }
