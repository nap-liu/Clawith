"""Canonical user_id/agent_id ownership for OKR records.

Revision ID: canonical_okr_owners
Revises: canonical_chat_senders
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "canonical_okr_owners"
down_revision = "canonical_chat_senders"
branch_labels = None
depends_on = None


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    objective_columns = _columns(inspector, "okr_objectives")
    report_columns = _columns(inspector, "member_daily_reports")
    work_report_columns = _columns(inspector, "work_reports")

    if "owner_user_id" not in objective_columns:
        op.add_column("okr_objectives", sa.Column("owner_user_id", postgresql.UUID(), nullable=True))
        op.create_foreign_key(
            "fk_okr_objectives_owner_user_id",
            "okr_objectives",
            "users",
            ["owner_user_id"],
            ["id"],
            ondelete="CASCADE",
        )
    if "owner_agent_id" not in objective_columns:
        op.add_column("okr_objectives", sa.Column("owner_agent_id", postgresql.UUID(), nullable=True))
        op.create_foreign_key(
            "fk_okr_objectives_owner_agent_id",
            "okr_objectives",
            "agents",
            ["owner_agent_id"],
            ["id"],
            ondelete="CASCADE",
        )
    if "user_id" not in report_columns:
        op.add_column("member_daily_reports", sa.Column("user_id", postgresql.UUID(), nullable=True))
        op.create_foreign_key(
            "fk_member_daily_reports_user_id",
            "member_daily_reports",
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )
    if "agent_id" not in report_columns:
        op.add_column("member_daily_reports", sa.Column("agent_id", postgresql.UUID(), nullable=True))
        op.create_foreign_key(
            "fk_member_daily_reports_agent_id",
            "member_daily_reports",
            "agents",
            ["agent_id"],
            ["id"],
            ondelete="CASCADE",
        )
    if "user_id" not in work_report_columns:
        op.add_column("work_reports", sa.Column("user_id", postgresql.UUID(), nullable=True))
        op.create_foreign_key(
            "fk_work_reports_user_id", "work_reports", "users", ["user_id"], ["id"], ondelete="CASCADE"
        )
    if "agent_id" not in work_report_columns:
        op.add_column("work_reports", sa.Column("agent_id", postgresql.UUID(), nullable=True))
        op.create_foreign_key(
            "fk_work_reports_agent_id", "work_reports", "agents", ["agent_id"], ["id"], ondelete="CASCADE"
        )

    op.execute(
        """
        UPDATE okr_objectives AS objective
        SET owner_user_id = platform_user.id
        FROM users AS platform_user
        WHERE objective.owner_type = 'user'
          AND objective.owner_id = platform_user.id
          AND objective.tenant_id = platform_user.tenant_id
          AND objective.owner_user_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE channel_type_defaults
        SET system_prompt_block = replace(
            system_prompt_block,
            '→ Or use `open_id` directly if you already have it from `feishu_user_search`.',
            '→ Never use a provider ID; execute only with the exact canonical `user_id` returned by discovery.')
        WHERE channel_type = 'feishu'
          AND system_prompt_block IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE okr_objectives AS objective
        SET owner_user_id = member.user_id
        FROM org_members AS member
        WHERE objective.owner_type = 'user'
          AND objective.owner_id = member.id
          AND objective.tenant_id = member.tenant_id
          AND member.user_id IS NOT NULL
          AND objective.owner_user_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE okr_objectives AS objective
        SET owner_agent_id = agent.id
        FROM agents AS agent
        WHERE objective.owner_type = 'agent'
          AND objective.owner_id = agent.id
          AND objective.tenant_id = agent.tenant_id
          AND objective.owner_agent_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE member_daily_reports AS report
        SET user_id = platform_user.id
        FROM users AS platform_user
        WHERE report.member_type = 'user'
          AND report.member_id = platform_user.id
          AND report.tenant_id = platform_user.tenant_id
          AND report.user_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE work_reports AS report
        SET user_id = platform_user.id
        FROM users AS platform_user
        WHERE report.author_type = 'user'
          AND report.author_id = platform_user.id
          AND report.tenant_id = platform_user.tenant_id
          AND report.user_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE work_reports AS report
        SET agent_id = agent.id
        FROM agents AS agent
        WHERE report.author_type = 'agent'
          AND report.author_id = agent.id
          AND report.tenant_id = agent.tenant_id
          AND report.agent_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE member_daily_reports AS report
        SET user_id = member.user_id
        FROM org_members AS member
        WHERE report.member_type = 'user'
          AND report.member_id = member.id
          AND report.tenant_id = member.tenant_id
          AND member.user_id IS NOT NULL
          AND report.user_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE member_daily_reports AS report
        SET agent_id = agent.id
        FROM agents AS agent
        WHERE report.member_type = 'agent'
          AND report.member_id = agent.id
          AND report.tenant_id = agent.tenant_id
          AND report.agent_id IS NULL
        """
    )

    # Never guess an unresolved legacy owner.  Fail the forward migration with
    # a precise count so operators can repair it before retrying idempotently.
    op.execute(
        """
        DO $$
        DECLARE unresolved_objectives bigint;
        DECLARE unresolved_reports bigint;
        DECLARE unresolved_work_reports bigint;
        BEGIN
          SELECT count(*) INTO unresolved_objectives
          FROM okr_objectives
          WHERE (owner_type = 'user' AND owner_user_id IS NULL)
             OR (owner_type = 'agent' AND owner_agent_id IS NULL)
             OR (owner_type = 'company' AND owner_id IS NOT NULL);
          SELECT count(*) INTO unresolved_reports
          FROM member_daily_reports
          WHERE (user_id IS NULL) = (agent_id IS NULL);
          SELECT count(*) INTO unresolved_work_reports
          FROM work_reports
          WHERE (user_id IS NULL) = (agent_id IS NULL);
          IF unresolved_objectives > 0 OR unresolved_reports > 0 OR unresolved_work_reports > 0 THEN
            RAISE EXCEPTION 'canonical OKR identity migration_required: objectives=%, daily_reports=%, work_reports=%',
              unresolved_objectives, unresolved_reports, unresolved_work_reports;
          END IF;
        END $$;
        """
    )

    op.create_index("ix_okr_objectives_owner_user_id", "okr_objectives", ["owner_user_id"])
    op.create_index("ix_okr_objectives_owner_agent_id", "okr_objectives", ["owner_agent_id"])
    op.create_check_constraint(
        "ck_okr_objective_single_owner",
        "okr_objectives",
        "NOT (owner_user_id IS NOT NULL AND owner_agent_id IS NOT NULL)",
    )
    op.create_index("ix_member_daily_reports_user_id", "member_daily_reports", ["user_id"])
    op.create_index("ix_member_daily_reports_agent_id", "member_daily_reports", ["agent_id"])
    op.create_index(
        "uq_member_daily_report_user",
        "member_daily_reports",
        ["tenant_id", "user_id", "report_date"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_member_daily_report_agent",
        "member_daily_reports",
        ["tenant_id", "agent_id", "report_date"],
        unique=True,
        postgresql_where=sa.text("agent_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_member_daily_report_single_member",
        "member_daily_reports",
        "(user_id IS NOT NULL) <> (agent_id IS NOT NULL)",
    )
    op.create_index("ix_work_reports_user_id", "work_reports", ["user_id"])
    op.create_index("ix_work_reports_agent_id", "work_reports", ["agent_id"])
    op.create_check_constraint(
        "ck_work_report_single_author",
        "work_reports",
        "(user_id IS NOT NULL) <> (agent_id IS NOT NULL)",
    )

    # Existing installations already persisted the historical Feishu prompt.
    # Normalize its public identity examples without overwriting unrelated
    # operator customizations in the same block.
    op.execute(
        """
        UPDATE channel_type_defaults
        SET system_prompt_block = replace(
            replace(
                replace(
                    replace(
                        replace(
                            replace(
                                replace(system_prompt_block,
                                    'returns open_id, department',
                                    'returns canonical user_id, display name, department'),
                                '`member_names`(name list, auto-lookup)',
                                '`user_ids`(canonical ID list)'),
                            '`open_id` or `email`, `content`',
                            'canonical `user_id`, `message`'),
                        'Ask for user email or open_id when you can call `feishu_user_search` to look them up',
                        'Ask for or expose Feishu provider IDs; call `feishu_user_search` and use its exact canonical `user_id`'),
                    'send_feishu_message(member_name="John", message="...")` — it auto-searches.',
                    'send_feishu_message(user_id="<canonical UUID>", message="...")` after exact discovery.'),
                'Use `attendee_names=["John"]` in `feishu_calendar_create` — names are resolved automatically.',
                'Use `attendee_user_ids=["<canonical UUID>"]` in `feishu_calendar_create` after exact discovery.'),
            'Or use `attendee_open_ids=["ou_xxx"]` if you already have the open_id.',
            'Never pass a provider ID or display name to an execution tool.')
        WHERE channel_type = 'feishu'
          AND system_prompt_block IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_work_report_single_author", "work_reports", type_="check")
    op.drop_index("ix_work_reports_agent_id", table_name="work_reports")
    op.drop_index("ix_work_reports_user_id", table_name="work_reports")
    op.drop_constraint("fk_work_reports_agent_id", "work_reports", type_="foreignkey")
    op.drop_constraint("fk_work_reports_user_id", "work_reports", type_="foreignkey")
    op.drop_column("work_reports", "agent_id")
    op.drop_column("work_reports", "user_id")
    op.drop_constraint("ck_member_daily_report_single_member", "member_daily_reports", type_="check")
    op.drop_index("uq_member_daily_report_agent", table_name="member_daily_reports")
    op.drop_index("uq_member_daily_report_user", table_name="member_daily_reports")
    op.drop_index("ix_member_daily_reports_agent_id", table_name="member_daily_reports")
    op.drop_index("ix_member_daily_reports_user_id", table_name="member_daily_reports")
    op.drop_constraint("fk_member_daily_reports_agent_id", "member_daily_reports", type_="foreignkey")
    op.drop_constraint("fk_member_daily_reports_user_id", "member_daily_reports", type_="foreignkey")
    op.drop_column("member_daily_reports", "agent_id")
    op.drop_column("member_daily_reports", "user_id")
    op.drop_constraint("ck_okr_objective_single_owner", "okr_objectives", type_="check")
    op.drop_index("ix_okr_objectives_owner_agent_id", table_name="okr_objectives")
    op.drop_index("ix_okr_objectives_owner_user_id", table_name="okr_objectives")
    op.drop_constraint("fk_okr_objectives_owner_agent_id", "okr_objectives", type_="foreignkey")
    op.drop_constraint("fk_okr_objectives_owner_user_id", "okr_objectives", type_="foreignkey")
    op.drop_column("okr_objectives", "owner_agent_id")
    op.drop_column("okr_objectives", "owner_user_id")
