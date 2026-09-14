"""Effective Agent management grants authorize human conversation audit."""

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from app.api.chat_sessions import (
    PatchSessionIn,
    delete_session,
    get_session_messages,
    list_sessions,
    rename_session,
)
from app.database import async_session
from app.models.agent import AgentPermission
from app.models.chat_session import ChatSession
from app.services.tools.session_introspection.handlers import (
    handle_list_sessions,
    handle_read_session_messages,
    handle_search_sessions,
)
from session_introspection_support import (
    _isolate_async_engine_between_tests,  # noqa: F401
    _seed_agent,
    _seed_message,
    _seed_session,
    _seed_tenant,
    _seed_user,
)


async def test_member_manage_audit_and_revocation_preserve_session_boundaries():
    tenant = await _seed_tenant()
    creator = await _seed_user(tenant_id=tenant.id)
    manager = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(creator.id, tenant_id=tenant.id)
    origin = await _seed_session(agent.id, manager.id, channel="dingtalk")
    target = await _seed_session(agent.id, creator.id, channel="miniprogram")
    await _seed_message(agent.id, creator.id, target.id, "user", "audit-visible-message")
    async with async_session() as db:
        grant = AgentPermission(
            agent_id=agent.id, scope_type="user", scope_id=manager.id, access_level="manage",
        )
        db.add(grant)
        await db.commit()
        page = await list_sessions(agent.id, scope="all", current_user=manager, db=db)
        assert str(target.id) in {item.id for item in page}
        messages = await get_session_messages(
            agent.id, session_id=target.id, limit=20, before=None, current_user=manager, db=db,
        )
        assert [message["content"] for message in messages] == ["audit-visible-message"]

    context = (agent.id, manager.id, str(origin.id))
    raw = json.loads(await handle_list_sessions(*context, {"raw": True}))
    assert str(target.id) in {item["id"] for item in raw["items"]}
    assert "audit-visible-message" in await handle_read_session_messages(*context, {"session_id": str(target.id)})
    assert "audit-visible-message" in await handle_search_sessions(*context, {"query": "audit-visible-message"})

    # Reading others' history does not newly authorize destructive operations.
    for operation in ("rename", "delete"):
        async with async_session() as db:
            with pytest.raises(HTTPException) as exc:
                if operation == "rename":
                    await rename_session(agent.id, target.id, PatchSessionIn(title="changed"), manager, db)
                else:
                    await delete_session(agent.id, target.id, manager, db)
            assert exc.value.status_code == 403
            assert await db.scalar(select(ChatSession.title).where(ChatSession.id == target.id)) == target.title

    async with async_session() as db:
        await db.execute(update(AgentPermission).where(AgentPermission.id == grant.id).values(access_level="use"))
        await db.commit()
    raw = json.loads(await handle_list_sessions(*context, {"raw": True}))
    assert str(target.id) not in {item["id"] for item in raw["items"]}
    assert "无法访问" in await handle_read_session_messages(*context, {"session_id": str(target.id)})
    assert "audit-visible-message" not in await handle_search_sessions(*context, {"query": "audit-visible"})
    async with async_session() as db:
        with pytest.raises(HTTPException) as exc:
            await list_sessions(agent.id, scope="all", current_user=manager, db=db)
        assert exc.value.status_code == 403
