"""Add provider-scoped directory graph, scheduling, and sync runs.

Revision ID: directory_sync_foundation
Revises: agent_runtime_model_overrides
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "directory_sync_foundation"
down_revision = "agent_runtime_model_overrides"
branch_labels = None
depends_on = None


def _scalar(sql: str) -> int:
    return int(op.get_bind().execute(sa.text(sql)).scalar() or 0)


def _non_blank_values(rows, field: str) -> set[object]:
    return {
        row[field]
        for row in rows
        if row[field] is not None and (not isinstance(row[field], str) or row[field])
    }


def _consolidate_safe_member_duplicates() -> int:
    """Collapse legacy copies only when every identity-bearing field agrees."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT member.id, member.provider_id, member.tenant_id,
                   member.external_id, member.user_id, member.department_id,
                   member.open_id, member.unionid, member.phone, member.email
            FROM org_members AS member
            JOIN (
                SELECT provider_id, external_id
                FROM org_members
                WHERE provider_id IS NOT NULL AND external_id IS NOT NULL
                  AND external_id <> ''
                GROUP BY provider_id, external_id
                HAVING count(*) > 1
            ) AS duplicate
              ON duplicate.provider_id = member.provider_id
             AND duplicate.external_id = member.external_id
            ORDER BY member.provider_id, member.external_id,
                     member.synced_at DESC NULLS LAST, member.id
            """
        )
    ).mappings().all()
    groups: dict[tuple[object, object], list] = {}
    for row in rows:
        groups.setdefault((row["provider_id"], row["external_id"]), []).append(row)

    merge_fields = ("department_id", "open_id", "unionid", "phone", "email")
    plans = []
    for key, copies in groups.items():
        tenant_values = _non_blank_values(copies, "tenant_id")
        user_values = _non_blank_values(copies, "user_id")
        conflicting_fields = [
            field for field in merge_fields
            if len(_non_blank_values(copies, field)) > 1
        ]
        if (
            len(tenant_values) != 1
            or any(copy["tenant_id"] is None for copy in copies)
            or len(user_values) != 1
            or any(copy["user_id"] is None for copy in copies)
            or conflicting_fields
        ):
            raise RuntimeError(
                "org_members contains duplicate provider external-id rows with "
                f"conflicting identity data for provider key {key[0]}"
            )
        survivor = copies[0]
        merged = {
            field: survivor[field]
            or next(iter(_non_blank_values(copies, field)), None)
            for field in merge_fields
        }
        plans.append((survivor["id"], [copy["id"] for copy in copies[1:]], merged))

    loser_parameter = sa.bindparam("loser_ids", expanding=True)
    for survivor_id, loser_ids, merged in plans:
        bind.execute(
            sa.text(
                """
                UPDATE org_members
                SET department_id = :department_id, open_id = :open_id,
                    unionid = :unionid, phone = :phone, email = :email
                WHERE id = :survivor_id
                """
            ),
            {"survivor_id": survivor_id, **merged},
        )
        bind.execute(
            sa.text(
                "UPDATE agent_relationships SET member_id = :survivor_id "
                "WHERE member_id IN :loser_ids"
            ).bindparams(loser_parameter),
            {"survivor_id": survivor_id, "loser_ids": loser_ids},
        )
        bind.execute(
            sa.text("DELETE FROM org_members WHERE id IN :loser_ids").bindparams(
                loser_parameter
            ),
            {"loser_ids": loser_ids},
        )
    return sum(len(loser_ids) for _, loser_ids, _ in plans)


def _validate_legacy_directory_rows() -> None:
    for table in ("org_departments", "org_members"):
        duplicates = _scalar(
            f"""
            SELECT count(*) FROM (
                SELECT provider_id, external_id
                FROM {table}
                WHERE provider_id IS NOT NULL AND external_id IS NOT NULL
                  AND external_id <> ''
                GROUP BY provider_id, external_id
                HAVING count(*) > 1
            ) AS duplicate_keys
            """
        )
        if duplicates:
            raise RuntimeError(
                f"{table} contains {duplicates} duplicate provider external-id key(s); "
                "repair them before applying directory_sync_foundation"
            )
        invalid_provider_scopes = _scalar(
            f"""
            SELECT count(*)
            FROM {table} AS item
            LEFT JOIN identity_providers AS provider ON provider.id = item.provider_id
            WHERE item.provider_id IS NOT NULL
              AND (
                  provider.id IS NULL
                  OR provider.tenant_id IS NULL
                  OR item.tenant_id IS NULL
                  OR item.tenant_id IS DISTINCT FROM provider.tenant_id
              )
            """
        )
        if invalid_provider_scopes:
            raise RuntimeError(
                f"{table} contains {invalid_provider_scopes} invalid provider/tenant scope(s); "
                "repair them before applying directory_sync_foundation"
            )


def _ensure_scope_constraints(inspector) -> None:
    targets = (
        ("identity_providers", "uq_identity_provider_tenant", ["id", "tenant_id"]),
        ("org_departments", "uq_org_department_scope", ["id", "tenant_id", "provider_id"]),
        ("org_members", "uq_org_member_scope", ["id", "tenant_id", "provider_id"]),
    )
    for table, name, columns in targets:
        existing = {item["name"] for item in inspector.get_unique_constraints(table)}
        if name not in existing:
            op.create_unique_constraint(name, table, columns)


def _ensure_external_id_indexes(inspector) -> None:
    for table in ("org_departments", "org_members"):
        name = f"uq_{table}_provider_external_id"
        existing = {item["name"] for item in inspector.get_indexes(table)}
        if name not in existing:
            op.create_index(
                name,
                table,
                ["provider_id", "external_id"],
                unique=True,
                postgresql_where=sa.text(
                    "provider_id IS NOT NULL AND external_id IS NOT NULL "
                    "AND external_id <> ''"
                ),
            )


def _migrate_channel_binding_uniqueness(inspector) -> None:
    constraints = {
        item["name"]
        for item in inspector.get_unique_constraints("channel_user_bindings")
    }
    indexes = {item["name"] for item in inspector.get_indexes("channel_user_bindings")}
    if "uq_channel_user_binding_subject" in constraints:
        op.drop_constraint(
            "uq_channel_user_binding_subject",
            "channel_user_bindings",
            type_="unique",
        )
    if "uq_channel_user_binding_subject_provider" not in indexes:
        op.create_index(
            "uq_channel_user_binding_subject_provider",
            "channel_user_bindings",
            [
                "tenant_id",
                "provider_id",
                "installation_scope",
                "channel_type",
                "id_type",
                "subject",
            ],
            unique=True,
            postgresql_where=sa.text("provider_id IS NOT NULL"),
        )
    if "uq_channel_user_binding_subject_providerless" not in indexes:
        op.create_index(
            "uq_channel_user_binding_subject_providerless",
            "channel_user_bindings",
            ["tenant_id", "installation_scope", "channel_type", "id_type", "subject"],
            unique=True,
            postgresql_where=sa.text("provider_id IS NULL"),
        )


def _validate_precreated_fact_tables(inspector, tables: set[str]) -> None:
    required_columns = {
        "directory_group_edges": {
            "id", "tenant_id", "provider_id", "parent_group_id", "child_group_id"
        },
        "directory_account_groups": {
            "id", "tenant_id", "provider_id", "account_id", "group_id", "is_primary"
        },
        "directory_sync_runs": {
            "id", "tenant_id", "provider_id", "trigger_type", "status", "stage",
            "processed_items", "total_items", "progress_percent", "stats",
            "error_summary", "created_by_user_id", "started_at", "finished_at",
            "created_at",
        },
    }
    for table, expected in required_columns.items():
        if table not in tables:
            continue
        actual = {column["name"] for column in inspector.get_columns(table)}
        missing = sorted(expected - actual)
        if missing:
            raise RuntimeError(
                f"pre-created {table} is incomplete; missing columns: {', '.join(missing)}"
            )
        primary_key = set(inspector.get_pk_constraint(table).get("constrained_columns") or [])
        if primary_key != {"id"}:
            raise RuntimeError(f"pre-created {table} has an invalid primary key")

    required_named_constraints = {
        "directory_group_edges": {
            "uq_directory_group_edge", "ck_directory_group_edge_not_self"
        },
        "directory_account_groups": {"uq_directory_account_group"},
        "directory_sync_runs": {
            "ck_directory_sync_runs_trigger_type",
            "ck_directory_sync_runs_status",
            "ck_directory_sync_runs_progress",
        },
    }
    for table, expected in required_named_constraints.items():
        if table not in tables:
            continue
        actual = {
            item["name"] for item in inspector.get_unique_constraints(table)
        } | {item["name"] for item in inspector.get_check_constraints(table)}
        if not expected.issubset(actual):
            raise RuntimeError(f"pre-created {table} is missing required constraints")

    required_foreign_keys = {
        "directory_group_edges": {
            ("tenant_id",),
            ("provider_id", "tenant_id"),
            ("parent_group_id", "tenant_id", "provider_id"),
            ("child_group_id", "tenant_id", "provider_id"),
        },
        "directory_account_groups": {
            ("tenant_id",),
            ("provider_id", "tenant_id"),
            ("account_id", "tenant_id", "provider_id"),
            ("group_id", "tenant_id", "provider_id"),
        },
        "directory_sync_runs": {
            ("tenant_id",), ("provider_id", "tenant_id"), ("created_by_user_id",)
        },
    }
    for table, expected in required_foreign_keys.items():
        if table not in tables:
            continue
        actual = {
            tuple(item["constrained_columns"])
            for item in inspector.get_foreign_keys(table)
        }
        if not expected.issubset(actual):
            raise RuntimeError(f"pre-created {table} is missing required foreign keys")


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    _validate_precreated_fact_tables(inspector, tables)
    provider_columns = {
        column["name"] for column in inspector.get_columns("identity_providers")
    }
    op.alter_column(
        "org_departments",
        "external_id",
        existing_type=sa.String(100),
        type_=sa.Text(),
        existing_nullable=True,
    )

    provider_column_specs = {
        "sync_enabled": sa.Column(
            "sync_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        "sync_interval_value": sa.Column("sync_interval_value", sa.Integer()),
        "sync_interval_unit": sa.Column("sync_interval_unit", sa.String(10)),
        "next_sync_at": sa.Column("next_sync_at", sa.DateTime(timezone=True)),
        "last_sync_attempt_at": sa.Column(
            "last_sync_attempt_at", sa.DateTime(timezone=True)
        ),
        "last_sync_success_at": sa.Column(
            "last_sync_success_at", sa.DateTime(timezone=True)
        ),
    }
    for column_name, column in provider_column_specs.items():
        if column_name not in provider_columns:
            op.add_column("identity_providers", column)

    provider_constraints = {
        item["name"] for item in inspector.get_unique_constraints("identity_providers")
    } | {item["name"] for item in inspector.get_check_constraints("identity_providers")}
    if "ck_identity_providers_sync_interval_unit" not in provider_constraints:
        op.create_check_constraint(
            "ck_identity_providers_sync_interval_unit",
            "identity_providers",
            "sync_interval_unit IS NULL OR sync_interval_unit IN ('hour', 'day', 'week', 'month')",
        )
    if "ck_identity_providers_sync_interval_value" not in provider_constraints:
        op.create_check_constraint(
            "ck_identity_providers_sync_interval_value",
            "identity_providers",
            "sync_interval_value IS NULL OR sync_interval_value > 0",
        )
    provider_indexes = {item["name"] for item in inspector.get_indexes("identity_providers")}
    if "ix_identity_providers_next_sync_at" not in provider_indexes:
        op.create_index(
            "ix_identity_providers_next_sync_at", "identity_providers", ["next_sync_at"]
        )

    _consolidate_safe_member_duplicates()
    _validate_legacy_directory_rows()
    _ensure_scope_constraints(inspector)
    _ensure_external_id_indexes(inspector)
    _migrate_channel_binding_uniqueness(inspector)

    op.create_table(
        "directory_group_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("parent_group_id <> child_group_id", name="ck_directory_group_edge_not_self"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["provider_id", "tenant_id"],
            ["identity_providers.id", "identity_providers.tenant_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_group_id", "tenant_id", "provider_id"],
            ["org_departments.id", "org_departments.tenant_id", "org_departments.provider_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["child_group_id", "tenant_id", "provider_id"],
            ["org_departments.id", "org_departments.tenant_id", "org_departments.provider_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_id", "parent_group_id", "child_group_id", name="uq_directory_group_edge"),
        if_not_exists=True,
    )
    op.create_index("ix_directory_group_edges_tenant_id", "directory_group_edges", ["tenant_id"], if_not_exists=True)
    op.create_index("ix_directory_group_edges_provider_id", "directory_group_edges", ["provider_id"], if_not_exists=True)
    op.create_index("ix_directory_group_edges_child_group_id", "directory_group_edges", ["child_group_id"], if_not_exists=True)

    op.create_table(
        "directory_account_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["provider_id", "tenant_id"],
            ["identity_providers.id", "identity_providers.tenant_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "tenant_id", "provider_id"],
            ["org_members.id", "org_members.tenant_id", "org_members.provider_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id", "tenant_id", "provider_id"],
            ["org_departments.id", "org_departments.tenant_id", "org_departments.provider_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_id", "account_id", "group_id", name="uq_directory_account_group"),
        if_not_exists=True,
    )
    op.create_index("ix_directory_account_groups_tenant_id", "directory_account_groups", ["tenant_id"], if_not_exists=True)
    op.create_index("ix_directory_account_groups_provider_id", "directory_account_groups", ["provider_id"], if_not_exists=True)
    op.create_index("ix_directory_account_groups_account_id", "directory_account_groups", ["account_id"], if_not_exists=True)
    op.create_index("ix_directory_account_groups_group_id", "directory_account_groups", ["group_id"], if_not_exists=True)

    op.create_table(
        "directory_sync_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("stage", sa.String(50), nullable=False, server_default="queued"),
        sa.Column("processed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_items", sa.Integer()),
        sa.Column("progress_percent", sa.Integer(), server_default="0"),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("error_summary", sa.Text()),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("trigger_type IN ('manual', 'scheduled')", name="ck_directory_sync_runs_trigger_type"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'needs_review', 'partial_failed', 'failed', 'cancelled')",
            name="ck_directory_sync_runs_status",
        ),
        sa.CheckConstraint(
            "progress_percent IS NULL OR (progress_percent >= 0 AND progress_percent <= 100)",
            name="ck_directory_sync_runs_progress",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["provider_id", "tenant_id"],
            ["identity_providers.id", "identity_providers.tenant_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index("ix_directory_sync_runs_tenant_id", "directory_sync_runs", ["tenant_id"], if_not_exists=True)
    op.create_index("ix_directory_sync_runs_provider_id", "directory_sync_runs", ["provider_id"], if_not_exists=True)
    op.create_index("ix_directory_sync_runs_status", "directory_sync_runs", ["status"], if_not_exists=True)
    op.create_index("ix_directory_sync_runs_created_at", "directory_sync_runs", ["created_at"], if_not_exists=True)
    op.create_index(
        "uq_directory_sync_runs_active_provider",
        "directory_sync_runs",
        ["provider_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
        if_not_exists=True,
    )

    # Preserve today's single-parent/single-department view as the first fact set.
    op.execute(
        """
        INSERT INTO directory_group_edges
            (id, tenant_id, provider_id, parent_group_id, child_group_id)
        SELECT md5('directory-edge:' || d.id::text)::uuid,
               d.tenant_id, d.provider_id, d.parent_id, d.id
        FROM org_departments AS d
        JOIN org_departments AS p ON p.id = d.parent_id
        JOIN identity_providers AS ip
          ON ip.id = d.provider_id AND ip.tenant_id = d.tenant_id
        WHERE d.tenant_id IS NOT NULL AND d.provider_id IS NOT NULL
          AND p.tenant_id = d.tenant_id AND p.provider_id = d.provider_id
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO directory_account_groups
            (id, tenant_id, provider_id, account_id, group_id, is_primary)
        SELECT md5('directory-membership:' || m.id::text || ':' || m.department_id::text)::uuid,
               m.tenant_id, m.provider_id, m.id, m.department_id, true
        FROM org_members AS m
        JOIN org_departments AS d ON d.id = m.department_id
        JOIN identity_providers AS ip
          ON ip.id = m.provider_id AND ip.tenant_id = m.tenant_id
        WHERE m.tenant_id IS NOT NULL AND m.provider_id IS NOT NULL
          AND d.tenant_id = m.tenant_id AND d.provider_id = m.provider_id
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("directory_sync_runs")
    op.drop_table("directory_account_groups")
    op.drop_table("directory_group_edges")
    op.execute("DROP INDEX IF EXISTS uq_org_members_provider_external_id")
    op.execute("DROP INDEX IF EXISTS uq_org_departments_provider_external_id")
    op.execute("ALTER TABLE org_members DROP CONSTRAINT IF EXISTS uq_org_member_scope")
    op.execute("ALTER TABLE org_departments DROP CONSTRAINT IF EXISTS uq_org_department_scope")
    op.execute("ALTER TABLE identity_providers DROP CONSTRAINT IF EXISTS uq_identity_provider_tenant")
    op.execute("DROP INDEX IF EXISTS uq_channel_user_binding_subject_provider")
    op.execute("DROP INDEX IF EXISTS uq_channel_user_binding_subject_providerless")
    op.create_unique_constraint(
        "uq_channel_user_binding_subject",
        "channel_user_bindings",
        ["tenant_id", "installation_scope", "id_type", "subject"],
    )
    op.drop_index("ix_identity_providers_next_sync_at", table_name="identity_providers")
    op.drop_constraint("ck_identity_providers_sync_interval_value", "identity_providers", type_="check")
    op.drop_constraint("ck_identity_providers_sync_interval_unit", "identity_providers", type_="check")
    op.drop_column("identity_providers", "last_sync_success_at")
    op.drop_column("identity_providers", "last_sync_attempt_at")
    op.drop_column("identity_providers", "next_sync_at")
    op.drop_column("identity_providers", "sync_interval_unit")
    op.drop_column("identity_providers", "sync_interval_value")
    op.drop_column("identity_providers", "sync_enabled")
    op.alter_column(
        "org_departments",
        "external_id",
        existing_type=sa.Text(),
        type_=sa.String(100),
        existing_nullable=True,
    )
