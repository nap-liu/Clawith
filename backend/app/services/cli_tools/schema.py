"""Pydantic schema for the CLI-tool shape stored in Tool.config.

Add-only evolution rule (see spec §11.1): never rename or remove fields.
New fields must have a default that preserves pre-upgrade behaviour.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SandboxConfig(BaseModel):
    """Per-tool sandbox overrides. `image=None` means follow the platform :stable alias."""

    model_config = ConfigDict(extra="forbid")

    cpu_limit: str = "1.0"
    memory_limit: str = "512m"
    network: bool = False
    readonly_fs: bool = True
    image: Optional[str] = None


class CliToolConfig(BaseModel):
    """Shape of Tool.config when Tool.type == 'cli'.

    Legacy pre-M1 rows carry extra keys like `binary` (hardcoded host path)
    and `timeout` (superseded by `timeout_seconds`). We ignore them instead
    of failing validation — reading them as CliToolConfig must never break
    — and drop them on the next write via `model_dump(exclude_unset=False)`.
    """

    model_config = ConfigDict(extra="ignore")

    binary_sha256: Optional[str] = None
    binary_size: Optional[int] = Field(default=None, ge=0)
    binary_original_name: Optional[str] = None
    binary_uploaded_at: Optional[datetime] = None

    args_template: list[str] = Field(default_factory=list)
    env_inject: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, gt=0, le=600)

    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    @field_validator("binary_sha256")
    @classmethod
    def _check_sha(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _SHA256_RE.match(v):
            raise ValueError("binary_sha256 must be 64 lower-case hex chars")
        return v
