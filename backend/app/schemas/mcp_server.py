"""Pydantic schemas for MCP server CRUD.

CRITICAL: Response models MUST NOT echo credential_template plaintext.
Use ``credential_state`` (literal "set" or "unset") instead — never
return the encrypted blob, never return the rendered cleartext.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MCPServerCreate(BaseModel):
    """Request body for POST /api/admin/mcp-servers"""

    name: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$",
                      description="Slug-style identifier; lowercase, digits, _, -")
    display_name: str = Field(..., min_length=1, max_length=200)
    base_url_template: str = Field(..., min_length=1)
    headers_template: dict = Field(default_factory=dict)
    credential_template: str | None = None  # plaintext on input; encrypted at rest
    system_prompt_block: str | None = None
    tenant_id: uuid.UUID | None = None  # platform admin can set; org admin can't override


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
    instructions: str | None
    instructions_captured_at: datetime | None
    created_at: datetime
    updated_at: datetime

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
            instructions=server.instructions,
            instructions_captured_at=server.instructions_captured_at,
            created_at=server.created_at,
            updated_at=server.updated_at,
        )


class TestConnectionResult(BaseModel):
    """Response for POST /test-connection"""

    success: bool
    instructions: str | None = None
    server_info: dict | None = None
    error: str | None = None
