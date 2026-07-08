import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def _user(user_id=None, role="member"):
    return SimpleNamespace(id=user_id or uuid.uuid4(), role=role, tenant_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_start_dingtalk_provisioning_api_requires_manage_access(monkeypatch):
    from app.api import dingtalk_provisioning as api

    current_user = _user()
    agent_id = uuid.uuid4()

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id, creator_id=uuid.uuid4()), None

    monkeypatch.setattr(api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc:
        await api.start_dingtalk_channel_provisioning_route(
            agent_id=agent_id,
            data={},
            current_user=current_user,
            db=object(),
        )

    assert exc.value.status_code == 403
    assert "creator" in exc.value.detail.lower() or "管理员" in exc.value.detail


@pytest.mark.asyncio
async def test_start_dingtalk_provisioning_api_returns_sanitized_payload(monkeypatch):
    from app.api import dingtalk_provisioning as api

    current_user = _user()
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=current_user.id, tenant_id=current_user.tenant_id)

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, None

    async def fake_start(_db, *, agent, requested_by_user_id):
        return {
            "status": "waiting_for_authorization",
            "provisioning_id": str(uuid.uuid4()),
            "authorization_url": "https://oapi.dingtalk.com/auth",
            "expires_at": "2026-07-08T10:30:00+00:00",
            "message": "请打开授权链接，在钉钉中完成数字员工机器人授权。",
            "client_secret": "must-not-leak",
        }

    monkeypatch.setattr(api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(api, "start_dingtalk_channel_provisioning", fake_start)

    result = await api.start_dingtalk_channel_provisioning_route(
        agent_id=agent_id,
        data={},
        current_user=current_user,
        db=object(),
    )

    assert result["authorization_url"] == "https://oapi.dingtalk.com/auth"
    assert "数字员工" in result["message"]
    assert "Agent" not in result["message"]
    assert "client_secret" not in result
    assert "must-not-leak" not in str(result)
