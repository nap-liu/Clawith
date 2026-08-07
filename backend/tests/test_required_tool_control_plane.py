from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import tools as tools_api
from app.api.tools import (
    AgentToolConfigUpdate,
    AgentToolUpdate,
    BulkToolUpdateItem,
    ToolUpdate,
)
from app.services.tool_enablement import (
    resolved_agent_tool_enabled,
    tool_is_required,
)


class _Scalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _Result:
    def __init__(self, *, values=None, scalar=None):
        self._values = values or []
        self._scalar = scalar

    def scalars(self):
        return _Scalars(self._values)

    def scalar_one_or_none(self):
        return self._scalar


class _QueuedDB:
    def __init__(self, *results):
        self._results = list(results)
        self.committed = False
        self.added = []

    async def execute(self, _statement):
        return self._results.pop(0)

    async def commit(self):
        self.committed = True

    def add(self, value):
        self.added.append(value)


def _tool(name: str, *, enabled: bool = True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        display_name=name,
        description="",
        type="builtin",
        enabled=enabled,
        source="builtin",
        tenant_id=None,
        category="communication",
        icon="",
        parameters_schema={},
        mcp_server_url=None,
        mcp_server_name=None,
        mcp_server_id=None,
        mcp_tool_name=None,
        is_default=True,
        config_schema={},
        created_at=None,
    )


def _user(role: str, tenant_id=None, *, identity_platform_admin: bool = False):
    return SimpleNamespace(
        role=role,
        tenant_id=tenant_id,
        identity=SimpleNamespace(is_platform_admin=identity_platform_admin),
    )


def test_send_media_is_the_narrow_required_tool_exception():
    assert tool_is_required("send_media") is True
    assert tool_is_required("send_channel_file") is False
    assert resolved_agent_tool_enabled("send_media", None) is True
    assert resolved_agent_tool_enabled(
        "send_media", SimpleNamespace(enabled=False)
    ) is True
    assert tools_api._tool_availability("send_media") == {
        "availability": "required",
        "can_disable": False,
    }


@pytest.mark.asyncio
async def test_global_single_update_rejects_disabling_required_tool():
    media = _tool("send_media")
    db = _QueuedDB(_Result(scalar=media))

    with pytest.raises(HTTPException, match="cannot be disabled") as exc:
        await tools_api.update_tool(
            media.id,
            ToolUpdate(enabled=False),
            current_user=_user("platform_admin"),
            db=db,
        )

    assert exc.value.status_code == 409
    assert media.enabled is True
    assert db.committed is False


@pytest.mark.asyncio
async def test_global_bulk_update_is_atomic_when_required_disable_is_present():
    ordinary = _tool("ordinary")
    media = _tool("send_media")
    db = _QueuedDB(_Result(values=[ordinary, media]))

    updates = [
        BulkToolUpdateItem(tool_id=str(ordinary.id), enabled=False),
        BulkToolUpdateItem(tool_id=str(media.id), enabled=False),
    ]
    with pytest.raises(HTTPException, match="cannot be disabled"):
        await tools_api.update_tools_bulk(
            updates,
            current_user=_user("platform_admin"),
            db=db,
        )

    assert ordinary.enabled is True
    assert media.enabled is True
    assert db.committed is False


@pytest.mark.asyncio
async def test_member_cannot_bulk_update_global_tool_state():
    with pytest.raises(HTTPException, match="Platform admin required") as exc:
        await tools_api.update_tools_bulk(
            [],
            current_user=_user("member", uuid.uuid4()),
            db=_QueuedDB(),
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_org_admin_cannot_change_builtin_global_enabled_state():
    tenant_id = uuid.uuid4()
    tool = _tool("ordinary")
    db = _QueuedDB(_Result(scalar=tool))

    with pytest.raises(HTTPException, match="Platform admin required") as exc:
        await tools_api.update_tool(
            tool.id,
            ToolUpdate(enabled=False),
            current_user=_user("org_admin", tenant_id),
            db=db,
        )

    assert exc.value.status_code == 403
    assert tool.enabled is True
    assert db.committed is False


@pytest.mark.asyncio
async def test_org_admin_can_update_own_tenant_builtin_config(monkeypatch):
    tenant_id = uuid.uuid4()
    tool = _tool("send_media")
    tool.config_schema = {
        "fields": [{"key": "allow_download", "type": "boolean"}]
    }
    db = _QueuedDB(_Result(scalar=tool))
    saved = {}

    async def _save(_db, saved_tenant_id, name, config, schema):
        saved.update(
            tenant_id=saved_tenant_id,
            name=name,
            config=config,
            schema=schema,
        )

    monkeypatch.setattr(tools_api, "set_tenant_tool_config", _save)

    result = await tools_api.update_tool(
        tool.id,
        ToolUpdate(config={"allow_download": True}),
        current_user=_user("org_admin", tenant_id),
        db=db,
    )

    assert result == {"ok": True}
    assert saved["tenant_id"] == tenant_id
    assert saved["config"] == {"allow_download": True}
    assert db.committed is True


@pytest.mark.asyncio
async def test_member_cannot_select_another_tenant_in_global_tool_list():
    with pytest.raises(HTTPException, match="another tenant") as exc:
        await tools_api.list_tools(
            tenant_id=str(uuid.uuid4()),
            current_user=_user("member", uuid.uuid4()),
            db=_QueuedDB(),
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_global_tool_list_masks_company_secrets_for_member(monkeypatch):
    tenant_id = uuid.uuid4()
    tool = _tool("secret_tool")
    tool.config_schema = {
        "fields": [{"key": "api_key", "type": "password"}]
    }

    async def _company_config(_db, _tool, _tenant_id):
        return {"api_key": "global-secret"}

    monkeypatch.setattr(tools_api, "get_tool_company_config", _company_config)
    result = await tools_api.list_tools(
        current_user=_user("member", tenant_id),
        db=_QueuedDB(_Result(values=[tool])),
    )

    assert result[0]["config"]["api_key"] == "****cret"
    assert "global-secret" not in str(result)


@pytest.mark.asyncio
async def test_agent_update_rejects_required_disable_before_writes(monkeypatch):
    agent_id = uuid.uuid4()
    media = _tool("send_media")
    assignment = SimpleNamespace(
        agent_id=agent_id,
        tool_id=media.id,
        enabled=True,
    )
    db = _QueuedDB(_Result(scalar=media))

    async def _access(_db, _user, _agent_id):
        return SimpleNamespace(tenant_id=None), "manage"

    async def _assignments(_db, _agent_id):
        return {str(media.id): assignment}

    monkeypatch.setattr("app.core.permissions.check_agent_access", _access)
    monkeypatch.setattr(tools_api, "_load_agent_tool_assignments", _assignments)

    with pytest.raises(HTTPException, match="cannot be disabled"):
        await tools_api.update_agent_tools(
            agent_id,
            [AgentToolUpdate(tool_id=str(media.id), enabled=False)],
            current_user=SimpleNamespace(),
            db=db,
        )

    assert assignment.enabled is True
    assert db.committed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler,args",
    [
        (tools_api.get_agent_tools, (uuid.uuid4(),)),
        (tools_api.get_agent_tools_with_config, (uuid.uuid4(),)),
        (tools_api.get_agent_tool_config, (uuid.uuid4(), uuid.uuid4())),
        (
            tools_api.update_agent_tool_config,
            (uuid.uuid4(), uuid.uuid4(), AgentToolConfigUpdate(config={})),
        ),
    ],
)
async def test_agent_tool_surfaces_reject_users_without_agent_access(
    monkeypatch, handler, args
):
    async def _deny(_db, _user, _agent_id):
        raise HTTPException(status_code=403, detail="No access to this agent")

    monkeypatch.setattr("app.core.permissions.check_agent_access", _deny)

    with pytest.raises(HTTPException) as exc:
        await handler(*args, current_user=SimpleNamespace(), db=_QueuedDB())

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_agent_tool_config_write_requires_manage_access(monkeypatch):
    async def _use_only(_db, _user, _agent_id):
        return SimpleNamespace(tenant_id=uuid.uuid4()), "use"

    monkeypatch.setattr("app.core.permissions.check_agent_access", _use_only)

    with pytest.raises(HTTPException) as exc:
        await tools_api.update_agent_tool_config(
            uuid.uuid4(),
            uuid.uuid4(),
            AgentToolConfigUpdate(config={"allow_download": True}),
            current_user=SimpleNamespace(role="member"),
            db=_QueuedDB(),
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_agent_tool_config_rejects_tool_outside_agent_tenant(monkeypatch):
    agent_tenant = uuid.uuid4()
    foreign_tool = _tool("foreign")
    foreign_tool.source = "admin"
    foreign_tool.tenant_id = uuid.uuid4()
    foreign_tool.config_schema = {}

    async def _manage(_db, _user, _agent_id):
        return SimpleNamespace(tenant_id=agent_tenant), "manage"

    async def _assignments(_db, _agent_id):
        return {}

    monkeypatch.setattr("app.core.permissions.check_agent_access", _manage)
    monkeypatch.setattr(tools_api, "_load_agent_tool_assignments", _assignments)

    with pytest.raises(HTTPException) as exc:
        await tools_api.update_agent_tool_config(
            uuid.uuid4(),
            foreign_tool.id,
            AgentToolConfigUpdate(config={"allow_download": True}),
            current_user=SimpleNamespace(role="org_admin"),
            db=_QueuedDB(_Result(scalar=foreign_tool)),
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_read_only_agent_access_never_receives_unmasked_tool_secrets(
    monkeypatch,
):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    secret_tool = _tool("secret_tool")
    secret_tool.config_schema = {
        "fields": [{"key": "api_key", "type": "password"}]
    }
    assignment = SimpleNamespace(
        agent_id=agent_id,
        tool_id=secret_tool.id,
        enabled=True,
        config={"api_key": "agent-secret"},
    )

    async def _use_only(_db, _user, _agent_id):
        return SimpleNamespace(tenant_id=tenant_id), "use"

    async def _assignments(_db, _agent_id):
        return {str(secret_tool.id): assignment}

    async def _company_config(_db, _tool, _tenant_id):
        return {"api_key": "global-secret"}

    monkeypatch.setattr("app.core.permissions.check_agent_access", _use_only)
    monkeypatch.setattr(tools_api, "_load_agent_tool_assignments", _assignments)
    monkeypatch.setattr(tools_api, "get_tool_company_config", _company_config)

    result = await tools_api.get_agent_tool_config(
        agent_id,
        secret_tool.id,
        current_user=SimpleNamespace(),
        db=_QueuedDB(
            _Result(scalar=secret_tool),
            _Result(scalar=assignment),
        ),
    )

    serialized = str(result)
    assert "global-secret" not in serialized
    assert "agent-secret" not in serialized
    assert result["global_config"]["api_key"] == "****cret"
    assert result["agent_config"]["api_key"] == "****cret"
