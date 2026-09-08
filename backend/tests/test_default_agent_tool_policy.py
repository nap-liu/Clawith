"""Default-tool policy tests for new and existing agents."""

import uuid
from types import SimpleNamespace

from app.services.tool_config import merge_tool_config_layers
from app.services.tool_seeder import (
    BUILTIN_TOOLS,
    builtin_tool_enabled,
    should_sync_builtin_default,
)
from app.services.toolscall.capability import toolscall_enabled_for_agent
from scripts.refresh_default_agent_tools import POLICY, plan_assignment_changes


def _seed(name: str) -> dict:
    return next(tool for tool in BUILTIN_TOOLS if tool["name"] == name)


def test_toolscall_is_agent_scoped_and_defaults_on():
    assert toolscall_enabled_for_agent({}) is True
    assert toolscall_enabled_for_agent(None) is True
    assert toolscall_enabled_for_agent({"toolscall_enabled": True}) is True
    assert toolscall_enabled_for_agent({"toolscall_enabled": False}) is False


def test_agent_only_toolscall_flag_cannot_be_overridden_by_broader_config():
    schema = {
        "fields": [
            {"key": "toolscall_enabled", "type": "checkbox", "agent_only": True},
            {"key": "max_timeout", "type": "number"},
        ]
    }

    inherited = merge_tool_config_layers(
        {"toolscall_enabled": False, "max_timeout": 300},
        {"toolscall_enabled": False, "max_timeout": 120},
        {},
        schema,
    )
    assert "toolscall_enabled" not in inherited
    assert inherited["max_timeout"] == 120
    assert toolscall_enabled_for_agent(inherited) is True

    opted_out = merge_tool_config_layers(
        {"toolscall_enabled": True},
        {},
        {"toolscall_enabled": False},
        schema,
    )
    assert toolscall_enabled_for_agent(opted_out) is False


def test_agentbay_builtin_tools_are_globally_and_by_default_disabled():
    agentbay_tools = [tool for tool in BUILTIN_TOOLS if tool["category"] == "agentbay"]

    assert agentbay_tools
    assert all(builtin_tool_enabled(tool) is False for tool in agentbay_tools)
    assert all(should_sync_builtin_default(tool) is True for tool in agentbay_tools)


def test_non_agentbay_builtin_enabled_state_is_unchanged():
    assert builtin_tool_enabled(_seed("execute_code_aio")) is True


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
