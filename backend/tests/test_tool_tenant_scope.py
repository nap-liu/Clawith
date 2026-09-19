import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.tools import _tool_record_visible_to_agent
from app.api.tools_agent import list_agent_installed_tools
from app.core.security import get_current_user
from app.database import get_db
from app.main import app


def make_tool(**overrides):
    values = {
        "id": uuid.uuid4(),
        "source": "builtin",
        "tenant_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_builtin_tools_are_visible_across_tenants():
    tenant_id = uuid.uuid4()
    tool = make_tool(source="builtin", tenant_id=None)

    assert _tool_record_visible_to_agent(tool, tenant_id, {}) is True


def test_admin_tools_are_visible_only_to_same_tenant():
    tenant_id = uuid.uuid4()
    foreign_tenant_id = uuid.uuid4()
    same_tenant_tool = make_tool(source="admin", tenant_id=tenant_id)
    foreign_tool = make_tool(source="admin", tenant_id=foreign_tenant_id)

    assert _tool_record_visible_to_agent(same_tenant_tool, tenant_id, {}) is True
    assert _tool_record_visible_to_agent(foreign_tool, tenant_id, {}) is False


def test_agent_installed_tools_require_explicit_assignment():
    tenant_id = uuid.uuid4()
    tool_id = uuid.uuid4()
    installed_tool = make_tool(source="agent", id=tool_id, tenant_id=tenant_id)

    assert _tool_record_visible_to_agent(installed_tool, tenant_id, {}) is False
    assert _tool_record_visible_to_agent(installed_tool, tenant_id, {str(tool_id): object()}) is True

    foreign_tool = make_tool(source="agent", id=tool_id, tenant_id=uuid.uuid4())
    assert _tool_record_visible_to_agent(foreign_tool, tenant_id, {str(tool_id): object()}) is False


class RejectingDb:
    async def execute(self, *_args, **_kwargs):
        raise AssertionError("unauthorized requests must be rejected before querying")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "requested_tenant"),
    [
        (SimpleNamespace(role="member", tenant_id=uuid.uuid4(), identity=None), None),
        (SimpleNamespace(role="org_admin", tenant_id=uuid.uuid4(), identity=None), str(uuid.uuid4())),
    ],
)
async def test_agent_installed_tools_reject_unauthorized_tenant_access(user, requested_tenant):
    with pytest.raises(HTTPException) as exc:
        await list_agent_installed_tools(
            tenant_id=requested_tenant,
            current_user=user,
            db=RejectingDb(),
        )

    assert exc.value.status_code == 403


def test_agent_installed_tools_http_rejects_ordinary_member_before_querying():
    user = SimpleNamespace(role="member", tenant_id=uuid.uuid4(), identity=None)

    async def override_db():
        yield RejectingDb()

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = override_db
    try:
        response = TestClient(app).get("/api/tools/agent-installed")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
