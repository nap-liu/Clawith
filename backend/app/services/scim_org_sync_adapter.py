"""Native SCIM 2.0 transport adapter into the shared directory facts."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org import (
    DirectoryAccountGroup,
    DirectoryGroupEdge,
    OrgDepartment,
    OrgMember,
)
from app.services.directory_user_status import sync_tenant_user_statuses
from app.services.org_sync_base import BaseOrgSyncAdapter
from app.services.org_sync_models import (
    ExternalDepartment,
    ExternalUser,
    configured_enterprise_root_name,
    normalize_enterprise_root,
)
from app.services.scim_client import ScimClient
from app.services.scim_directory import ScimDirectorySnapshot
from app.services.vendor_directory_snapshot import build_vendor_scim_snapshot

MAX_REPORTED_ERRORS = 50


def _report_error(errors: list[str], message: str) -> None:
    if len(errors) < MAX_REPORTED_ERRORS:
        errors.append(message)


class ScimOrgSyncAdapter(BaseOrgSyncAdapter):
    """Normalize native SCIM Users and Groups without assuming ID formats."""

    provider_type = "scim"

    @property
    def api_base_url(self) -> str:
        directory = (self.config or {}).get("directory") or {}
        return directory.get("base_url") or (self.config or {}).get("scim_base_url") or ""

    async def get_access_token(self) -> str:
        return ""

    async def fetch_departments(self) -> list[ExternalDepartment]:
        raise NotImplementedError("SCIM fetches one validated User/Group snapshot")

    async def fetch_users(self, department_external_id: str) -> list[ExternalUser]:
        raise NotImplementedError("SCIM fetches each User once across all Groups")

    async def sync_org_structure(self, db: AsyncSession) -> dict[str, Any]:
        client = ScimClient.from_provider_config(self.config or {})
        try:
            snapshot = await client.fetch_snapshot()
        finally:
            await client.close()

        await self._emit_sync_progress(
            stage="validating",
            processed=0,
            total=len(snapshot.users) + len(snapshot.groups),
            percent=15,
        )
        snapshot.validate()
        provider = await self._ensure_provider(db)
        departments = self._normalized_departments(snapshot)
        sync_start = datetime.now(timezone.utc)
        errors: list[str] = []

        group_count = await self._apply_groups(db, provider.id, departments, errors)
        await self._rebuild_department_paths(db, provider.id)
        await db.commit()
        user_stats = await self._apply_users(db, provider.id, snapshot, errors)

        await self._emit_sync_progress(
            stage="reconciling",
            processed=len(snapshot.users) + len(snapshot.groups),
            total=len(snapshot.users) + len(snapshot.groups),
            percent=92,
        )
        identity_conflicts = user_stats["identity_conflicts"]
        if not errors and not identity_conflicts:
            await self._reconcile(db, provider.id, sync_start)
        await self._refresh_member_department_paths(db, provider.id)
        await self._update_member_counts(db, provider.id)
        status_counts = await sync_tenant_user_statuses(
            db,
            tenant_id=provider.tenant_id,
            changed_provider_id=provider.id,
        )
        if not errors and not identity_conflicts:
            config = (provider.config or {}).copy()
            config["last_synced_at"] = datetime.now(timezone.utc).isoformat()
            provider.config = config
        await db.commit()

        return {
            "departments": group_count,
            "members": user_stats["members"],
            "users_created": user_stats["users_created"],
            "users_linked": user_stats["users_linked"],
            "profiles_synced": user_stats["profiles_synced"],
            "identity_conflicts": identity_conflicts,
            "tenant_users_enabled": status_counts["enabled"],
            "tenant_users_disabled": status_counts["disabled"],
            "errors": errors,
            "provider": "scim",
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }

    def _normalized_departments(
        self,
        snapshot: ScimDirectorySnapshot,
    ) -> list[ExternalDepartment]:
        parent_ids: dict[str, list[str]] = {group.id: [] for group in snapshot.groups}
        for parent_id, child_id in snapshot.group_edges():
            parent_ids[child_id].append(parent_id)
        departments = [
            ExternalDepartment(
                external_id=group.id,
                name=group.display_name,
                parent_external_id=(
                    sorted(parent_ids[group.id])[0] if parent_ids[group.id] else None
                ),
                parent_external_ids=sorted(parent_ids[group.id]),
            )
            for group in snapshot.groups
        ]
        return normalize_enterprise_root(
            departments,
            root_name=configured_enterprise_root_name(self.config),
        )

    async def _apply_groups(
        self,
        db: AsyncSession,
        provider_id,
        departments: list[ExternalDepartment],
        errors: list[str],
    ) -> int:
        parent_ids = {
            department.external_id: list(dict.fromkeys([
                *department.parent_external_ids,
                *([department.parent_external_id] if department.parent_external_id else []),
            ]))
            for department in departments
        }
        departments_by_id = {
            department.external_id: department for department in departments
        }
        normalized_snapshot = build_vendor_scim_snapshot(departments, [])
        ordered = [
            departments_by_id[group.id]
            for group in normalized_snapshot.topological_groups()
        ]
        for index, department in enumerate(ordered, start=1):
            try:
                async with db.begin_nested():
                    await self._upsert_department(
                        db,
                        self.provider,
                        ExternalDepartment(
                            external_id=department.external_id,
                            name=department.name,
                            parent_external_id=department.parent_external_id,
                            parent_external_ids=department.parent_external_ids,
                        ),
                    )
            except Exception:
                _report_error(errors, "A SCIM Group could not be applied")
            if index % 100 == 0:
                await db.commit()
            await self._emit_sync_progress(
                stage="applying_groups",
                processed=index,
                total=len(ordered),
                percent=15 + int(20 * index / max(len(ordered), 1)),
            )
        await db.commit()

        rows = (
            await db.execute(
                select(OrgDepartment).where(OrgDepartment.provider_id == provider_id)
            )
        ).scalars().all()
        group_db_ids = {row.external_id: row.id for row in rows}
        await db.execute(delete(DirectoryGroupEdge).where(DirectoryGroupEdge.provider_id == provider_id))
        for child_id, group_parent_ids in parent_ids.items():
            for parent_id in group_parent_ids:
                parent_db_id = group_db_ids.get(parent_id)
                child_db_id = group_db_ids.get(child_id)
                if parent_db_id is None or child_db_id is None:
                    _report_error(errors, "A SCIM Group edge could not be resolved")
                    continue
                db.add(
                    DirectoryGroupEdge(
                        tenant_id=self.tenant_id,
                        provider_id=provider_id,
                        parent_group_id=parent_db_id,
                        child_group_id=child_db_id,
                    )
                )
        await db.commit()
        return len(ordered)

    async def _apply_users(
        self,
        db: AsyncSession,
        provider_id,
        snapshot: ScimDirectorySnapshot,
        errors: list[str],
    ) -> dict[str, int]:
        groups_by_user = snapshot.user_groups()
        group_rows = (
            await db.execute(
                select(OrgDepartment).where(OrgDepartment.provider_id == provider_id)
            )
        ).scalars().all()
        group_db_ids = {row.external_id: row.id for row in group_rows}
        counters = {
            "members": 0,
            "users_created": 0,
            "users_linked": 0,
            "profiles_synced": 0,
            "identity_conflicts": 0,
        }
        for index, user in enumerate(snapshot.users, start=1):
            group_ids = list(groups_by_user[user.id])
            external_user = ExternalUser(
                external_id=user.id,
                name=user.display_name or user.user_name or user.id,
                email=user.emails[0] if user.emails else "",
                mobile=user.phone_numbers[0] if user.phone_numbers else "",
                avatar_url=user.photos[0] if user.photos else "",
                title=user.title or "",
                department_external_id=group_ids[0] if group_ids else "",
                department_path=user.organizational_path,
                department_ids=group_ids,
                status="active" if user.active else "inactive",
            )
            try:
                async with db.begin_nested():
                    stats = await self._upsert_member(db, self.provider, external_user, "")
                    member = (
                        await db.execute(
                            select(OrgMember).where(
                                OrgMember.provider_id == provider_id,
                                OrgMember.external_id == user.id,
                            )
                        )
                    ).scalars().first()
                    if member is None:
                        raise RuntimeError("SCIM account was not persisted")
                    await db.execute(
                        delete(DirectoryAccountGroup).where(
                            DirectoryAccountGroup.provider_id == provider_id,
                            DirectoryAccountGroup.account_id == member.id,
                        )
                    )
                    for position, group_id in enumerate(group_ids):
                        group_db_id = group_db_ids.get(group_id)
                        if group_db_id is None:
                            raise RuntimeError("SCIM account Group was not persisted")
                        db.add(
                            DirectoryAccountGroup(
                                tenant_id=self.tenant_id,
                                provider_id=provider_id,
                                account_id=member.id,
                                group_id=group_db_id,
                                is_primary=position == 0,
                            )
                        )
                counters["members"] += 1
                counters["users_created"] += int(bool(stats.get("user_created")))
                counters["users_linked"] += int(bool(stats.get("user_linked")))
                counters["profiles_synced"] += int(bool(stats.get("profile_synced")))
                counters["identity_conflicts"] += int(bool(stats.get("identity_conflict")))
            except Exception:
                _report_error(errors, "A SCIM User could not be applied")
            if index % 100 == 0:
                await db.commit()
            await self._emit_sync_progress(
                stage="applying_accounts",
                processed=index,
                total=len(snapshot.users),
                percent=35 + int(55 * index / max(len(snapshot.users), 1)),
            )
        await db.commit()
        return counters
