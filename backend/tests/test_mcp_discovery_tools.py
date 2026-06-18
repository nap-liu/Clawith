from __future__ import annotations
import uuid
import pytest
from types import SimpleNamespace
from app.database import async_session, engine
from app.models.mcp_server import MCPServer  # noqa: F401 — registers mcp_servers table in metadata (FK needed by Tool)
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose(); yield; await engine.dispose()


def _ctx(token):
    h = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=h)))


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t); await db.commit(); await db.refresh(t); return t


async def _seed_user(tenant_id=None) -> User:
    async with async_session() as db:
        s = uuid.uuid4().hex[:12]
        ident = Identity(username=f"u_{s}", email=f"{s}@t.local", password_hash="x")
        db.add(ident); await db.flush()
        u = User(identity_id=ident.id, display_name="U", role="member", is_active=True, tenant_id=tenant_id)
        db.add(u); await db.commit(); await db.refresh(u); return u


async def _pat(user, scope="read"):
    from app.services.pat_service import issue_pat
    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="t", scope=scope)
    return token


async def test_list_available_tools_unauth():
    from app.mcp_server.tools_discovery import list_available_tools_impl
    out = await list_available_tools_impl(_ctx("clw_bogus"))
    assert "未鉴权" in out


async def test_list_available_tools_lists_builtin():
    from app.models.tool import Tool
    from app.mcp_server.tools_discovery import list_available_tools_impl
    tenant = await _seed_tenant(); user = await _seed_user(tenant_id=tenant.id)
    tool_name = f"test_tool_{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        db.add(Tool(name=tool_name, display_name="Test Tool", description="", category="custom",
                    source="builtin", enabled=True, is_default=True))
        await db.commit()
    token = await _pat(user)
    out = await list_available_tools_impl(_ctx(token))
    assert tool_name in out


async def test_list_models_lists_enabled():
    from app.models.llm import LLMModel
    from app.mcp_server.tools_discovery import list_models_impl
    tenant = await _seed_tenant(); user = await _seed_user(tenant_id=tenant.id)
    async with async_session() as db:
        db.add(LLMModel(tenant_id=tenant.id, provider="dashscope", model="qwen3.5-plus",
                        label="QwenX", api_key_encrypted="x", enabled=True, context_window=262144))
        await db.commit()
    token = await _pat(user)
    out = await list_models_impl(_ctx(token))
    assert "QwenX" in out
