"""One-off refresh of one tenant's current agent-tool assignments.

This script intentionally touches only seven named tools. It enables the six
approved assignments for every active agent in the target tenant and explicitly
disables the lightweight ``execute_code`` assignment. It never changes tool
metadata or the platform's default-allocation mechanism; the three admin tools
are attached to existing agents only by this script.

Run inside a backend container after deploying the matching code:

    python -m scripts.refresh_default_agent_tools --tenant <tenant-uuid> --dry-run
    python -m scripts.refresh_default_agent_tools --tenant <tenant-uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.tool import AgentTool, Tool


ENABLED_AGENT_TOOL_NAMES = (
    "execute_code_aio",
    "mcp_bailian_web_search",
    "yybpc-cli",
    "request_confirmation",
    "mcp_ragflow_retrieval",
    "read_image",
)
DISABLED_AGENT_TOOL_NAMES = ("execute_code",)
POLICY = {
    **{name: True for name in ENABLED_AGENT_TOOL_NAMES},
    **{name: False for name in DISABLED_AGENT_TOOL_NAMES},
}
ADMIN_TOOL_NAMES = {
    "mcp_bailian_web_search",
    "yybpc-cli",
    "mcp_ragflow_retrieval",
}


@dataclass(frozen=True)
class AssignmentChange:
    agent_id: uuid.UUID
    tool_id: uuid.UUID
    tool_name: str
    before: bool | None
    after: bool

    @property
    def operation(self) -> str:
        return "insert" if self.before is None else "update"


def plan_assignment_changes(
    agent_ids: list[uuid.UUID],
    tools_by_name: dict[str, Tool],
    assignments: dict[tuple[uuid.UUID, uuid.UUID], AgentTool],
) -> list[AssignmentChange]:
    """Return the minimal idempotent assignment changes for the policy."""
    changes: list[AssignmentChange] = []
    for agent_id in agent_ids:
        for tool_name, desired in POLICY.items():
            tool = tools_by_name[tool_name]
            assignment = assignments.get((agent_id, tool.id))
            current = assignment.enabled if assignment is not None else None
            if current == desired:
                continue
            changes.append(
                AssignmentChange(
                    agent_id=agent_id,
                    tool_id=tool.id,
                    tool_name=tool_name,
                    before=current,
                    after=desired,
                )
            )
    return changes


def _serializable_change(change: AssignmentChange) -> dict:
    value = asdict(change)
    value["agent_id"] = str(change.agent_id)
    value["tool_id"] = str(change.tool_id)
    value["operation"] = change.operation
    return value


async def run(tenant_id: uuid.UUID, *, dry_run: bool = False) -> dict:
    async with async_session() as db:
        candidate_tools = (
            await db.execute(select(Tool).where(Tool.name.in_(tuple(POLICY))))
        ).scalars().all()
        tools_by_name: dict[str, Tool] = {}
        for name in POLICY:
            expected_source = "admin" if name in ADMIN_TOOL_NAMES else "builtin"
            matching = [
                tool
                for tool in candidate_tools
                if tool.name == name
                and tool.source == expected_source
                and (
                    expected_source == "builtin"
                    or tool.tenant_id in (None, tenant_id)
                )
            ]
            if expected_source == "admin":
                tenant_scoped = [tool for tool in matching if tool.tenant_id == tenant_id]
                matching = tenant_scoped or [tool for tool in matching if tool.tenant_id is None]
            if not matching:
                raise RuntimeError(
                    f"Missing {expected_source} tool {name!r} visible to tenant {tenant_id}"
                )
            if len(matching) > 1:
                raise RuntimeError(
                    f"Ambiguous {expected_source} tool {name!r}: "
                    f"found {len(matching)} visible rows"
                )
            tool = matching[0]
            if not tool.enabled:
                raise RuntimeError(f"Tool {name!r} is globally disabled; enable it before refresh")
            tools_by_name[name] = tool

        agent_ids = [
            row[0]
            for row in (
                await db.execute(
                    select(Agent.id).where(
                        Agent.tenant_id == tenant_id,
                        Agent.is_deleted == False,  # noqa: E712
                    )
                )
            ).all()
        ]
        tool_ids = [tool.id for tool in tools_by_name.values()]
        assignment_rows = (
            await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id.in_(agent_ids),
                    AgentTool.tool_id.in_(tool_ids),
                )
            )
        ).scalars().all()
        assignments = {
            (assignment.agent_id, assignment.tool_id): assignment
            for assignment in assignment_rows
        }
        assignment_changes = plan_assignment_changes(agent_ids, tools_by_name, assignments)

        for change in assignment_changes:
            existing = assignments.get((change.agent_id, change.tool_id))
            if existing is None:
                db.add(
                    AgentTool(
                        agent_id=change.agent_id,
                        tool_id=change.tool_id,
                        enabled=change.after,
                    )
                )
            else:
                existing.enabled = change.after

        summary = {
            "tenant_id": str(tenant_id),
            "active_agents": len(agent_ids),
            "assignment_inserts": sum(c.operation == "insert" for c in assignment_changes),
            "assignment_updates": sum(c.operation == "update" for c in assignment_changes),
            "assignment_changes": [_serializable_change(c) for c in assignment_changes],
            "dry_run": dry_run,
        }

        if dry_run:
            await db.rollback()
        else:
            await db.commit()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True, type=uuid.UUID)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.tenant, dry_run=args.dry_run))
