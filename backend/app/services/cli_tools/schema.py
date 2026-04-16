"""Pydantic schema for the CLI-tool shape stored in Tool.config.

Add-only evolution rule (see spec §11.1): never rename or remove fields.
New fields must have a default that preserves pre-upgrade behaviour.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Hostname charset: lowercase letters, digits, dot, dash. No spaces, no
# control chars, no shell metacharacters — the value flows into the
# sandbox env (`CLAWITH_EGRESS_ALLOWLIST`) and in Phase 2 into a tinyproxy
# config / nftables rule, so we refuse anything that could break either.
# IDN hostnames must be punycoded by the caller before reaching us.
_HOSTNAME_RE = re.compile(r"^[a-z0-9.-]+$")


class SandboxConfig(BaseModel):
    """Per-tool sandbox overrides. `image=None` means follow the platform :stable alias."""

    model_config = ConfigDict(extra="forbid")

    cpu_limit: str = "1.0"
    memory_limit: str = "512m"
    network: bool = False
    readonly_fs: bool = True
    image: Optional[str] = None

    # Which sandbox implementation executes this tool. "docker" is the
    # secure-but-slow default (full container, ~300ms cold start).
    # "bwrap" trades isolation for ~10x faster starts; only enable after
    # the tool author has reviewed the trade-offs in BubblewrapBackend's
    # docstring. New fields must default to the pre-upgrade behaviour —
    # existing configs must keep getting docker.
    backend: Literal["docker", "bwrap"] = "docker"

    egress_allowlist: list[str] = Field(
        default_factory=list,
        description=(
            "Hostnames the sandbox is allowed to reach when network=True. "
            "Empty list + network=True means allow all (existing behavior). "
            "Non-empty list enforces: all other DNS lookups and TCP connect "
            "attempts fail. Applies to the `docker` backend via --dns and a "
            "tinyproxy container; `bwrap` backend uses nftables."
        ),
    )

    @field_validator("egress_allowlist")
    @classmethod
    def _check_allowlist(cls, v: list[str]) -> list[str]:
        """Defence-in-depth: only allow hostname-safe characters.

        Values flow into a process environment variable and (phase 2) into
        a tinyproxy config file and nftables rules. Rejecting anything
        with whitespace, NUL, slashes or shell metacharacters closes off
        the obvious prompt-injection → env-var-smuggling → rule-injection
        chain.
        """
        cleaned: list[str] = []
        for entry in v:
            if not isinstance(entry, str):
                raise ValueError("egress_allowlist entries must be strings")
            # Explicit empty-string / whitespace check before the regex
            # so the error message is useful for operators.
            if not entry or entry != entry.strip():
                raise ValueError(
                    "egress_allowlist entries must be non-empty and not "
                    "contain leading/trailing whitespace"
                )
            if not _HOSTNAME_RE.match(entry):
                raise ValueError(
                    f"egress_allowlist entry {entry!r} must match "
                    f"[a-z0-9.-]+ (lowercase hostname chars only)"
                )
            cleaned.append(entry)
        return cleaned


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

    # Per-(tenant,tool,user) persistent HOME. When true, the sandbox HOME
    # is a rw bind mount surviving across runs — needed for login tokens
    # and caches (svc, gh, kubectl). When false, HOME is /tmp tmpfs and
    # everything is wiped each run. Off by default: most CLIs are
    # stateless and don't deserve disk.
    persistent_home: bool = False

    # Per-(tool, agent, user) sliding-window rate limit. 0 = unlimited.
    # Guards against LLM-driven runaway loops where prompt injection could
    # hammer a downstream service (reports, paid APIs) by calling the same
    # tool thousands of times per minute. Window is hard-coded 60s; making
    # it configurable is a separate PR.
    rate_limit_per_minute: int = Field(default=60, ge=0, le=10000)

    # Soft quota for the persistent HOME directory. When a run would start
    # with usage already above the limit, the executor refuses with
    # VALIDATION_ERROR — the admin must clear the cache before new runs.
    # Only consulted when persistent_home=True. 0 disables the check (use
    # with care: a runaway tool can fill the whole cli_state volume).
    home_quota_mb: int = Field(default=500, ge=0, le=100_000)

    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    @field_validator("binary_sha256")
    @classmethod
    def _check_sha(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _SHA256_RE.match(v):
            raise ValueError("binary_sha256 must be 64 lower-case hex chars")
        return v
