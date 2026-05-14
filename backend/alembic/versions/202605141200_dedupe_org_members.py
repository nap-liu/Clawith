"""dedupe org_members merge duplicate rows per user_id

Revision ID: 20260514_dedupe_org_mem
Revises: 20260512_mcp_allowlist
Create Date: 2026-05-14

Background: oauth2 SSO bug caused a new OrgMember row to be created on every
login (lookup chain missed because external_id/open_id/unionid were all None).
Production carried 289 active rows for 129 distinct users — 223 of those rows
are duplicates. The root-cause fix in app/services/sso_service.py prevents
future churn; this migration collapses the historical pile-up.

Algorithm (single transaction):
  1. Rank rows per user_id by canonical priority
  2. For each duplicate, repoint its agent_relationships rows to canonical
     (skipping ones that would conflict on (agent_id, canonical_member_id))
  3. Delete remaining conflicting agent_relationships rows
  4. Delete the duplicate org_members row

Idempotent: re-running on an already-deduped table finds zero candidates and
returns immediately.

NOT reversible: downgrade() raises. Recovery path = restore from the pg_dump
taken before upgrade (see /alidata/clawith/config/backups/).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = '20260514_dedupe_org_mem'
down_revision: Union[str, None] = '20260512_mcp_allowlist'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FIND_DUPLICATES_SQL = text("""
WITH ranked AS (
    SELECT
        om.id,
        om.user_id,
        ROW_NUMBER() OVER (
            PARTITION BY om.user_id
            ORDER BY
                (SELECT COUNT(*) FROM agent_relationships ar WHERE ar.member_id = om.id) DESC,
                (om.external_id IS NOT NULL) DESC,
                (om.name NOT LIKE 'Oauth2 User %' AND om.name NOT LIKE 'Dingtalk User %'
                 AND om.name NOT LIKE 'Wecom User %' AND om.name NOT LIKE 'Feishu User %') DESC,
                om.synced_at ASC
        ) AS rn
    FROM org_members om
    WHERE om.status = 'active' AND om.user_id IS NOT NULL
)
SELECT r.id AS dup_id, c.id AS canonical_id
FROM ranked r
JOIN ranked c ON c.user_id = r.user_id AND c.rn = 1
WHERE r.rn > 1
""")


def upgrade() -> None:
    conn = op.get_bind()
    pairs = conn.execute(_FIND_DUPLICATES_SQL).fetchall()
    if not pairs:
        return

    for dup_id, canonical_id in pairs:
        conn.execute(
            text("""
                UPDATE agent_relationships AS ar
                SET member_id = :canonical
                WHERE ar.member_id = :dup
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_relationships ar2
                      WHERE ar2.agent_id = ar.agent_id
                        AND ar2.member_id = :canonical
                  )
            """),
            {"canonical": canonical_id, "dup": dup_id},
        )
        conn.execute(
            text("DELETE FROM agent_relationships WHERE member_id = :dup"),
            {"dup": dup_id},
        )
        conn.execute(
            text("DELETE FROM org_members WHERE id = :dup"),
            {"dup": dup_id},
        )


def downgrade() -> None:
    raise NotImplementedError(
        "Data migration is not reversible; restore from pg_dump backup taken before upgrade."
    )
