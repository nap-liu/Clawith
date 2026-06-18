"""Pydantic schemas for PAT (Personal Access Token) REST endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PATCreate(BaseModel):
    """Request body for POST /api/personal-access-tokens."""

    name: str
    expires_at: datetime | None = None
    scope: str = "read"


class PATCreated(BaseModel):
    """Response for POST — contains plaintext token (issued once, never returned again)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    token: str  # plaintext — only present on creation
    token_prefix: str
    scope: str
    created_at: datetime
    expires_at: datetime | None


class PATOut(BaseModel):
    """Response item for GET list — no plaintext token."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    token_prefix: str
    scope: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
