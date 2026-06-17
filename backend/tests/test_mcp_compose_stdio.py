# backend/tests/test_mcp_compose_stdio.py
import uuid
from app.services.mcp_server_service import compose_runtime_config
from app.models.mcp_server import MCPServer, MCPServerOverride


def _server(**kw):
    base = dict(
        id=uuid.uuid4(), tenant_id=None, name="yx", display_name="yx",
        base_url_template="", headers_template={},
        credential_template=None, system_prompt_block=None, instructions=None,
        transport="stdio", command_template="npx",
        args_template=["-y", "pkg"], env_template={"T": "${agent.a}"},
    )
    base.update(kw)
    return MCPServer(**base)


def _override(server_id, scope_type, **kw):
    base = dict(
        id=uuid.uuid4(), mcp_server_id=server_id, scope_type=scope_type,
        scope_id=uuid.uuid4(), system_prompt_block=None,
        url_template=None, headers_template=None, credential_template=None,
        command_template=None, args_template=None, env_template=None,
    )
    base.update(kw)
    return MCPServerOverride(**base)


def test_compose_stdio_picks_agent_over_server():
    srv = _server()
    a = _override(srv.id, "agent", env_template={"T": "${agent.b}"})
    cfg = compose_runtime_config(srv, None, a)
    assert cfg.transport == "stdio"
    assert cfg.command_template == "npx"
    assert cfg.args_template == ["-y", "pkg"]
    assert cfg.env_template == {"T": "${agent.b}"}   # agent override wins


def test_compose_stdio_no_overrides_returns_server_values():
    srv = _server()
    cfg = compose_runtime_config(srv, None, None)
    assert cfg.transport == "stdio"
    assert cfg.command_template == "npx"
    assert cfg.args_template == ["-y", "pkg"]
    assert cfg.env_template == {"T": "${agent.a}"}


def test_compose_stdio_tenant_override_wins_over_server():
    srv = _server()
    t = _override(srv.id, "tenant", args_template=["-y", "other-pkg"])
    cfg = compose_runtime_config(srv, t, None)
    assert cfg.args_template == ["-y", "other-pkg"]


def test_compose_stdio_agent_wins_over_tenant():
    srv = _server()
    t = _override(srv.id, "tenant", command_template="uvx")
    a = _override(srv.id, "agent", command_template="bunx")
    cfg = compose_runtime_config(srv, t, a)
    assert cfg.command_template == "bunx"


def test_compose_http_server_no_stdio_fields():
    """HTTP server should have transport=http and None for stdio fields."""
    srv = MCPServer(
        id=uuid.uuid4(), tenant_id=None, name="h", display_name="h",
        base_url_template="https://x/mcp", headers_template={},
        credential_template=None, system_prompt_block=None, instructions=None,
        # No transport/command/args/env — rely on defaults
    )
    cfg = compose_runtime_config(srv, None, None)
    assert cfg.transport == "http"
    assert cfg.command_template is None
    assert cfg.args_template is None
    assert cfg.env_template is None
