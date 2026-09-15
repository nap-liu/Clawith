"""Configure multimodal tool-result projection independently of model vision."""

from alembic import op
import sqlalchemy as sa

revision = "tool_result_multimodal"
down_revision = "additive_agent_permissions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("llm_models", sa.Column(
        "tool_result_multimodal_mode", sa.String(16), nullable=False, server_default="auto",
    ))
    op.create_check_constraint(
        "ck_llm_models_tool_result_multimodal_mode", "llm_models",
        "tool_result_multimodal_mode IN ('auto','native','user_message')",
    )


def downgrade():
    op.drop_constraint("ck_llm_models_tool_result_multimodal_mode", "llm_models", type_="check")
    op.drop_column("llm_models", "tool_result_multimodal_mode")
