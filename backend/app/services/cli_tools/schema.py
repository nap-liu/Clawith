"""Pydantic schema for the CLI-tool shape stored in Tool.config.

Three-layer model (security-driven split):

- ``BinaryMetadata`` — system-owned. Written **only** by the binary upload
  endpoint (POST /tools/cli/{id}/binary). Admins must not be able to set
  these via PATCH; doing so would let a malicious admin point the sandbox
  at the wrong on-disk blob.
- ``RuntimeConfig`` — admin-editable runtime policy (argv template, env
  vars, timeout, persistent HOME toggle, rate limit, HOME quota).
- ``SandboxConfig`` — admin-editable sandbox policy (cpu/memory/network/
  readonly_fs/image, sandbox backend, egress allowlist).

Add-only evolution rule (see spec §11.1): never rename or remove fields.
New fields must have a default that preserves pre-upgrade behaviour.

On read, ``CliToolConfig`` accepts four historical shapes so that
existing DB rows load without migration:

1. New nested shape — ``{"binary": {...}, "runtime": {...}, "sandbox": {...}}``
2. M2 flat shape — ``{"binary_sha256": ..., "args_template": ...,
   "env_inject": ..., "timeout_seconds": ..., "persistent_home": ...,
   "sandbox": {...}}``
3. Post-M2 flat shape — adds ``rate_limit_per_minute`` /
   ``home_quota_mb`` at the top level. These lift into ``runtime``.
4. M1 pre-upload flat shape — ``{"binary": "/usr/local/bin/svc",
   "timeout": 30, "env_inject": {...}}``. The legacy ``binary`` path and
   ``timeout`` int are dropped; binary metadata is left empty so the
   executor refuses the tool until someone uploads a real binary.

Dump always produces the new nested shape, so the next write cleans up
legacy rows naturally.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Hostname charset: lowercase letters, digits, dot, dash. No spaces, no
# control chars, no shell metacharacters — the value flows into the
# sandbox env (`CLAWITH_EGRESS_ALLOWLIST`) and in Phase 2 into a tinyproxy
# config / nftables rule, so we refuse anything that could break either.
# IDN hostnames must be punycoded by the caller before reaching us.
_HOSTNAME_RE = re.compile(r"^[a-z0-9.-]+$")


class BinaryMetadata(BaseModel):
    """System-written binary metadata.

    Populated exclusively by the binary upload endpoint. All fields are
    Optional so a freshly-created tool (no binary uploaded yet) still
    validates. Admin PATCH bodies never reach this class — ``CliToolUpdate``
    rejects any incoming ``binary`` key with ``extra="forbid"``.
    """

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


class RuntimeConfig(BaseModel):
    """Admin-editable runtime policy."""

    model_config = ConfigDict(extra="forbid")

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


# Fields that historically lived at the top level of Tool.config (M2 and
# post-M2) but now belong under ``runtime``. Used by the legacy-flat
# adapter below.
_RUNTIME_FLAT_KEYS = frozenset({
    "args_template",
    "env_inject",
    "timeout_seconds",
    "persistent_home",
    "rate_limit_per_minute",
    "home_quota_mb",
})

# Legacy M2 flat binary keys → new nested names under ``binary``.
_BINARY_FLAT_MAP = {
    "binary_sha256": "sha256",
    "binary_size": "size",
    "binary_original_name": "original_name",
    "binary_uploaded_at": "uploaded_at",
}


class CliToolConfig(BaseModel):
    """Shape of Tool.config when Tool.type == 'cli'.

    See module docstring for the accepted input shapes and the single
    output shape.
    """

    # extra=ignore drops truly unknown legacy keys (e.g. M1 ``binary``
    # string path, M1 ``timeout`` int) silently — reading old rows must
    # never fail. The model_validator below also actively strips them
    # before validation so the dump stays clean.
    model_config = ConfigDict(extra="ignore")

    binary: BinaryMetadata = Field(default_factory=BinaryMetadata)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_shapes(cls, data: Any) -> Any:
        """Normalise M1 flat, M2 flat, post-M2 flat and new nested payloads.

        Rules:
          - New nested (has one of ``binary``/``runtime``/``sandbox`` as
            a dict or pydantic model): keep as-is but still lift any
            stray flat M2/post-M2 keys into their subtree so mixed shapes
            survive.
          - M1 ``binary`` as a string (hardcoded host path): drop it.
          - M1 ``timeout`` int: drop it (superseded by
            ``runtime.timeout_seconds``).
          - M2 flat binary keys: move them under ``binary``.
          - M2 / post-M2 flat runtime keys (args_template, env_inject,
            timeout_seconds, persistent_home, rate_limit_per_minute,
            home_quota_mb): move them under ``runtime``.
        """
        if not isinstance(data, dict):
            return data

        # Copy so we don't mutate caller input.
        src: dict[str, Any] = dict(data)

        # Pre-existing subtrees (may be absent). Start fresh dicts; we'll
        # merge legacy flat keys into them below. A caller may supply
        # either a plain dict or an already-constructed pydantic model
        # instance (happens when code calls ``CliToolConfig(binary=
        # BinaryMetadata(...), ...)`` directly); both must be preserved.
        def _to_subdict(value: Any) -> dict[str, Any]:
            if isinstance(value, dict):
                return dict(value)
            if isinstance(value, BaseModel):
                return value.model_dump()
            # Anything else (e.g. M1 ``binary`` as string path, or None):
            # treat as "no subtree supplied".
            return {}

        binary_sub = _to_subdict(src.pop("binary", None))
        runtime_sub = _to_subdict(src.pop("runtime", None))
        sandbox_sub = _to_subdict(src.pop("sandbox", None))

        # Lift M2 flat binary keys into the binary subtree (only if the
        # subtree doesn't already define them — explicit nested wins).
        for flat_key, nested_key in _BINARY_FLAT_MAP.items():
            if flat_key in src:
                value = src.pop(flat_key)
                binary_sub.setdefault(nested_key, value)

        # Lift M2 / post-M2 flat runtime keys into the runtime subtree.
        for flat_key in _RUNTIME_FLAT_KEYS:
            if flat_key in src:
                value = src.pop(flat_key)
                runtime_sub.setdefault(flat_key, value)

        # Explicitly drop known M1 leftovers so nothing surprising
        # survives into the normalised payload.
        src.pop("timeout", None)

        return {
            "binary": binary_sub,
            "runtime": runtime_sub,
            "sandbox": sandbox_sub,
        }
