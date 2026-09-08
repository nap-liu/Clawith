"""Sync or roll back only the scene-management tool's new runtime fields."""

import argparse
import asyncio
import copy
import json

from sqlalchemy import select, text

from app.database import async_session
from app.models.tool import Tool
from app.services.tool_seeder import BUILTIN_TOOLS

RUNTIME_FIELDS = frozenset({"include_soul", "include_memory", "tools", "mcp_server_overrides"})


async def synchronize(action: str) -> dict:
    if action not in {"status", "apply", "rollback"}:
        raise ValueError("Unknown scene schema action")
    async with async_session() as db:
        await db.execute(text("SET LOCAL lock_timeout = '2s'"))
        await db.execute(text("SET LOCAL statement_timeout = '30s'"))
        query = select(Tool).where(Tool.name == "manage_scene", Tool.type == "builtin")
        if action != "status":
            query = query.with_for_update()
        tool = (await db.execute(query)).scalar_one()
        schema = copy.deepcopy(tool.parameters_schema or {})
        properties = schema.setdefault("properties", {})
        before = sorted(RUNTIME_FIELDS.intersection(properties))
        if action == "apply":
            seed = next(item for item in BUILTIN_TOOLS if item["name"] == "manage_scene")
            for field in RUNTIME_FIELDS:
                properties[field] = copy.deepcopy(seed["parameters_schema"]["properties"][field])
        elif action == "rollback":
            for field in RUNTIME_FIELDS:
                properties.pop(field, None)
            if "required" in schema:
                schema["required"] = [field for field in schema["required"] if field not in RUNTIME_FIELDS]
        changed = schema != tool.parameters_schema
        if action != "status" and changed:
            tool.parameters_schema = schema
            await db.commit()
        return {"action": action, "changed": changed, "before": before,
                "after": sorted(RUNTIME_FIELDS.intersection(properties))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "apply", "rollback"))
    print(json.dumps(asyncio.run(synchronize(parser.parse_args().action))))


if __name__ == "__main__":
    main()
