"""Replace the legacy DingTalk transport root with the tenant enterprise name.

Revision ID: cleanup_dingtalk_root
Revises: directory_sync_foundation
"""

import sqlalchemy as sa
from alembic import op


revision = "cleanup_dingtalk_root"
down_revision = "directory_sync_foundation"
branch_labels = None
depends_on = None


def _mapped_path(path: str, old_name: str, new_name: str) -> str:
    suffix = path[len(old_name):] if path.startswith(old_name) else ""
    return f"{new_name}{suffix}"[:500]


def _cleanup_legacy_dingtalk_roots(bind) -> int:
    """Clean only exact historical roots and return the changed root count."""
    roots = bind.execute(sa.text(
        """
        SELECT department.id AS root_id,
               department.provider_id,
               department.tenant_id,
               department.name AS old_name,
               tenant.name AS new_name
        FROM org_departments AS department
        JOIN identity_providers AS provider
          ON provider.id = department.provider_id
         AND provider.tenant_id = department.tenant_id
        JOIN tenants AS tenant ON tenant.id = department.tenant_id
        WHERE provider.provider_type = 'dingtalk'
          AND department.parent_id IS NULL
          AND department.external_id = '1'
          AND lower(trim(department.name)) = 'root'
        """
    )).mappings().all()
    for root in roots:
        parameters = dict(root)
        departments = bind.execute(
            sa.text(
                """
                SELECT id, path FROM org_departments
                WHERE provider_id = :provider_id
                  AND tenant_id = :tenant_id
                  AND (
                    id = :root_id OR path = :old_name
                    OR path LIKE :path_prefix
                  )
                """
            ),
            {**parameters, "path_prefix": f"{root['old_name']}/%"},
        ).mappings().all()
        department_updates = [
            {
                "row_id": row["id"],
                "mapped_path": _mapped_path(
                    row["path"] or "", root["old_name"], root["new_name"]
                ),
            }
            for row in departments
        ]
        if department_updates:
            bind.execute(
                sa.text(
                    "UPDATE org_departments SET path = :mapped_path "
                    "WHERE id = :row_id"
                ),
                department_updates,
            )
        members = bind.execute(
            sa.text(
                """
                SELECT id, department_path FROM org_members
                WHERE provider_id = :provider_id
                  AND tenant_id = :tenant_id
                  AND (
                    department_path = :old_name
                    OR department_path LIKE :path_prefix
                  )
                """
            ),
            {**parameters, "path_prefix": f"{root['old_name']}/%"},
        ).mappings().all()
        member_updates = [
            {
                "row_id": row["id"],
                "mapped_path": _mapped_path(
                    row["department_path"] or "",
                    root["old_name"],
                    root["new_name"],
                ),
            }
            for row in members
        ]
        if member_updates:
            bind.execute(
                sa.text(
                    "UPDATE org_members SET department_path = :mapped_path "
                    "WHERE id = :row_id"
                ),
                member_updates,
            )
        bind.execute(
            sa.text(
                """
                UPDATE org_departments
                SET name = :new_name
                WHERE id = :root_id
                  AND provider_id = :provider_id
                  AND tenant_id = :tenant_id
                """
            ),
            parameters,
        )
    return len(roots)


def upgrade() -> None:
    """Replace historical DingTalk transport roots with tenant names."""
    _cleanup_legacy_dingtalk_roots(op.get_bind())


def downgrade() -> None:
    """Do not recreate a known invalid transport placeholder."""
