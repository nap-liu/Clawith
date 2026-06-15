# backend/tests/test_mcp_config_parser_stdio.py
from app.services.mcp_config_parser import _from_server_spec


def test_stdio_spec_parsed_not_rejected():
    out = _from_server_spec({"command": "npx", "args": ["-y", "pkg"], "env": {"K": "v"}}, "yx")
    assert out.get("error") is None
    assert out["transport"] == "stdio"
    assert out["command"] == "npx"
    assert out["args"] == ["-y", "pkg"]
    assert out["env"] == {"K": "v"}


def test_http_spec_still_works():
    out = _from_server_spec({"url": "https://x/mcp"}, "h")
    assert out.get("error") is None
    assert out.get("transport", "http") == "http"
    assert out["url"] == "https://x/mcp"


def test_stdio_spec_no_args_defaults_empty():
    """stdio spec without args/env should default to empty collections."""
    out = _from_server_spec({"command": "uvx", "args": [], "env": {}}, "yx2")
    assert out.get("error") is None
    assert out["transport"] == "stdio"
    assert out["args"] == []
    assert out["env"] == {}


def test_stdio_spec_missing_args_still_ok():
    """stdio spec without optional args/env keys should still parse."""
    out = _from_server_spec({"command": "npx"}, "yx3")
    assert out.get("error") is None
    assert out["transport"] == "stdio"
    assert out["args"] == []
    assert out["env"] == {}


def test_parse_mcp_input_bare_url_returns_transport_http():
    """A bare URL string must include transport='http' in the result dict."""
    from app.services.mcp_config_parser import parse_mcp_input
    out = parse_mcp_input("https://myserver.example/mcp")
    assert out is not None
    assert out.get("error") is None
    assert out["transport"] == "http"
    assert out["url"] == "https://myserver.example/mcp"


def test_parse_mcp_input_stdio_via_mcpservers_block():
    """Full mcpServers block with stdio entry should parse correctly."""
    from app.services.mcp_config_parser import parse_mcp_input
    inp = {"mcpServers": {"yunxiao": {"command": "npx", "args": ["-y", "pkg"], "env": {"T": "1"}}}}
    out = parse_mcp_input(inp)
    assert out is not None
    assert out.get("error") is None
    assert out["transport"] == "stdio"
    assert out["command"] == "npx"
