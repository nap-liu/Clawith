"""Pydantic schemas for MCP server CRUD.

CRITICAL: Response models MUST NOT echo credential_template plaintext.
Use ``credential_state`` (literal "set" or "unset") instead — never
return the encrypted blob, never return the rendered cleartext.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class MCPServerCreate(BaseModel):
    """Request body for POST /api/admin/mcp-servers"""

    name: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$",
                      description="Slug-style identifier; lowercase, digits, _, -")
    display_name: str = Field(..., min_length=1, max_length=200)
    base_url_template: str = Field(default="")
    headers_template: dict = Field(default_factory=dict)
    credential_template: str | None = None  # plaintext on input; encrypted at rest
    system_prompt_block: str | None = None
    placeholder_allowlist: list[str] | None = None
    tenant_id: uuid.UUID | None = None  # platform admin can set; org admin can't override
    transport: str = "http"
    command_template: str | None = None
    args_template: list[str] | None = None
    env_template: dict | None = None

    @model_validator(mode="after")
    def _check_transport_fields(self) -> "MCPServerCreate":
        if self.transport == "http":
            if not (self.base_url_template or "").strip():
                raise ValueError("base_url_template is required for transport=http")
        elif self.transport == "stdio":
            if not (self.command_template or "").strip():
                raise ValueError("command_template is required for transport=stdio")
        return self


class MCPServerUpdate(BaseModel):
    """Request body for PATCH /api/admin/mcp-servers/{id}.

    All fields optional — partial update. ``credential_template`` of None
    means "don't touch"; explicit empty string means "clear it".
    """

    display_name: str | None = Field(None, min_length=1, max_length=200)
    base_url_template: str | None = Field(None, min_length=1)
    headers_template: dict | None = None
    credential_template: str | None = None  # see docstring
    system_prompt_block: str | None = None  # explicit "" to clear
    placeholder_allowlist: list[str] | None = None  # null = don't touch, [] = clear restriction
    transport: str | None = None
    command_template: str | None = None
    args_template: list[str] | None = None
    env_template: dict | None = None


class MCPServerOut(BaseModel):
    """Response — credential field IS NEVER plaintext."""

    id: uuid.UUID
    tenant_id: uuid.UUID | None
    name: str
    display_name: str
    base_url_template: str
    headers_template: dict
    credential_state: Literal["set", "unset"]  # masked
    system_prompt_block: str | None
    placeholder_allowlist: list[str] | None
    instructions: str | None
    instructions_captured_at: datetime | None
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    transport: str = "http"
    command_template: str | None = None
    args_template: list[str] | None = None
    env_template: dict | None = None  # placeholder values, not rendered secrets

    @classmethod
    def from_orm_model(cls, server) -> "MCPServerOut":
        return cls(
            id=server.id,
            tenant_id=server.tenant_id,
            name=server.name,
            display_name=server.display_name,
            base_url_template=server.base_url_template,
            headers_template=server.headers_template or {},
            credential_state="set" if (server.credential_template or "").strip() else "unset",
            system_prompt_block=server.system_prompt_block,
            placeholder_allowlist=server.placeholder_allowlist,
            instructions=server.instructions,
            instructions_captured_at=server.instructions_captured_at,
            created_by_user_id=server.created_by_user_id,
            created_at=server.created_at,
            updated_at=server.updated_at,
            transport=getattr(server, "transport", "http") or "http",
            command_template=getattr(server, "command_template", None),
            args_template=getattr(server, "args_template", None),
            env_template=getattr(server, "env_template", None),
        )


class TestConnectionResult(BaseModel):
    """Response for POST /test-connection"""

    success: bool
    instructions: str | None = None
    server_info: dict | None = None
    error: str | None = None


class MCPServerOverridePut(BaseModel):
    """Request body for PUT /overrides/{scope}/{scope_id}.

    All fields optional — partial update. None = "unset/default";
    string = "set". Use empty string to explicitly clear text fields.
    """

    system_prompt_block: str | None = None
    url_template: str | None = None
    headers_template: dict | None = None
    credential_template: str | None = None  # plaintext on input


class MCPServerOverrideOut(BaseModel):
    id: uuid.UUID
    mcp_server_id: uuid.UUID
    scope_type: Literal["tenant", "agent"]
    scope_id: uuid.UUID
    system_prompt_block: str | None
    url_template: str | None
    headers_template: dict | None
    credential_state: Literal["set", "unset"]
    last_modified_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_model(cls, ovr) -> "MCPServerOverrideOut":
        return cls(
            id=ovr.id,
            mcp_server_id=ovr.mcp_server_id,
            scope_type=ovr.scope_type,  # type: ignore[arg-type]
            scope_id=ovr.scope_id,
            system_prompt_block=ovr.system_prompt_block,
            url_template=ovr.url_template,
            headers_template=ovr.headers_template,
            credential_state="set" if (ovr.credential_template or "").strip() else "unset",
            last_modified_by_user_id=ovr.last_modified_by_user_id,
            created_at=ovr.created_at,
            updated_at=ovr.updated_at,
        )


class OverridesGroupedOut(BaseModel):
    """Response for GET /overrides — grouped by scope_type."""

    tenant: list[MCPServerOverrideOut] = Field(default_factory=list)
    agent: list[MCPServerOverrideOut] = Field(default_factory=list)


class DraftOverrides(BaseModel):
    """Unsaved edits being typed in the UI. dry-run applies these on top of the
    composed (server + tenant + agent) config, then resolves placeholders.
    Any field set to None means "fall through to the composed value".
    """

    base_url_template: str | None = None
    headers_template: dict | None = None
    credential_template: str | None = None
    system_prompt_block: str | None = None


class DryRunRequest(BaseModel):
    """Request for POST /dry-run.

    identity:
    * current_user — uses caller's id/email/etc. for ${user.*} resolution
    * synthetic — uses fixed dummy values; safe for sharing screenshots

    scope:
    * platform — only server.system_prompt_block is rendered
    * tenant — server + tenant override (tenant_id must be set)
    * agent — server + tenant override + agent override (agent_id must be set)
    """

    identity: Literal["current_user", "synthetic"] = "synthetic"
    scope: Literal["platform", "tenant", "agent"] = "platform"
    tenant_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    draft_overrides: DraftOverrides | None = None


class DryRunResponse(BaseModel):
    """Response for POST /dry-run.

    NEVER returns resolved credential plaintext. Authorization-class
    headers are masked.
    """

    resolved_url: str
    resolved_headers: dict[str, str]  # Authorization-class keys masked to "Bearer ***"
    resolved_credential_state: Literal["set", "unset"]
    resolved_prompt: str
    used_layers: list[Literal["platform", "tenant", "agent", "draft"]]
    errors: list[str] = Field(default_factory=list)  # placeholder render errors etc.
