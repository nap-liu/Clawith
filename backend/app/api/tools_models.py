"""Pydantic models for the tools API."""

import uuid

from pydantic import BaseModel, Field, field_validator


class ToolCreate(BaseModel):
    name: str
    display_name: str
    description: str = ""
    type: str = "mcp"
    category: str = "custom"
    icon: str = "🔧"
    parameters_schema: dict = {}
    mcp_server_url: str | None = None
    mcp_server_name: str | None = None
    mcp_tool_name: str | None = None
    is_default: bool = False
    # Optional: platform admins can specify target tenant (e.g. when managing
    # another company's tools via the Enterprise Settings page).
    tenant_id: str | None = None


class ToolUpdate(BaseModel):
    display_name: str | None = Field(None, max_length=200)
    description: str | None = None
    icon: str | None = None
    enabled: bool | None = None
    mcp_server_url: str | None = None
    mcp_server_name: str | None = None
    parameters_schema: dict | None = None
    is_default: bool | None = None
    config: dict | None = None
    tenant_id: str | None = None

    @field_validator("display_name")
    @classmethod
    def _normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("display_name is required")
        return normalized


class AgentToolUpdate(BaseModel):
    tool_id: str
    enabled: bool


class CategoryConfigUpdate(BaseModel):
    config: dict


class BulkToolUpdateItem(BaseModel):
    tool_id: str
    enabled: bool


class MCPServerUpdate(BaseModel):
    server_id: uuid.UUID | None = None
    server_name: str            # Identifies which server's tools to update
    server_url: str             # New MCP server URL (may contain embedded key)
    api_key: str | None = None  # Optional standalone Bearer key
    # Target tenant (platform admins may manage another company's tools)
    tenant_id: str | None = None
    # NEW (P4 bridge to mcp_servers table)
    system_prompt_block: str | None = None
    headers_template: dict | None = None


class MCPTestRequest(BaseModel):
    server_url: str
    # Optional standalone API Key. If provided, it is sent as
    # 'Authorization: Bearer {api_key}' and is NOT embedded in the URL.
    api_key: str | None = None


class AgentToolConfigUpdate(BaseModel):
    config: dict


class EmailTestRequest(BaseModel):
    config: dict
