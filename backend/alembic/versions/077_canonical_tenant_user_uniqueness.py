"""enforce canonical tenant users and normalized unique email

Revision ID: canonical_tenant_user
Revises: org_member_nickname
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "canonical_tenant_user"
down_revision: Union[str, Sequence[str], None] = "org_member_nickname"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    duplicate_tenant_identity = connection.execute(
        sa.text(
            """
            SELECT tenant_id, identity_id, count(*)
            FROM users
            WHERE identity_id IS NOT NULL
            GROUP BY tenant_id, identity_id
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate_tenant_identity:
        raise RuntimeError(
            "duplicate (tenant_id, identity_id) users require canonical cleanup before migration"
        )

    duplicate_email = connection.execute(
        sa.text(
            """
            SELECT lower(btrim(email)), count(*)
            FROM identities
            WHERE email IS NOT NULL AND btrim(email) <> ''
            GROUP BY lower(btrim(email))
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate_email:
        raise RuntimeError(
            "case-insensitive duplicate identity emails require cleanup before migration"
        )

    duplicate_phone = connection.execute(
        sa.text(
            """
            SELECT regexp_replace(phone, '[[:space:]+-]', '', 'g'), count(*)
            FROM identities
            WHERE phone IS NOT NULL AND btrim(phone) <> ''
            GROUP BY regexp_replace(phone, '[[:space:]+-]', '', 'g')
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate_phone:
        raise RuntimeError(
            "normalized duplicate identity phones require cleanup before migration"
        )

    # Store the same canonical representation used by application matching and
    # the functional unique index.  The duplicate preflight above makes this
    # normalization deterministic and prevents a late unique-index failure.
    connection.execute(
        sa.text(
            """
            UPDATE identities
            SET email = nullif(lower(btrim(email)), '')
            WHERE email IS NOT NULL
              AND email IS DISTINCT FROM nullif(lower(btrim(email)), '')
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE identities
            SET phone = nullif(
                regexp_replace(phone, '[[:space:]+-]', '', 'g'), ''
            )
            WHERE phone IS NOT NULL
              AND phone IS DISTINCT FROM nullif(
                  regexp_replace(phone, '[[:space:]+-]', '', 'g'), ''
              )
            """
        )
    )

    connection.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_users_tenant_identity_not_null
            ON users (tenant_id, identity_id)
            WHERE identity_id IS NOT NULL
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_identities_email_lower_not_null
            ON identities (lower(btrim(email)))
            WHERE email IS NOT NULL AND btrim(email) <> ''
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_identities_phone_normalized_not_null
            ON identities (regexp_replace(phone, '[[:space:]+-]', '', 'g'))
            WHERE phone IS NOT NULL AND btrim(phone) <> ''
            """
        )
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_identities_phone_normalized_not_null")
    op.execute("DROP INDEX IF EXISTS uq_identities_email_lower_not_null")
    op.execute("DROP INDEX IF EXISTS uq_users_tenant_identity_not_null")
