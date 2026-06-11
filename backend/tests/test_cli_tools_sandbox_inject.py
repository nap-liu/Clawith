"""Pure-function tests for the sandbox CLI injection block builder."""
import pytest

from app.services.cli_tools.sandbox_inject import (
    build_wrapper_write_sh,
    build_env_exports_sh,
    build_python_prelude,
    render_env,
    shell_quote,
)
from app.services.cli_tools.placeholders import PlaceholderContext


def test_shell_quote_wraps_and_escapes():
    assert shell_quote("abc") == "'abc'"
    assert shell_quote("a'b") == "'a'\\''b'"


def test_build_wrapper_write_sh_contains_expected_parts():
    text = build_wrapper_write_sh(
        name="svc",
        binary_path="/data/cli_binaries/_global/t1/aa.bin",
    )
    # Must write wrapper to $HOME/.local/bin/svc
    assert 'mkdir -p "$HOME/.local/bin"' in text
    assert "base64 -d" in text
    assert "chmod 755" in text
    # The wrapper content must be base64'd — verify by decoding
    import base64
    import re
    m = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", text)
    assert m, f"no base64 block found in: {text}"
    decoded = base64.b64decode(m.group(1)).decode()
    assert "#!/bin/sh" in decoded
    assert "exec '/data/cli_binaries/_global/t1/aa.bin' \"$@\"" in decoded


def test_build_wrapper_write_sh_rejects_unsafe_name():
    with pytest.raises(ValueError):
        build_wrapper_write_sh(name="bad name; rm", binary_path="/x")


def test_build_wrapper_write_sh_rejects_trailing_newline_name():
    with pytest.raises(ValueError):
        build_wrapper_write_sh(name="svc\n", binary_path="/x")


def test_build_env_exports_sh_basic():
    result = build_env_exports_sh({"YYBPC_CLI_USER_PHONE": "13800000000", "YYBPC_CLI_HOME": "/data/cli_state/x"})
    assert result.startswith("export ")
    assert "YYBPC_CLI_USER_PHONE='13800000000'" in result
    assert "YYBPC_CLI_HOME='/data/cli_state/x'" in result


def test_build_env_exports_sh_empty_returns_empty_string():
    assert build_env_exports_sh({}) == ""


def test_build_env_exports_sh_rejects_unsafe_key():
    with pytest.raises(ValueError):
        build_env_exports_sh({"A; touch /tmp/P; B": "v"})


def test_build_python_prelude_contains_wrapper_and_env():
    wrappers = [{"name": "svc", "binary_path": "/data/cli_binaries/x.bin"}]
    env = {"MY_KEY": "my_val"}
    prelude = build_python_prelude(wrappers, env)
    assert "import os as _os" in prelude
    assert "expanduser" in prelude
    assert ".local/bin" in prelude
    assert "svc" in prelude
    assert "my_val" in prelude


def test_render_env_resolves_placeholders_and_skips_userless_identity():
    ctx_with_user = PlaceholderContext(
        user={"id": "u1", "phone": "138", "email": ""},
        state={"dir": "/data/cli_state/t/tool/u1"},
    )
    env = {"YYBPC_CLI_USER_PHONE": "$user.phone", "YYBPC_CLI_HOME": "$state.dir", "FIXED": "1"}
    assert render_env(env, ctx_with_user) == {
        "YYBPC_CLI_USER_PHONE": "138",
        "YYBPC_CLI_HOME": "/data/cli_state/t/tool/u1",
        "FIXED": "1",
    }
    # No user in context → $user.* / $state.* entries are skipped entirely
    # (svc then runs identity-less and reports NOT_LOGGED_IN itself).
    assert render_env(env, PlaceholderContext()) == {"FIXED": "1"}


# ──────────────────────────────────────────────────────────────────────────────
# DB-backed tests for build_cli_injection
# ──────────────────────────────────────────────────────────────────────────────
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def cli_inject_session(monkeypatch):
    """aiosqlite engine with identity+users+tools tables; patches agent_tools.async_session.

    Strategy:
    - Import agent_tools (and transitively Tenant, MCPServer, etc.) first so all
      ORM-managed tables are registered in Base.metadata before we call .create().
      This satisfies every FK resolution at ORM create time.
    - MCPServer uses JSONB (PostgreSQL-only): create it via raw DDL stub so SQLite
      doesn't choke on the column type.
    - Tenant FKs to llm_models and other tables we don't need; create Tenant via
      raw DDL stub too — it's only needed to satisfy users.tenant_id at INSERT time
      (and SQLite doesn't enforce FK constraints without PRAGMA foreign_keys=ON).
    - Create identities first, then users (FK order), then tools.
    """
    # Import agent_tools first — this pulls in Tenant, etc. and registers
    # their tables in Base.metadata, satisfying FK resolution for users.
    # Also import mcp_server explicitly: agent_tools only imports it lazily
    # (inside function body), so the mcp_servers table wouldn't otherwise
    # be in metadata when Tool.__table__.create() runs.
    import app.services.agent_tools as at_mod  # noqa: F401
    import app.models.mcp_server  # noqa: F401 — registers mcp_servers in Base.metadata
    from app.models.user import Identity, User
    from app.models.tool import Tool

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        # Stub tables that have complex types (JSONB) or deep FK chains.
        # Raw DDL satisfies physical FK constraint checks at INSERT time;
        # the ORM metadata entry (already registered by the imports above)
        # satisfies SQLAlchemy FK resolution at .create() time.
        for stub in (
            "mcp_servers", "tenants", "llm_models", "agents",
            "tasks", "channel_configs", "org_members", "org_departments",
            "agent_agent_relationships", "agent_relationships",
            "agent_permissions", "agent_templates", "agent_user_onboardings",
            "task_logs", "mcp_server_overrides",
        ):
            await conn.execute(text(f"CREATE TABLE IF NOT EXISTS {stub} (id TEXT PRIMARY KEY)"))
        # Create the tables we actually insert into, in FK-dependency order.
        await conn.run_sync(lambda c: Identity.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: User.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: Tool.__table__.create(c, checkfirst=True))

    Session = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(at_mod, "async_session", Session)
    yield Session
    await eng.dispose()


@pytest.mark.asyncio
async def test_build_inject_for_agent_renders_cli_tools(cli_inject_session, monkeypatch, tmp_path):
    from app.models.user import Identity, User
    from app.models.tool import Tool
    from app.services.agent_tools import build_cli_injection
    from app.services.cli_tools import state_storage as ss_mod

    monkeypatch.setattr(ss_mod.os, "chown", lambda p, u, g: None)
    monkeypatch.setenv("CLI_STATE_ROOT", str(tmp_path))

    async with cli_inject_session() as s:
        # User model uses association_proxy → Identity for email/phone.
        identity = Identity(email="u@x.com", phone="13800000000", password_hash="x")
        s.add(identity)
        await s.flush()
        user = User(
            identity_id=identity.id,
            display_name="Test User",
            role="member",
            is_active=True,
        )
        s.add(user)
        await s.flush()
        uid = user.id
        tool = Tool(
            name="svc", display_name="svc", description="数据查询 CLI", type="cli",
            category="cli", icon="🔧", source="admin", enabled=True, is_default=True,
            parameters_schema={},
            config={
                "binary": {"sha256": "a" * 64, "size": 1, "original_name": "svc"},
                "env": {"YYBPC_CLI_USER_PHONE": "$user.phone", "YYBPC_CLI_HOME": "$state.dir"},
            },
            config_schema={},
        )
        s.add(tool)
        await s.commit()
        tid = tool.id

    injection = await build_cli_injection(agent_id=None, user_id=uid)
    assert injection is not None
    # Must return dict shape with env and wrappers
    assert "env" in injection
    assert "wrappers" in injection
    # svc wrapper is present
    wrapper_names = [w["name"] for w in injection["wrappers"]]
    assert "svc" in wrapper_names
    # binary path contains the tool's tenant-key, id, and sha256
    svc_wrapper = next(w for w in injection["wrappers"] if w["name"] == "svc")
    assert f"_global/{tid}/{'a' * 64}.bin" in svc_wrapper["binary_path"]
    # identity env is resolved
    assert injection["env"].get("YYBPC_CLI_USER_PHONE") == "13800000000"


@pytest.mark.asyncio
async def test_build_cli_injection_only_tool_names_filters(cli_inject_session, monkeypatch, tmp_path):
    from app.models.user import Identity, User
    from app.models.tool import Tool
    from app.services.agent_tools import build_cli_injection
    from app.services.cli_tools import state_storage as ss_mod

    monkeypatch.setattr(ss_mod.os, "chown", lambda p, u, g: None)
    monkeypatch.setenv("CLI_STATE_ROOT", str(tmp_path))

    async with cli_inject_session() as s:
        identity = Identity(email="u2@x.com", phone="13800000001", password_hash="x")
        s.add(identity)
        await s.flush()
        user = User(identity_id=identity.id, display_name="U2", role="member", is_active=True)
        s.add(user)
        await s.flush()
        uid = user.id
        for nm in ("svc", "foo"):
            s.add(Tool(
                name=nm, display_name=nm, description=f"{nm} CLI", type="cli",
                category="cli", icon="🔧", source="admin", enabled=True, is_default=True,
                parameters_schema={},
                config={
                    "binary": {"sha256": "a" * 64, "size": 1, "original_name": nm},
                    "env": {"YYBPC_CLI_USER_PHONE": "$user.phone"},
                },
                config_schema={},
            ))
        await s.commit()

    injection_all = await build_cli_injection(agent_id=None, user_id=uid)
    all_names = [w["name"] for w in injection_all["wrappers"]]
    assert "svc" in all_names and "foo" in all_names

    injection_one = await build_cli_injection(agent_id=None, user_id=uid, only_tool_names={"svc"})
    one_names = [w["name"] for w in injection_one["wrappers"]]
    assert "svc" in one_names and "foo" not in one_names


@pytest.mark.asyncio
async def test_build_inject_no_cli_tools_returns_none(cli_inject_session):
    from app.services.agent_tools import build_cli_injection
    assert await build_cli_injection(agent_id=None, user_id=None) is None


# ──────────────────────────────────────────────────────────────────────────────
# Tests for get_agent_tools_for_llm: cli tools must not appear as LLM functions;
# their docs must be appended to execute_code_aio's description instead.
# ──────────────────────────────────────────────────────────────────────────────
import uuid as _uuid


@pytest.fixture
async def llm_tools_session(monkeypatch):
    """aiosqlite with tools+agent_tools; patches agent_tools.async_session
    and stubs channel/os helpers so get_agent_tools_for_llm runs offline."""
    import app.services.agent_tools as at_mod  # import FIRST (registers ORM models)
    import app.models.mcp_server  # noqa: F401 — registers mcp_servers in Base.metadata
    from app.models.tool import Tool, AgentTool
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        # FK target stubs (PK-only) for tools/agent_tools foreign keys.
        # Add table names here if .create() raises NoReferencedTableError.
        for stub in (
            "mcp_servers", "tenants", "agents",
            "llm_models", "tasks", "channel_configs", "org_members", "org_departments",
            "agent_agent_relationships", "agent_relationships", "agent_permissions",
            "agent_templates", "agent_user_onboardings", "task_logs", "mcp_server_overrides",
        ):
            await conn.execute(text(f"CREATE TABLE IF NOT EXISTS {stub} (id TEXT PRIMARY KEY)"))
        await conn.run_sync(lambda c: Tool.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: AgentTool.__table__.create(c, checkfirst=True))

    async def _false(*a, **k):
        return False

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(at_mod, "_agent_has_feishu", _false)
    monkeypatch.setattr(at_mod, "_agent_has_any_channel", _false)
    monkeypatch.setattr(at_mod, "_get_computer_os_type", _none)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(at_mod, "async_session", Session)
    yield Session
    await eng.dispose()


@pytest.mark.asyncio
async def test_cli_tool_is_standalone_function_not_folded(llm_tools_session):
    """A CLI tool with a binary surfaces as its own LLM function (with a
    `command` param) and is NOT folded into execute_code_aio's description."""
    from app.models.tool import Tool
    from app.services.agent_tools import get_agent_tools_for_llm

    async with llm_tools_session() as s:
        s.add(Tool(
            name="svc", display_name="黄鹤楼主档",
            description="数据查询 CLI。report 是唯一数据来源。",
            type="cli", category="cli", icon="🔧", source="admin", enabled=True,
            is_default=True, parameters_schema={},
            config={"binary": {"sha256": "a" * 64, "size": 10, "original_name": "svc"}},
            config_schema={},
        ))
        s.add(Tool(
            name="execute_code_aio", display_name="Sandbox",
            description="Run code in sandbox.", type="builtin", category="code",
            icon="💻", source="builtin", enabled=True, is_default=True,
            parameters_schema={"type": "object", "properties": {}},
            config={}, config_schema={},
        ))
        await s.commit()

    tools = await get_agent_tools_for_llm(_uuid.uuid4())
    names = [t["function"]["name"] for t in tools]
    # svc is now a standalone LLM function, not folded into aio.
    assert "svc" in names
    svc = next(t for t in tools if t["function"]["name"] == "svc")
    # The function must have a "command" property (exact description text may vary)
    assert "command" in svc["function"]["parameters"]["properties"]
    assert svc["function"]["parameters"]["required"] == ["command"]
    # execute_code_aio description is clean — no folded CLI docs.
    aio = next(t for t in tools if t["function"]["name"] == "execute_code_aio")
    assert "svc" not in (aio["function"]["description"] or "")
    assert "Sandbox CLI commands" not in (aio["function"]["description"] or "")


@pytest.mark.asyncio
async def test_cli_tool_without_binary_not_surfaced(llm_tools_session):
    """A CLI tool with no uploaded binary does not appear as an LLM function."""
    from app.models.tool import Tool
    from app.services.agent_tools import get_agent_tools_for_llm

    async with llm_tools_session() as s:
        s.add(Tool(
            name="svc", display_name="svc", description="no binary yet",
            type="cli", category="cli", icon="🔧", source="admin", enabled=True,
            is_default=True, parameters_schema={}, config={}, config_schema={},
        ))
        s.add(Tool(
            name="execute_code_aio", display_name="Sandbox",
            description="Run code in sandbox.", type="builtin", category="code",
            icon="💻", source="builtin", enabled=True, is_default=True,
            parameters_schema={"type": "object", "properties": {}},
            config={}, config_schema={},
        ))
        await s.commit()

    tools = await get_agent_tools_for_llm(_uuid.uuid4())
    names = [t["function"]["name"] for t in tools]
    assert "svc" not in names


@pytest.mark.asyncio
async def test_creator_identity_bound_for_autonomous_origin(cli_inject_session, monkeypatch, tmp_path):
    """Trigger/cron and A2A pass the agent creator's User PK as user_id;
    svc binds to the creator's phone (digital employee acts on its owner's
    behalf). Confirmed product semantics — NOT identity-less for autonomous
    origins. Guards against regressing to the old (wrong) NOT_LOGGED_IN spec."""
    from app.models.tool import Tool
    from app.models.user import Identity, User
    from app.services.agent_tools import build_cli_injection
    from app.services.cli_tools import state_storage as ss_mod

    monkeypatch.setattr(ss_mod.os, "chown", lambda p, u, g: None)
    monkeypatch.setenv("CLI_STATE_ROOT", str(tmp_path))

    async with cli_inject_session() as s:
        # User model uses association_proxy → Identity for email/phone.
        identity = Identity(email="creator@x.com", phone="13900000000", password_hash="x")
        s.add(identity)
        await s.flush()
        creator = User(identity_id=identity.id, display_name="Creator", role="member", is_active=True)
        s.add(creator)
        await s.flush()
        creator_id = creator.id
        s.add(Tool(
            name="svc", display_name="svc", description="d", type="cli",
            category="cli", icon="🔧", source="admin", enabled=True, is_default=True,
            parameters_schema={},
            config={"binary": {"sha256": "a" * 64, "size": 1, "original_name": "svc"},
                    "env": {"YYBPC_CLI_USER_PHONE": "$user.phone", "YYBPC_CLI_HOME": "$state.dir"}},
            config_schema={},
        ))
        await s.commit()

    # Autonomous origin passes the creator's User PK (as heartbeat.py / A2A do).
    injection = await build_cli_injection(agent_id=None, user_id=creator_id)
    assert injection is not None
    # creator identity must be bound, not dropped
    assert injection["env"].get("YYBPC_CLI_USER_PHONE") == "13900000000"


# ──────────────────────────────────────────────────────────────────────────────
# D3-2: cross-tenant isolation — admin tool scoped to tenant A must not inject
# into an agent belonging to tenant B.
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
async def cli_inject_session_agents(monkeypatch):
    """Like cli_inject_session but also creates agent_tools table and an
    agents stub with a tenant_id column so build_cli_injection's
    select(AgentModel.tenant_id) query can return a non-null value."""
    import app.services.agent_tools as at_mod  # noqa: F401 — registers ORM
    import app.models.mcp_server  # noqa: F401
    from app.models.user import Identity, User
    from app.models.tool import Tool, AgentTool
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        # Stubs for tables with JSONB or deep FK chains.
        for stub in (
            "mcp_servers", "tenants", "llm_models",
            "tasks", "channel_configs", "org_members", "org_departments",
            "agent_agent_relationships", "agent_relationships",
            "agent_permissions", "agent_templates", "agent_user_onboardings",
            "task_logs", "mcp_server_overrides",
        ):
            await conn.execute(text(f"CREATE TABLE IF NOT EXISTS {stub} (id TEXT PRIMARY KEY)"))
        # agents needs at least id + tenant_id; build_cli_injection only
        # selects AgentModel.tenant_id so extra columns are not needed.
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS agents "
            "(id TEXT PRIMARY KEY, tenant_id TEXT)"
        ))
        await conn.run_sync(lambda c: Identity.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: User.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: Tool.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: AgentTool.__table__.create(c, checkfirst=True))

    Session = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(at_mod, "async_session", Session)
    yield Session
    await eng.dispose()


@pytest.mark.asyncio
async def test_cross_tenant_admin_tool_not_injected(cli_inject_session_agents, monkeypatch, tmp_path):
    """Admin cli tool scoped to tenant A must NOT be injected into an agent
    belonging to tenant B (cross-tenant isolation, D3-2).

    The agents stub uses TEXT columns; SQLAlchemy's UUID type stores UUID
    values as hex-without-dashes in SQLite, so insert with .hex format so
    the select(AgentModel.tenant_id) query can find the row.
    """
    import uuid as _uuid_mod
    from app.models.tool import Tool
    from app.services.agent_tools import build_cli_injection
    from app.services.cli_tools import state_storage as ss_mod

    monkeypatch.setattr(ss_mod.os, "chown", lambda p, u, g: None)
    monkeypatch.setenv("CLI_STATE_ROOT", str(tmp_path))

    tenant_a = _uuid_mod.uuid4()
    tenant_b = _uuid_mod.uuid4()
    agent_b_id = _uuid_mod.uuid4()

    async with cli_inject_session_agents() as s:
        # Admin cli tool explicitly scoped to tenant A.
        s.add(Tool(
            name="svc_a", display_name="svc_a", description="tenant-A CLI",
            type="cli", category="cli", icon="🔧", source="admin", enabled=True,
            is_default=True, parameters_schema={},
            config={
                "binary": {"sha256": "b" * 64, "size": 1, "original_name": "svc_a"},
                "env": {},
            },
            config_schema={},
            tenant_id=tenant_a,
        ))
        await s.commit()
        # Raw-insert agent row for tenant B. Use hex format (no dashes) so
        # SQLAlchemy's UUID type lookup (which sends hex to SQLite) finds it.
        await s.execute(
            text("INSERT INTO agents (id, tenant_id) VALUES (:id, :tid)"),
            {"id": agent_b_id.hex, "tid": tenant_b.hex},
        )
        await s.commit()

    # Tenant B agent must not receive tenant A's CLI tool.
    injection_b = await build_cli_injection(agent_id=agent_b_id, user_id=None)
    assert injection_b is None, (
        "cross-tenant CLI injection: tenant A tool must not appear in tenant B agent's injection"
    )


@pytest.mark.asyncio
async def test_same_tenant_admin_tool_injected(cli_inject_session_agents, monkeypatch, tmp_path):
    """Admin cli tool scoped to tenant A IS injected into an agent from tenant A."""
    import uuid as _uuid_mod
    from app.models.tool import Tool
    from app.services.agent_tools import build_cli_injection
    from app.services.cli_tools import state_storage as ss_mod

    monkeypatch.setattr(ss_mod.os, "chown", lambda p, u, g: None)
    monkeypatch.setenv("CLI_STATE_ROOT", str(tmp_path))

    tenant_a = _uuid_mod.uuid4()
    agent_a_id = _uuid_mod.uuid4()

    async with cli_inject_session_agents() as s:
        s.add(Tool(
            name="svc_a", display_name="svc_a", description="tenant-A CLI",
            type="cli", category="cli", icon="🔧", source="admin", enabled=True,
            is_default=True, parameters_schema={},
            config={
                "binary": {"sha256": "c" * 64, "size": 1, "original_name": "svc_a"},
                "env": {},
            },
            config_schema={},
            tenant_id=tenant_a,
        ))
        await s.commit()
        # Use hex format so the UUID column lookup finds the row.
        await s.execute(
            text("INSERT INTO agents (id, tenant_id) VALUES (:id, :tid)"),
            {"id": agent_a_id.hex, "tid": tenant_a.hex},
        )
        await s.commit()

    injection_a = await build_cli_injection(agent_id=agent_a_id, user_id=None)
    assert injection_a is not None, "same-tenant CLI tool must be injected into same-tenant agent"
    wrapper_names = [w["name"] for w in injection_a["wrappers"]]
    assert "svc_a" in wrapper_names


# ──────────────────────────────────────────────────────────────────────────────
# D3-1: graceful degrade — agent has cli tool but no execute_code_aio.
# get_agent_tools_for_llm must not raise; cli tool must not appear in result;
# execute_code_aio description must not contain cli suffix.
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cli_tool_no_aio_graceful_degrade(llm_tools_session):
    """When an agent has a cli tool enabled but execute_code_aio is NOT in the
    tool list, get_agent_tools_for_llm must not raise and must not surface the
    cli tool as an LLM function (graceful degrade, D3-1)."""
    from app.models.tool import Tool
    from app.services.agent_tools import get_agent_tools_for_llm

    async with llm_tools_session() as s:
        s.add(Tool(
            name="svc", display_name="svc", description="CLI only, no aio in this agent",
            type="cli", category="cli", icon="🔧", source="admin", enabled=True,
            is_default=True, parameters_schema={}, config={}, config_schema={},
        ))
        # Intentionally do NOT add execute_code_aio.
        await s.commit()

    tools = await get_agent_tools_for_llm(_uuid.uuid4())
    names = [t["function"]["name"] for t in tools]
    assert "svc" not in names, "cli tool must not appear as an LLM function"
    # execute_code_aio is absent — no description to check; just verify no crash.
    assert "execute_code_aio" not in names
