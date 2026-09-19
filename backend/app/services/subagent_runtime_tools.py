"""Subagent cancellation follow-up and tool preparation helpers."""

from __future__ import annotations

from app.services.subagent_runtime_shared import *  # noqa: F401,F403
from app.services.turn_tool_settings import current_tool_settings, effective_assignment

async def cancel_local_subagent_tasks(
    run_ids: list[uuid.UUID],
    *,
    expected_lease_owners: dict[uuid.UUID, str | None] | None = None,
) -> None:
    """Interrupt local workers after their durable runs were revoked.

    Cross-instance workers observe the cancelled status on their next lease,
    round or tool boundary. This local fast path closes the same-instance gap.
    """

    if not run_ids:
        return
    async with _running_tasks_guard:
        tasks = [
            _running_tasks.get(run_id)
            for run_id in run_ids
            if _running_tasks.get(run_id) is not None
            and (
                expected_lease_owners is None
                or _running_task_lease_owners.get(run_id)
                == expected_lease_owners.get(run_id)
            )
        ]
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()


async def publish_cancelled_subagent_turns(
    run_ids: list[uuid.UUID],
    *,
    project_id: uuid.UUID | None = None,
) -> None:
    """Publish committed child and Project-cohort cancellation state."""

    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
        publish_conversation_turn_event,
    )

    for run_id in dict.fromkeys(run_ids):
        async with async_session() as db:
            child = await db.get(ChatSession, run_id)
            if child is None:
                continue
            snapshot = await get_conversation_turn_snapshot(
                db,
                agent_id=child.agent_id,
                conversation_id=str(child.id),
            )
            if snapshot.status != "cancelled" or snapshot.anchor_id is None:
                continue
        await publish_conversation_turn_event(
            agent_id=child.agent_id,
            conversation_id=str(child.id),
            payload={"type": "done", "role": "assistant", "content": ""},
            snapshot=snapshot,
            event_kind="turn_terminal",
        )

    if project_id is not None:
        from app.services.project_group_turn_lifecycle import (
            reconcile_and_publish_project_group_turns,
        )

        await reconcile_and_publish_project_group_turns(project_id)


async def finalize_cancelled_project_member_turns(
    project_id: uuid.UUID,
    run_ids: list[uuid.UUID],
) -> None:
    """Post-commit observer publication and local worker interruption."""

    await publish_cancelled_subagent_turns(run_ids, project_id=project_id)
    await cancel_local_subagent_tasks(run_ids)


async def prepare_subagent_tools(
    agent_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    execution_user_id: uuid.UUID | None = None,
) -> list[dict]:
    """Return the Agent's normal tools with the child-only protocol surface."""
    from app.models.tool import AgentTool, Tool
    from app.services.agent_tools import get_agent_tools_for_llm
    from app.services.tool_enablement import resolved_agent_tool_enabled

    hidden = {
        "run_subagent",
        "get_subagent_status",
        "send_message_to_subagent",
        "stop_subagent",
        "send_message_to_parent",
        "request_confirmation",
    }
    child_tools: list[dict] = []
    project_tools: list[dict] = []
    is_project_runtime = False
    async with async_session() as db:
        child = await db.get(ChatSession, session_id) if session_id else None
        if child is not None and child.source_channel == SUBAGENT_CHANNEL and child.project_id is not None:
            is_project_runtime = True
            if execution_user_id is None:
                raise ValueError("Project Subagent tool preparation requires its execution user identity")
            from app.services.project_runtime_tools import (
                add_project_workspace_parameter,
                load_project_runtime_scope,
                project_runtime_tool_schemas,
            )

            project, member, scoped_child, _run = await load_project_runtime_scope(
                db,
                session_id=child.id,
                agent_id=agent_id,
                execution_user_id=execution_user_id,
            )
            runtime_config = dict(scoped_child.im_config or {})
            scene_scope = current_tool_settings(agent_id)
            if scene_scope is not None:
                tools = await get_agent_tools_for_llm(agent_id)
            elif runtime_config.get("project_run_frozen"):
                assignment_snapshot = [
                    item
                    for item in runtime_config.get("capability_snapshot", [])
                    if isinstance(item, dict) and item.get("type") == "agent_tool" and item.get("tool_id")
                ]
                tools = await get_agent_tools_for_llm(
                    agent_id,
                    assignment_snapshot=assignment_snapshot,
                )
            else:
                tools = await get_agent_tools_for_llm(agent_id)
            child_tools = [tool for tool in tools if tool.get("function", {}).get("name") not in hidden]
            # Builtins remain governed by the normal Agent tool policy. MCP is
            # deny-by-default and re-enabled only by the immutable project
            # snapshot captured when this validated child was created.
            from app.models.tool import Tool

            all_mcp_names = set((await db.execute(select(Tool.name).where(Tool.type == "mcp"))).scalars())
            allowed_server_ids = {
                uuid.UUID(str(entry["capability_id"]))
                for entry in runtime_config.get("capability_snapshot", [])
                if isinstance(entry, dict) and entry.get("type") == "mcp" and entry.get("capability_id")
            }
            allowed_mcp_names = set()
            if allowed_server_ids:
                allowed_mcp_names = set(
                    (
                        await db.execute(
                            select(Tool.name).where(
                                Tool.type == "mcp",
                                Tool.mcp_server_id.in_(allowed_server_ids),
                            )
                        )
                    ).scalars()
                )
            allowed_mcp_names.update(
                str(entry.get("name") or "")
                for entry in runtime_config.get("capability_snapshot", [])
                if isinstance(entry, dict) and entry.get("type") == "agent_tool"
            )
            allowed_mcp_names.intersection_update(all_mcp_names)
            child_tools = [
                item
                for item in child_tools
                if item.get("function", {}).get("name") not in all_mcp_names
                or item.get("function", {}).get("name") in allowed_mcp_names
            ]
            # Project collaboration has one durable, scoped path. Generic
            # Agent/session messaging bypasses the project member snapshot,
            # role contract, A2A causality, and project audit trail; exposing
            # it here caused cross-session delivery failures and mechanical
            # status notifications instead of professional collaboration.
            project_collaboration_bypass_tools = {
                "send_message_to_agent",
                "send_file_to_agent",
                "send_message_to_parent",
                "send_session_message",
            }
            child_tools = [
                item
                for item in child_tools
                if item.get("function", {}).get("name") not in project_collaboration_bypass_tools
            ]
            child_tools = add_project_workspace_parameter(child_tools)
            if (
                runtime_config.get("project_execution_tools_enabled") is False
                or project.status in {"planning", "paused", "waiting", "completed"}
            ):
                # Conversation remains available outside active execution, but
                # no inherited or project mutation surface is exposed.
                child_tools = []
                project_tools = []
            else:
                if runtime_config.get("project_run_frozen"):
                    from types import SimpleNamespace

                    runtime_member = SimpleNamespace(
                        is_enabled=True,
                        is_leader=runtime_config.get("project_role_snapshot") == "leader",
                        config_snapshot=dict(runtime_config.get("member_config_snapshot") or {}),
                    )
                else:
                    runtime_member = member
                project_tools = project_runtime_tool_schemas(
                    project,
                    runtime_member,
                )
        else:
            tools = await get_agent_tools_for_llm(agent_id)
            child_tools = [tool for tool in tools if tool.get("function", {}).get("name") not in hidden]
        row = (
            await db.execute(
                select(Tool, AgentTool)
                .outerjoin(
                    AgentTool,
                    (AgentTool.tool_id == Tool.id) & (AgentTool.agent_id == agent_id),
                )
                .where(Tool.name == "send_message_to_parent")
                .limit(1)
            )
        ).first()
    if project_tools:
        projected_names = {item["function"]["name"] for item in project_tools}
        child_tools = [item for item in child_tools if item.get("function", {}).get("name") not in projected_names]
    if is_project_runtime:
        return [*child_tools, *project_tools]
    parent_tool = row[0] if row else None
    if parent_tool is None:
        raise RuntimeError("send_message_to_parent builtin tool is not seeded")
    assignment = effective_assignment(agent_id, parent_tool, row[1])
    if not parent_tool.enabled or not resolved_agent_tool_enabled(
        parent_tool.name,
        assignment,
    ):
        return [*child_tools, *project_tools]
    child_tools.append(
        {
            "type": "function",
            "function": {
                "name": parent_tool.name,
                "description": parent_tool.description,
                "parameters": parent_tool.parameters_schema,
            },
        }
    )
    child_tools.extend(project_tools)
    return child_tools
