"""Remove the recall builtin before rolling back to a binary that predates it.

Run this module with the current backend image, then switch backend/frontend
digests.  The operation is idempotent and removes explicit AgentTool bindings
through the same transaction as the builtin Tool row.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, select

from app.database import async_session
from app.models.tool import AgentTool, Tool

RECALL_TOOL_NAME = "recall_message"


async def remove_builtin_tool(tool_name: str) -> tuple[int, int]:
    """Delete one exact builtin tool and its assignments transactionally."""
    async with async_session() as db:
        tool = (
            await db.execute(
                select(Tool)
                .where(
                    Tool.name == tool_name,
                    Tool.type == "builtin",
                    Tool.source == "builtin",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if tool is None:
            return 0, 0
        assignments = await db.execute(delete(AgentTool).where(AgentTool.tool_id == tool.id))
        tools = await db.execute(delete(Tool).where(Tool.id == tool.id))
        await db.commit()
        return int(assignments.rowcount or 0), int(tools.rowcount or 0)


async def rollback_im_recall() -> tuple[int, int]:
    return await remove_builtin_tool(RECALL_TOOL_NAME)


if __name__ == "__main__":
    assignment_count, tool_count = asyncio.run(rollback_im_recall())
    print(f"removed_agent_tools={assignment_count} removed_tools={tool_count}")
