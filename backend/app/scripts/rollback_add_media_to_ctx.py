"""Remove add_media_to_ctx before rolling back to an older backend image."""

from __future__ import annotations

import asyncio

from app.scripts.rollback_im_recall import remove_builtin_tool

ADD_MEDIA_TO_CTX_TOOL_NAME = "add_media_to_ctx"


async def rollback_add_media_to_ctx() -> tuple[int, int]:
    return await remove_builtin_tool(ADD_MEDIA_TO_CTX_TOOL_NAME)


if __name__ == "__main__":
    assignment_count, tool_count = asyncio.run(rollback_add_media_to_ctx())
    print(f"removed_agent_tools={assignment_count} removed_tools={tool_count}")
