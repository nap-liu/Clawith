"""Secret-free channel configuration API DTOs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


_SENSITIVE_PARTS = (
    "secret",
    "token",
    "password",
    "api_key",
    "private_key",
    "signing_key",
    "encrypt_key",
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SENSITIVE_PARTS)
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class ChannelConfigPublic(BaseModel):
    """Channel status and non-secret settings safe for human-facing APIs."""

    id: uuid.UUID
    agent_id: uuid.UUID
    channel_type: str
    app_id: str | None = None
    is_configured: bool
    is_connected: bool
    last_tested_at: datetime | None = None
    extra_config: dict = Field(default_factory=dict)
    created_at: datetime
    has_app_secret: bool = False
    has_encrypt_key: bool = False
    has_verification_token: bool = False

    @model_validator(mode="before")
    @classmethod
    def from_channel_config(cls, value):
        if isinstance(value, dict):
            data = dict(value)
        else:
            data = {
                "id": value.id,
                "agent_id": value.agent_id,
                "channel_type": value.channel_type,
                "app_id": value.app_id,
                "is_configured": value.is_configured,
                "is_connected": value.is_connected,
                "last_tested_at": value.last_tested_at,
                "extra_config": value.extra_config or {},
                "created_at": value.created_at,
                "has_app_secret": bool(value.app_secret),
                "has_encrypt_key": bool(value.encrypt_key),
                "has_verification_token": bool(value.verification_token),
            }
        data["extra_config"] = _redact(data.get("extra_config") or {})
        data.pop("app_secret", None)
        data.pop("encrypt_key", None)
        data.pop("verification_token", None)
        return data

    model_config = {"from_attributes": True}
