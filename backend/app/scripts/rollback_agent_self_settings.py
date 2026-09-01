"""Remove update_self_settings before rolling back to an older backend image."""

from __future__ import annotations

import asyncio

from app.scripts.rollback_im_recall import remove_builtin_tool

UPDATE_SELF_SETTINGS_TOOL_NAME = "update_self_settings"


async def rollback_agent_self_settings() -> tuple[int, int]:
    return await remove_builtin_tool(UPDATE_SELF_SETTINGS_TOOL_NAME)


if __name__ == "__main__":
    assignment_count, tool_count = asyncio.run(rollback_agent_self_settings())
    print(f"removed_agent_tools={assignment_count} removed_tools={tool_count}")
