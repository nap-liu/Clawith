"""Separate explicit login suspension from provider-derived user activity."""

from alembic import op
import sqlalchemy as sa


revision = "user_login_suspension"
down_revision = "trigger_execution_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_login_suspended",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Fail closed for ambiguous historical rows.  The only safely recoverable
    # shape is a linked account whose every provider source is already
    # inactive/deleted: that is the state produced by directory aggregation.
    # No-source rows and inactive Users that still have an active source retain
    # the legacy administrator block.
    op.execute(
        sa.text(
            """
            UPDATE users AS u
               SET is_login_suspended = true
             WHERE u.is_active = false
               AND (
                   NOT EXISTS (
                       SELECT 1
                         FROM org_members AS linked
                        WHERE linked.user_id = u.id
                          AND linked.tenant_id = u.tenant_id
                   )
                   OR EXISTS (
                   SELECT 1
                     FROM org_members AS om
                     JOIN identity_providers AS ip ON ip.id = om.provider_id
                    WHERE om.user_id = u.id
                      AND om.tenant_id = u.tenant_id
                      AND om.status = 'active'
                      AND ip.is_active = true
                      AND (ip.tenant_id = u.tenant_id OR ip.tenant_id IS NULL)
                   )
               )
            """
        )
    )


def downgrade() -> None:
    op.drop_column("users", "is_login_suspended")
