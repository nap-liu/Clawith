"""Add cli_tool_binary_versions (binary version history + rollback).

Revision ID: 20260416_cli_tool_binary_versions
Revises: f8a934bf9f17
Create Date: 2026-04-16

Seed rule: for every existing ``tools`` row with ``type='cli'`` whose
``config.binary.sha256`` is populated, insert a single row marked
``is_current=TRUE`` so the freshly-created history starts consistent.
``uploaded_at`` is lifted from the config; ``uploaded_by_user_id`` is
left NULL for these legacy seeds (we don't know who uploaded them).
"""
from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision = "20260416_binary_versions"
down_revision = "f8a934bf9f17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cli_tool_binary_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tool_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "uploaded_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "is_current",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("notes", sa.String(500), nullable=True),
    )

    # Non-unique composite for "list versions for tool, newest first".
    op.create_index(
        "ix_cli_tool_binary_versions_tool_id_uploaded_at",
        "cli_tool_binary_versions",
        ["tool_id", "uploaded_at"],
    )

    # Partial unique: at most one current version per tool. Postgres-only
    # syntax; this project runs on postgres in production (asyncpg) and
    # sqlite/aiosqlite in tests. SQLite tolerates the CREATE UNIQUE INDEX
    # with WHERE clause natively, so the same DDL is safe in both.
    op.execute(
        "CREATE UNIQUE INDEX uq_cli_tool_binary_versions_current "
        "ON cli_tool_binary_versions (tool_id) WHERE is_current = TRUE"
    )

    # ─── Data migration: seed one row per tool whose config.binary.sha256
    # is populated. Runs against whatever dialect alembic is bound to; we
    # read the row with the connection's default JSON decoding and write
    # back with portable SQL. ───────────────────────────────────────────
    bind = op.get_bind()
    tools = bind.execute(
        sa.text("SELECT id, config FROM tools WHERE type = 'cli'")
    ).mappings().all()

    now_iso = datetime.now(timezone.utc)

    for row in tools:
        config = row["config"] or {}
        # The config may be a dict (postgres JSONB) or a string (sqlite JSON
        # stored as text) depending on the driver. Normalise.
        if isinstance(config, str):
            import json

            try:
                config = json.loads(config)
            except (ValueError, TypeError):
                continue
        if not isinstance(config, dict):
            continue

        binary = config.get("binary") or {}
        # Also accept the legacy flat shape (pre-schema-refactor).
        sha = None
        size = None
        original_name = None
        uploaded_at = None
        if isinstance(binary, dict) and binary.get("sha256"):
            sha = binary.get("sha256")
            size = binary.get("size") or 0
            original_name = binary.get("original_name") or "legacy.bin"
            uploaded_at = binary.get("uploaded_at")
        elif config.get("binary_sha256"):
            sha = config.get("binary_sha256")
            size = config.get("binary_size") or 0
            original_name = config.get("binary_original_name") or "legacy.bin"
            uploaded_at = config.get("binary_uploaded_at")

        if not sha or not isinstance(sha, str) or len(sha) != 64:
            continue

        # uploaded_at may arrive as an ISO string; let the DB coerce it.
        # If absent, default to now.
        if uploaded_at is None:
            uploaded_ts: datetime = now_iso
        elif isinstance(uploaded_at, datetime):
            uploaded_ts = uploaded_at
        else:
            try:
                uploaded_ts = datetime.fromisoformat(str(uploaded_at).replace("Z", "+00:00"))
            except ValueError:
                uploaded_ts = now_iso

        bind.execute(
            sa.text(
                """
                INSERT INTO cli_tool_binary_versions
                  (id, tool_id, sha256, size, original_name,
                   uploaded_at, uploaded_by_user_id, is_current, notes)
                VALUES
                  (:id, :tool_id, :sha256, :size, :original_name,
                   :uploaded_at, NULL, TRUE, :notes)
                """
            ),
            {
                "id": _new_uuid(),
                "tool_id": row["id"],
                "sha256": sha,
                "size": int(size or 0),
                "original_name": str(original_name)[:255],
                "uploaded_at": uploaded_ts,
                "notes": "seeded by cli_tool_binary_versions migration",
            },
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_cli_tool_binary_versions_current")
    op.drop_index(
        "ix_cli_tool_binary_versions_tool_id_uploaded_at",
        table_name="cli_tool_binary_versions",
    )
    op.drop_table("cli_tool_binary_versions")


def _new_uuid() -> str:
    import uuid

    return str(uuid.uuid4())
