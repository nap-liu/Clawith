"""Add source to tools and backfill data

Revision ID: add_tool_source
Revises: add_sso_login_enabled
Create Date: 2026-03-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'add_tool_source'
down_revision: Union[str, None] = 'add_sso_login_enabled'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add source column
    op.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'builtin'")

    # 2. Backfill existing data
    # (a) Builtin tools (type='builtin') -> source='builtin'
    op.execute("UPDATE tools SET source = 'builtin' WHERE type = 'builtin'")
    
    # (b) Admin/Company tools (type='mcp' and tenant_id IS NOT NULL) -> source='admin'
    op.execute("UPDATE tools SET source = 'admin' WHERE type = 'mcp' AND tenant_id IS NOT NULL")
    
    # (c) Agent user-installed tools (type='mcp' and tenant_id IS NULL) -> source='agent'
    op.execute("UPDATE tools SET source = 'agent' WHERE type = 'mcp' AND tenant_id IS NULL")

    # 3. Fix existing agent_tools source column where they were incorrectly stored as 'system'
    # Any agent_tools record for an agent-installed MCP tool (no tenant_id) should be 'user_installed' instead of 'system'
    op.execute("""
        UPDATE agent_tools 
        SET source = 'user_installed' 
        WHERE source = 'system' 
          AND tool_id IN (
              SELECT id FROM tools WHERE type = 'mcp' AND tenant_id IS NULL
          )
    """)


def downgrade() -> None:
    op.drop_column("tools", "source")
