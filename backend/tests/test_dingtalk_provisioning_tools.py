import uuid

import pytest


PROVISIONING_TOOL_NAMES = {
    "start_dingtalk_channel_provisioning",
    "get_dingtalk_channel_provisioning_status",
}


def _seed_tool(name: str) -> dict:
    from app.services.tool_seeder import BUILTIN_TOOLS

    for tool in BUILTIN_TOOLS:
        if tool.get("name") == name:
            return tool
    raise AssertionError(f"{name} missing from BUILTIN_TOOLS")


def _fallback_tool(name: str) -> dict:
    from app.services import agent_tools

    for tool in agent_tools.AGENT_TOOLS:
        function = tool.get("function") or {}
        if function.get("name") == name:
            return function
    raise AssertionError(f"{name} missing from AGENT_TOOLS fallback")


def test_dingtalk_provisioning_tools_are_seeded_for_digital_employee_copy():
    for name in PROVISIONING_TOOL_NAMES:
        tool = _seed_tool(name)
        assert tool["category"] == "communication"
        assert tool["is_default"] is True
        assert len(tool["name"]) <= 100
        assert len(tool["icon"]) <= 10
        text = f"{tool['display_name']} {tool['description']}"
        assert "数字员工" in text
        assert "Agent" not in text
        assert "client_secret" not in text
        assert tool["parameters_schema"]["type"] == "object"


def test_dingtalk_provisioning_tools_exist_in_fallback_definitions():
    for name in PROVISIONING_TOOL_NAMES:
        function = _fallback_tool(name)
        text = f"{function['name']} {function['description']}"
        assert "数字员工" in text
        assert "Agent" not in text
        assert function["parameters"]["type"] == "object"


@pytest.mark.asyncio
async def test_execute_tool_routes_dingtalk_provisioning(monkeypatch):
    from app.services import agent_tools

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    calls = []

    async def fake_start(aid, uid, arguments):
        calls.append(("start", aid, uid, arguments))
        return "start-ok"

    async def fake_status(aid, uid, arguments):
        calls.append(("status", aid, uid, arguments))
        return "status-ok"

    async def fake_log_activity(*_args, **_kwargs):
        return None

    async def fake_get_agent_tenant_id(_agent_id):
        return None

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", fake_get_agent_tenant_id)
    monkeypatch.setattr(agent_tools, "_start_dingtalk_channel_provisioning_tool", fake_start, raising=False)
    monkeypatch.setattr(agent_tools, "_get_dingtalk_channel_provisioning_status_tool", fake_status, raising=False)
    monkeypatch.setattr("app.services.activity_logger.log_activity", fake_log_activity)

    start = await agent_tools.execute_tool(
        "start_dingtalk_channel_provisioning",
        {"replace_existing": True},
        agent_id,
        user_id,
    )
    status = await agent_tools.execute_tool(
        "get_dingtalk_channel_provisioning_status",
        {"provisioning_id": "abc"},
        agent_id,
        user_id,
    )

    assert start == "start-ok"
    assert status == "status-ok"
    assert calls == [
        ("start", agent_id, user_id, {"replace_existing": True}),
        ("status", agent_id, user_id, {"provisioning_id": "abc"}),
    ]


@pytest.mark.asyncio
async def test_start_dingtalk_provisioning_tool_requires_user_context():
    from app.services import agent_tools

    result = await agent_tools._start_dingtalk_channel_provisioning_tool(uuid.uuid4(), None, {})

    assert "数字员工" in result
    assert "登录用户" in result
    assert "Agent" not in result
