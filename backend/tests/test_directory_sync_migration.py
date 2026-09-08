"""Behavior checks for fail-closed legacy directory migration validation."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa


def _load_migration_module(
    filename: str = "202609021300_directory_sync_foundation.py",
):
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / filename
    )
    spec = importlib.util.spec_from_file_location("directory_sync_foundation_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_legacy_tables(connection):
    connection.execute(
        sa.text("CREATE TABLE identity_providers (id TEXT PRIMARY KEY, tenant_id TEXT)")
    )
    connection.execute(
        sa.text(
            "CREATE TABLE org_departments (id TEXT PRIMARY KEY, provider_id TEXT, "
            "tenant_id TEXT, external_id TEXT)"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TABLE org_members (id TEXT PRIMARY KEY, provider_id TEXT, "
            "tenant_id TEXT, external_id TEXT, user_id TEXT, department_id TEXT, "
            "open_id TEXT, unionid TEXT, phone TEXT, email TEXT, synced_at TEXT)"
        )
    )
    connection.execute(
        sa.text("CREATE TABLE agent_relationships (id TEXT PRIMARY KEY, member_id TEXT)")
    )


def test_legacy_directory_validation_allows_platform_local_rows(monkeypatch):
    migration = _load_migration_module()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_tables(connection)
        connection.execute(
            sa.text(
                "INSERT INTO org_members (id, provider_id, tenant_id, external_id) "
                "VALUES ('local', NULL, NULL, NULL)"
            )
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration._validate_legacy_directory_rows()
    engine.dispose()


def test_cleanup_dingtalk_root_rewrites_only_exact_legacy_placeholder():
    migration = _load_migration_module(
        "202609031500_cleanup_dingtalk_root.py"
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL)"
        ))
        connection.execute(sa.text(
            "CREATE TABLE identity_providers ("
            "id TEXT PRIMARY KEY, tenant_id TEXT, provider_type TEXT)"
        ))
        connection.execute(sa.text(
            "CREATE TABLE org_departments ("
            "id TEXT PRIMARY KEY, provider_id TEXT, tenant_id TEXT, "
            "external_id TEXT, name TEXT, parent_id TEXT, path TEXT)"
        ))
        connection.execute(sa.text(
            "CREATE TABLE org_members ("
            "id TEXT PRIMARY KEY, provider_id TEXT, tenant_id TEXT, "
            "department_path TEXT)"
        ))
        connection.execute(sa.text(
            "INSERT INTO tenants VALUES ('tenant', '爷爷不泡茶')"
        ))
        connection.execute(sa.text(
            "INSERT INTO identity_providers VALUES "
            "('ding', 'tenant', 'dingtalk'), "
            "('scim', 'tenant', 'scim')"
        ))
        connection.execute(sa.text(
            "INSERT INTO org_departments VALUES "
            "('root', 'ding', 'tenant', '1', 'Root', NULL, 'Root'), "
            "('child', 'ding', 'tenant', '2', '技术部', 'root', 'Root/技术部'), "
            "('other', 'scim', 'tenant', '1', 'Root', NULL, 'Root')"
        ))
        connection.execute(sa.text(
            "INSERT INTO org_members VALUES "
            "('member', 'ding', 'tenant', 'Root/技术部')"
        ))

        assert migration._cleanup_legacy_dingtalk_roots(connection) == 1

        rows = dict(connection.execute(sa.text(
            "SELECT id, name || '|' || path FROM org_departments"
        )).all())
        member_path = connection.execute(sa.text(
            "SELECT department_path FROM org_members WHERE id='member'"
        )).scalar_one()
        assert rows["root"] == "爷爷不泡茶|爷爷不泡茶"
        assert rows["child"] == "技术部|爷爷不泡茶/技术部"
        assert rows["other"] == "Root|Root"
        assert member_path == "爷爷不泡茶/技术部"

    engine.dispose()


def test_safe_member_duplicates_keep_latest_and_repoint_relationship(monkeypatch):
    migration = _load_migration_module()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_tables(connection)
        connection.execute(
            sa.text(
                "INSERT INTO org_members "
                "(id, provider_id, tenant_id, external_id, user_id, department_id, "
                "unionid, phone, synced_at) VALUES "
                "('old', 'provider', 'tenant', 'staff', 'user', 'department', "
                "'union', 'phone', '2026-01-01'), "
                "('latest', 'provider', 'tenant', 'staff', 'user', NULL, "
                "'union', 'phone', '2026-02-01')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO agent_relationships (id, member_id) "
                "VALUES ('relationship', 'old')"
            )
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

        assert migration._consolidate_safe_member_duplicates() == 1
        member = connection.execute(
            sa.text("SELECT id, department_id FROM org_members")
        ).one()
        relationship_member = connection.execute(
            sa.text("SELECT member_id FROM agent_relationships")
        ).scalar_one()

        assert member == ("latest", "department")
        assert relationship_member == "latest"

    engine.dispose()


def test_conflicting_member_duplicates_fail_before_writes(monkeypatch):
    migration = _load_migration_module()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_tables(connection)
        connection.execute(
            sa.text(
                "INSERT INTO org_members "
                "(id, provider_id, tenant_id, external_id, user_id, phone, synced_at) "
                "VALUES ('first', 'provider', 'tenant', 'staff', 'user-a', "
                "'phone', '2026-01-01'), ('second', 'provider', 'tenant', "
                "'staff', 'user-b', 'phone', '2026-02-01')"
            )
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

        with pytest.raises(RuntimeError, match="conflicting identity data"):
            migration._consolidate_safe_member_duplicates()

        assert connection.execute(sa.text("SELECT count(*) FROM org_members")).scalar() == 2

    engine.dispose()


@pytest.mark.parametrize(
    ("provider_values", "member_values"),
    [
        (None, "('member', 'missing-provider', 'tenant-a', 'external-a')"),
        ("('provider', NULL)", "('member', 'provider', 'tenant-a', 'external-a')"),
        ("('provider', 'tenant-a')", "('member', 'provider', NULL, 'external-a')"),
        ("('provider', 'tenant-a')", "('member', 'provider', 'tenant-b', 'external-a')"),
    ],
)
def test_legacy_directory_validation_rejects_invalid_provider_scope(
    monkeypatch,
    provider_values,
    member_values,
):
    migration = _load_migration_module()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_tables(connection)
        if provider_values:
            connection.execute(
                sa.text(
                    "INSERT INTO identity_providers (id, tenant_id) VALUES "
                    + provider_values
                )
            )
        connection.execute(
            sa.text(
                "INSERT INTO org_members (id, provider_id, tenant_id, external_id) VALUES "
                + member_values
            )
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        with pytest.raises(RuntimeError, match="invalid provider/tenant scope"):
            migration._validate_legacy_directory_rows()
    engine.dispose()
