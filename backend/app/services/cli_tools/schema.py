"""Pydantic schema for the CLI-tool shape stored in Tool.config (v5).

v5 model — a CLI tool is just "a binary + the env it needs":

- ``BinaryMetadata`` — system-owned. Written **only** by the binary upload
  endpoint (POST /tools/cli/{id}/binary). Admins must not be able to set
  these via PATCH.
- ``env`` — the only admin-editable field. Values support placeholders
  resolved at injection time: $user.id / $user.phone / $user.email /
  $agent.id / $tenant.id / $state.dir (per-(tenant,tool,user) persistent
  state directory; the entry is skipped when no user is in context).

Execution model: the binary is exposed as a shell function inside the
agent's aio-sandbox session (see services/cli_tools/sandbox_inject.py).
There is no subprocess runner, no argv template, no per-call rate limit
or resource caps any more — those were subprocess-era concepts.

On read, legacy shapes (three-layer nested / M2 flat / M1 flat) load
without migration: ``runtime.env_inject`` (or flat ``env_inject``) lifts
into ``env``; every other runtime/sandbox knob is silently dropped.
Dump always produces the v5 shape, so the next write cleans legacy rows.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BinaryMetadata(BaseModel):
    """System-written binary metadata (upload endpoint only)."""

    model_config = ConfigDict(extra="forbid")

    sha256: Optional[str] = None
    size: Optional[int] = Field(default=None, ge=0)
    original_name: Optional[str] = None
    uploaded_at: Optional[datetime] = None

    @field_validator("sha256")
    @classmethod
    def _check_sha(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _SHA256_RE.match(v):
            raise ValueError("sha256 must be 64 lower-case hex chars")
        return v


# Legacy M2 flat binary keys → nested names under ``binary``.
_BINARY_FLAT_MAP = {
    "binary_sha256": "sha256",
    "binary_size": "size",
    "binary_original_name": "original_name",
    "binary_uploaded_at": "uploaded_at",
}


class CliToolConfig(BaseModel):
    """Shape of Tool.config when Tool.type == 'cli' (v5: binary + env)."""

    model_config = ConfigDict(extra="ignore")

    binary: BinaryMetadata = Field(default_factory=BinaryMetadata)
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_shapes(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        src: dict[str, Any] = dict(data)

        def _to_subdict(value: Any) -> dict[str, Any]:
            if isinstance(value, dict):
                return dict(value)
            if isinstance(value, BaseModel):
                return value.model_dump()
            return {}

        binary_sub = _to_subdict(src.pop("binary", None))
        # Record whether caller explicitly provided an "env" key (even if empty)
        # before popping it — an explicit env={} must suppress legacy env_inject.
        has_env = isinstance(data.get("env"), dict)
        env_sub: dict[str, Any] = _to_subdict(src.pop("env", None))

        # Legacy nested runtime: only env_inject survives (as env).
        runtime_sub = _to_subdict(src.pop("runtime", None))
        legacy_env = runtime_sub.get("env_inject")
        # Legacy flat env_inject (M2 / post-M2).
        if not legacy_env:
            legacy_env = src.pop("env_inject", None)
        if not has_env and isinstance(legacy_env, dict):
            env_sub = dict(legacy_env)

        # Legacy sandbox subtree: dropped entirely.
        src.pop("sandbox", None)

        # M2 flat binary keys lift into binary (explicit nested wins).
        for flat_key, nested_key in _BINARY_FLAT_MAP.items():
            if flat_key in src:
                value = src.pop(flat_key)
                binary_sub.setdefault(nested_key, value)

        return {"binary": binary_sub, "env": env_sub}
