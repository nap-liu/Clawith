"""Validation contracts for channel-neutral scenes."""

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from app.utils.mini_program_uri import parse_mini_program_uri

SCENE_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class SceneSystemPrompt(BaseModel):
    id: str = Field(max_length=64)
    name: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=12000)
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not ITEM_ID_RE.fullmatch(value):
            raise ValueError("id must contain only letters, numbers, '_' or '-'")
        return value


class SceneQuickAction(BaseModel):
    id: str = Field(max_length=64)
    label: str = Field(min_length=1, max_length=80)
    type: Literal["open_uri", "send_message"]
    enabled: bool = True
    uri: str | None = Field(default=None, max_length=2048)
    message: str | None = Field(default=None, max_length=12000)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not ITEM_ID_RE.fullmatch(value):
            raise ValueError("id must contain only letters, numbers, '_' or '-'")
        return value

    @model_validator(mode="after")
    def validate_payload(self):
        if self.type == "open_uri":
            if not self.uri:
                raise ValueError("open_uri action requires uri")
            validate_scene_uri(self.uri)
        else:
            if not (self.message or "").strip():
                raise ValueError("send_message action requires message")
        return self


class SceneConfig(BaseModel):
    welcome_message: str = Field(default="", max_length=12000)
    system_prompts: list[SceneSystemPrompt] = Field(default_factory=list)
    quick_actions: list[SceneQuickAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self):
        for field_name in ("system_prompts", "quick_actions"):
            ids = [item.id for item in getattr(self, field_name)]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{field_name} contains duplicate ids")
        return self


class SceneSaveRequest(SceneConfig):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    expected_revision: int | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value


class SceneToolSaveRequest(SceneConfig):
    """Patch-oriented save contract used only by the guarded management tool."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool = True
    expected_revision: int | None = Field(default=None, ge=0)
    force_overwrite: bool = False

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value


class SceneRollbackRequest(BaseModel):
    target_revision: int = Field(ge=1)
    expected_revision: int = Field(ge=1)


class ScenePublishRequest(BaseModel):
    expected_revision: int = Field(ge=0)


def validate_scene_key(value: str) -> str:
    value = (value or "").strip().lower()
    if not SCENE_KEY_RE.fullmatch(value):
        raise ValueError("scene_key must start with a letter and contain only lowercase letters, numbers, '_' or '-'")
    return value


def validate_scene_uri(value: str) -> str:
    if any(ord(char) < 32 for char in value) or "\\" in value:
        raise ValueError("uri contains unsafe characters")
    if value.startswith("/"):
        if value.startswith("//"):
            raise ValueError("protocol-relative uri is not allowed")
        return value

    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    if parsed.scheme == "miniprogram":
        if parse_mini_program_uri(value) is None:
            raise ValueError("miniprogram uri must use miniprogram://navigate-to/<path>")
        return value
    raise ValueError("uri must be a relative path, http(s) URL, or miniprogram://navigate-to URI")
