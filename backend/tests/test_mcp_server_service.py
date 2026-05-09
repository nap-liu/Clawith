import uuid
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.services.mcp_server_service import compose_runtime_config


def _server(**kw):
    base = dict(
        id=uuid.uuid4(), tenant_id=None, name="srv", display_name="srv",
        base_url_template="https://srv/x", headers_template={"H": "h0"},
        credential_template="cred-base", system_prompt_block="P-base",
        instructions=None,
    )
    base.update(kw)
    return MCPServer(**base)


def _override(server_id, scope_type, **kw):
    base = dict(
        id=uuid.uuid4(),
        mcp_server_id=server_id,
        scope_type=scope_type,
        scope_id=uuid.uuid4(),
        system_prompt_block=None,
        url_template=None,
        headers_template=None,
        credential_template=None,
    )
    base.update(kw)
    return MCPServerOverride(**base)


def test_compose_no_overrides_returns_server_values():
    srv = _server()
    cfg = compose_runtime_config(srv, None, None)
    assert cfg.url_template == "https://srv/x"
    assert cfg.headers_template == {"H": "h0"}
    assert cfg.credential_template == "cred-base"
    assert cfg.prompt_blocks == ["P-base"]


def test_compose_tenant_overrides_url_and_appends_prompt():
    srv = _server()
    t_ovr = _override(srv.id, "tenant", url_template="https://tenant/y", system_prompt_block="P-tenant")
    cfg = compose_runtime_config(srv, t_ovr, None)
    assert cfg.url_template == "https://tenant/y"
    assert cfg.prompt_blocks == ["P-base", "P-tenant"]
    # headers + credential not overridden
    assert cfg.headers_template == {"H": "h0"}
    assert cfg.credential_template == "cred-base"


def test_compose_agent_wins_over_tenant_for_url():
    srv = _server()
    t_ovr = _override(srv.id, "tenant", url_template="https://tenant/y")
    a_ovr = _override(srv.id, "agent", url_template="https://agent/z")
    cfg = compose_runtime_config(srv, t_ovr, a_ovr)
    assert cfg.url_template == "https://agent/z"


def test_compose_three_layer_prompt_append_in_order():
    srv = _server(system_prompt_block="A")
    t_ovr = _override(srv.id, "tenant", system_prompt_block="B")
    a_ovr = _override(srv.id, "agent", system_prompt_block="C")
    cfg = compose_runtime_config(srv, t_ovr, a_ovr)
    assert cfg.prompt_blocks == ["A", "B", "C"]


def test_compose_skips_empty_prompt_layers():
    srv = _server(system_prompt_block=None)
    a_ovr = _override(srv.id, "agent", system_prompt_block="only-agent")
    cfg = compose_runtime_config(srv, None, a_ovr)
    assert cfg.prompt_blocks == ["only-agent"]


def test_compose_headers_template_replace_not_merge():
    srv = _server(headers_template={"H1": "v1", "H2": "v2"})
    a_ovr = _override(srv.id, "agent", headers_template={"H1": "newv"})
    cfg = compose_runtime_config(srv, None, a_ovr)
    # Replace semantics — H2 is gone (per spec §3.3)
    assert cfg.headers_template == {"H1": "newv"}
