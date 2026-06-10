"""Pure-function tests for the sandbox CLI injection block builder."""
import pytest

from app.services.cli_tools.sandbox_inject import (
    build_cli_function,
    render_env,
    shell_quote,
)
from app.services.cli_tools.placeholders import PlaceholderContext


def test_shell_quote_wraps_and_escapes():
    assert shell_quote("abc") == "'abc'"
    assert shell_quote("a'b") == "'a'\\''b'"


def test_build_cli_function_binds_env_inside_function():
    text = build_cli_function(
        name="svc",
        binary_path="/data/cli_binaries/_global/t1/aa.bin",
        env={"YYBPC_CLI_USER_PHONE": "13800000000", "YYBPC_CLI_HOME": "/data/cli_state/x"},
    )
    # One function definition, env as command-prefix assignments (NOT export).
    assert text.startswith("svc() {")
    assert "export" not in text
    assert "YYBPC_CLI_USER_PHONE='13800000000'" in text
    assert "YYBPC_CLI_HOME='/data/cli_state/x'" in text
    assert "'/data/cli_binaries/_global/t1/aa.bin' \"$@\"" in text
    assert text.rstrip().endswith("}")


def test_build_cli_function_rejects_unsafe_name():
    with pytest.raises(ValueError):
        build_cli_function(name="bad name; rm", binary_path="/x", env={})


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


def test_build_cli_function_rejects_unsafe_env_key():
    with pytest.raises(ValueError):
        build_cli_function(name="svc", binary_path="/x", env={"A; touch /tmp/P; B": "v"})


def test_build_cli_function_rejects_trailing_newline_name():
    with pytest.raises(ValueError):
        build_cli_function(name="svc\n", binary_path="/x", env={})


# ──────────────────────────────────────────────────────────────────────────────
# DB-backed tests for build_cli_inject_prefix
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
async def test_build_inject_prefix_for_agent_renders_cli_tools(cli_inject_session, monkeypatch, tmp_path):
    from app.models.user import Identity, User
    from app.models.tool import Tool
    from app.services.agent_tools import build_cli_inject_prefix
    from app.services.cli_tools import state_storage as ss_mod

    monkeypatch.setattr(ss_mod.os, "chown", lambda p, u, g: None)
    monkeypatch.setenv("CLI_STATE_ROOT", str(tmp_path))
    # CLI_TOOLS_ROOT determines where binary paths are rendered (storage_root in impl).
    binaries_root = tmp_path / "binaries"
    monkeypatch.setenv("CLI_TOOLS_ROOT", str(binaries_root))

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

    prefix = await build_cli_inject_prefix(agent_id=None, user_id=uid)
    assert prefix is not None
    assert "svc() {" in prefix
    assert "YYBPC_CLI_USER_PHONE='13800000000'" in prefix
    assert f"_global/{tid}/{'a' * 64}.bin" in prefix


@pytest.mark.asyncio
async def test_build_inject_prefix_no_cli_tools_returns_none(cli_inject_session):
    from app.services.agent_tools import build_cli_inject_prefix
    assert await build_cli_inject_prefix(agent_id=None, user_id=None) is None
