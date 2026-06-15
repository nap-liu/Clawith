"""mcp stdio transport: transport + command/args/env templates"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "202606151000_mcp_stdio_transport"
down_revision: Union[str, None] = "72382a243406"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("transport", sa.String(10), server_default="http", nullable=False))
    op.add_column("mcp_servers", sa.Column("command_template", sa.Text(), nullable=True))
    op.add_column("mcp_servers", sa.Column("args_template", postgresql.JSONB(), nullable=True))
    op.add_column("mcp_servers", sa.Column("env_template", postgresql.JSONB(), nullable=True))
    op.add_column("mcp_server_overrides", sa.Column("command_template", sa.Text(), nullable=True))
    op.add_column("mcp_server_overrides", sa.Column("args_template", postgresql.JSONB(), nullable=True))
    op.add_column("mcp_server_overrides", sa.Column("env_template", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    for t in ("mcp_server_overrides", "mcp_servers"):
        for c in ("env_template", "args_template", "command_template"):
            op.drop_column(t, c)
    op.drop_column("mcp_servers", "transport")
