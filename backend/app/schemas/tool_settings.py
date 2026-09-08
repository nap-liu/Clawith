"""Shared tool-panel draft contracts for scoped configurations."""

import uuid
from typing import Any

from pydantic import BaseModel, Field


class ToolSetting(BaseModel):
    tool_id: uuid.UUID
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class MCPServerOverrideSetting(BaseModel):
    server_id: uuid.UUID
    system_prompt_block: str | None = None
    url_template: str | None = None
    headers_template: dict | None = None
    credential_template: str | None = None
    command_template: str | None = None
    args_template: list[str] | None = None
    env_template: dict | None = None
