"""Versioned system integration schemas."""
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCOPES = {"employees:read", "auth:login"}


def validate_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or "\\" in value):
        raise ValueError("Invalid origin")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}:
        raise ValueError("HTTPS origin required")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


class ApplicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    tenant_id: UUID
    enabled: bool = True
    trust_user_identity: bool = False
    scopes: list[str] = Field(default_factory=lambda: sorted(SCOPES))
    embed_origins: list[str] = Field(default_factory=list, max_length=20)
    redirect_origins: list[str] = Field(default_factory=list, max_length=20)
    rate_limit_per_minute: int = Field(default=120, ge=1, le=10000)
    expires_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def scopes_known(cls, value):
        if not set(value) <= SCOPES or len(set(value)) != len(value):
            raise ValueError("Unsupported or duplicated scope")
        return value

    @field_validator("embed_origins", "redirect_origins")
    @classmethod
    def origins_valid(cls, values):
        return list(dict.fromkeys(validate_origin(value) for value in values))

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value):
        if value and value.tzinfo is None:
            raise ValueError("Timezone required")
        return value


class DelegatedUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=256)
    phone: str = Field(min_length=5, max_length=50)
    asserted_at: int = Field(ge=1)


class LoginLinkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user: DelegatedUser
    redirect_uri: str = Field(min_length=1, max_length=2048)
    embed_origin: str | None = Field(default=None, max_length=500)

    @field_validator("embed_origin")
    @classmethod
    def optional_origin(cls, value):
        return validate_origin(value) if value else None


class LoginExchangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=20, max_length=4096)


class EmployeeAccessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user: DelegatedUser


class EmployeeSearchInput(EmployeeAccessInput):
    search: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1, le=10000)
    page_size: int = Field(default=20, ge=1, le=100)


class EmployeeOut(BaseModel):
    id: UUID
    name: str
    avatar_url: str | None
    description: str
    access_url: str


class EmployeePageOut(BaseModel):
    items: list[EmployeeOut]
    total: int
    page: int
    page_size: int
    has_more: bool


class OAuthTokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"]
    expires_in: int
    scope: str


class LoginLinkOut(BaseModel):
    login_url: str
    expires_in: int


class CapabilitiesOut(BaseModel):
    protocol_version: Literal[1]
    scopes: list[str]
    trust_user_identity: bool
