# backend/tests/test_mcp_stdio_schema.py
import pytest
from pydantic import ValidationError
from app.schemas.mcp_server import MCPServerCreate, MCPServerUpdate, MCPServerOut, MCPServerOverridePut


def test_create_accepts_stdio():
    m = MCPServerCreate(
        name="yx", display_name="yx", base_url_template="",
        transport="stdio", command_template="npx",
        args_template=["-y", "alibabacloud-devops-mcp-server"],
        env_template={"YUNXIAO_ACCESS_TOKEN": "${agent.tok}"},
    )
    assert m.transport == "stdio"
    assert m.command_template == "npx"
    assert m.args_template == ["-y", "alibabacloud-devops-mcp-server"]
    assert m.env_template == {"YUNXIAO_ACCESS_TOKEN": "${agent.tok}"}


def test_create_http_requires_base_url():
    """http transport must have a non-empty base_url_template."""
    with pytest.raises(ValidationError):
        MCPServerCreate(name="x", display_name="x", base_url_template="", transport="http")


def test_create_http_default_accepts_url():
    """Default transport=http with a URL is still valid."""
    m = MCPServerCreate(name="srv", display_name="srv", base_url_template="https://x/mcp")
    assert m.transport == "http"
    assert m.base_url_template == "https://x/mcp"


def test_create_stdio_requires_command():
    """stdio transport must have a non-empty command_template."""
    with pytest.raises(ValidationError):
        MCPServerCreate(name="yx", display_name="yx", base_url_template="", transport="stdio")


def test_update_accepts_stdio_fields():
    u = MCPServerUpdate(
        transport="stdio",
        command_template="uvx",
        args_template=["mcp-server-git"],
        env_template={"GH_TOKEN": "${agent.gh_token}"},
    )
    assert u.transport == "stdio"
    assert u.command_template == "uvx"


def test_create_rejects_unknown_transport():
    """MCPServerCreate with an unknown transport (e.g. 'grpc') must raise ValidationError."""
    with pytest.raises(ValidationError):
        MCPServerCreate(name="g", display_name="g", base_url_template="https://x/mcp", transport="grpc")


def test_update_rejects_unknown_transport():
    """MCPServerUpdate with an unsupported transport must raise ValidationError."""
    with pytest.raises(ValidationError):
        MCPServerUpdate(transport="ftp")


def test_override_put_accepts_env_template():
    """MCPServerOverridePut should accept env_template (stdio credentials per agent)."""
    p = MCPServerOverridePut(env_template={"T": "x"})
    assert p.env_template == {"T": "x"}
    assert p.command_template is None
    assert p.args_template is None


def test_override_put_accepts_all_stdio_fields():
    """MCPServerOverridePut should accept command/args/env together."""
    p = MCPServerOverridePut(
        command_template="npx",
        args_template=["-y", "pkg"],
        env_template={"KEY": "val"},
    )
    assert p.command_template == "npx"
    assert p.args_template == ["-y", "pkg"]
    assert p.env_template == {"KEY": "val"}


def test_out_includes_stdio_fields():
    """MCPServerOut should include transport, command_template, args_template, env_template."""
    import uuid
    from datetime import datetime, timezone

    class FakeServer:
        id = uuid.uuid4()
        tenant_id = None
        name = "yx"
        display_name = "yx"
        base_url_template = ""
        headers_template = {}
        credential_template = None
        system_prompt_block = None
        placeholder_allowlist = None
        instructions = None
        instructions_captured_at = None
        created_by_user_id = None
        created_at = datetime.now(timezone.utc)
        updated_at = datetime.now(timezone.utc)
        transport = "stdio"
        command_template = "npx"
        args_template = ["-y", "pkg"]
        env_template = {"K": "${agent.v}"}

    out = MCPServerOut.from_orm_model(FakeServer())
    assert out.transport == "stdio"
    assert out.command_template == "npx"
    assert out.args_template == ["-y", "pkg"]
    assert out.env_template == {"K": "${agent.v}"}
