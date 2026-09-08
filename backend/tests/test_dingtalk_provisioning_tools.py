import uuid
from types import SimpleNamespace

import pytest


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


@pytest.mark.asyncio
async def test_start_dingtalk_provisioning_tool_rejects_non_boolean_force_flag():
    from app.services import agent_tools

    result = await agent_tools._start_dingtalk_channel_provisioning_tool(
        uuid.uuid4(),
        uuid.uuid4(),
        {"force_reconfigure": "yes"},
    )

    assert "force_reconfigure" in result
    assert "布尔值" in result


@pytest.mark.asyncio
async def test_start_dingtalk_provisioning_tool_defaults_to_safe_reuse(monkeypatch):
    from app.core import permissions
    from app.services import agent_tools, dingtalk_provisioning

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id)
    captured = []

    class FakeResult:
        def scalar_one_or_none(self):
            return agent

    class FakeSession:
        async def execute(self, statement):
            return FakeResult()

        async def commit(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    async def fake_can_manage(db, uid, target_agent):
        return True

    async def fake_start(db, *, agent, requested_by_user_id, force_reconfigure):
        captured.append(force_reconfigure)
        return {
            "flow_action": "reused",
            "authorization_url": "https://auth.example/existing",
            "provisioning_id": str(uuid.uuid4()),
            "expires_at": "2026-07-16T08:00:00+00:00",
        }

    monkeypatch.setattr(agent_tools, "async_session", lambda: FakeSession())
    monkeypatch.setattr(permissions, "user_can_manage_agent_id", fake_can_manage)
    monkeypatch.setattr(dingtalk_provisioning, "start_dingtalk_channel_provisioning", fake_start)

    result = await agent_tools._start_dingtalk_channel_provisioning_tool(
        agent_id,
        user_id,
        {},
    )

    assert captured == [False]
    assert "复用原钉钉授权链接" in result
    assert "https://auth.example/existing" in result
    assert "2026-07-16T08:00:00+00:00 [UTC]" in result


@pytest.mark.asyncio
async def test_start_dingtalk_provisioning_tool_guides_configured_channel_without_new_link(monkeypatch):
    from app.core import permissions
    from app.services import agent_tools, dingtalk_provisioning

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id)

    class FakeResult:
        def scalar_one_or_none(self):
            return agent

    class FakeSession:
        async def execute(self, statement):
            return FakeResult()

        async def commit(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    async def fake_can_manage(db, uid, target_agent):
        return True

    async def fake_start(db, *, agent, requested_by_user_id, force_reconfigure):
        assert force_reconfigure is False
        return {
            "status": "already_configured",
            "flow_action": "already_configured",
            "authorization_url": None,
            "message": "当前数字员工的钉钉通道已经配置完成，无需重复配置。",
        }

    monkeypatch.setattr(agent_tools, "async_session", lambda: FakeSession())
    monkeypatch.setattr(permissions, "user_can_manage_agent_id", fake_can_manage)
    monkeypatch.setattr(dingtalk_provisioning, "start_dingtalk_channel_provisioning", fake_start)

    result = await agent_tools._start_dingtalk_channel_provisioning_tool(agent_id, user_id, {})

    assert "已经配置完成" in result
    assert "无需重复配置" in result
    assert "明确要求强制重配" in result
    assert "http" not in result
