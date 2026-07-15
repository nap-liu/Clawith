"""Default-tool policy tests for new and existing agents."""

import uuid
from types import SimpleNamespace

from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_SEED
from app.services.tool_seeder import BUILTIN_TOOLS, SYNC_IS_DEFAULT_TOOL_NAMES
from scripts.refresh_default_agent_tools import POLICY, plan_assignment_changes


def _seed(name: str) -> dict:
    return next(tool for tool in BUILTIN_TOOLS if tool["name"] == name)


def test_requested_builtin_default_flags_are_canonical():
    assert _seed("execute_code")["is_default"] is False
    assert _seed("execute_code_aio")["is_default"] is True
    assert _seed("read_image")["is_default"] is True
    assert REQUEST_CONFIRMATION_TOOL_SEED["is_default"] is True


def test_requested_builtin_flags_are_synced_to_existing_databases():
    assert {
        "execute_code",
        "execute_code_aio",
        "read_image",
        "request_confirmation",
    }.issubset(SYNC_IS_DEFAULT_TOOL_NAMES)


def test_refresh_plan_enables_six_tools_and_disables_lightweight_executor():
    agent_id = uuid.uuid4()
    tools = {
        name: SimpleNamespace(id=uuid.uuid4())
        for name in POLICY
    }
    assignments = {}

    changes = plan_assignment_changes([agent_id], tools, assignments)

    assert len(changes) == 7
    desired = {change.tool_name: change.after for change in changes}
    assert desired["execute_code"] is False
    assert all(desired[name] is True for name in POLICY if name != "execute_code")
    assert all(change.operation == "insert" for change in changes)


def test_refresh_plan_is_minimal_and_overrides_mismatched_rows():
    agent_id = uuid.uuid4()
    tools = {
        name: SimpleNamespace(id=uuid.uuid4())
        for name in POLICY
    }
    assignments = {
        (agent_id, tool.id): SimpleNamespace(enabled=desired)
        for name, desired in POLICY.items()
        if name != "mcp_bailian_web_search"
        for tool in [tools[name]]
    }
    assignments[(agent_id, tools["mcp_bailian_web_search"].id)] = SimpleNamespace(enabled=False)

    changes = plan_assignment_changes([agent_id], tools, assignments)

    assert [(change.tool_name, change.before, change.after) for change in changes] == [
        ("mcp_bailian_web_search", False, True)
    ]
