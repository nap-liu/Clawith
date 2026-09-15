"""Disable the exact new builtin before an older release owns tool dispatch."""

import argparse
import asyncio

from sqlalchemy import select

import app.models.registry  # noqa: F401
from app.database import async_session
from app.models.tool import Tool
from app.services.agent_login_contract import AGENT_LOGIN_SEED


async def tool_state(*, disable: bool = False) -> dict:
    async with async_session() as db:
        tool = await db.scalar(select(Tool).where(
            Tool.name == AGENT_LOGIN_SEED["name"], Tool.type == "builtin", Tool.source == "builtin",
        ).with_for_update())
        if tool is None:
            return {"present": False, "enabled": False}
        previous = tool.enabled
        if disable:
            tool.enabled = False
        result = {"present": True, "previous_enabled": previous, "enabled": tool.enabled}
        await db.commit()
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "disable"))
    args = parser.parse_args()
    print(asyncio.run(tool_state(disable=args.action == "disable")))
