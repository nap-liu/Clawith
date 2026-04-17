"""Schema tests for Tool.config (CLI-tool shape).

Exercises the three-layer model (BinaryMetadata / RuntimeConfig /
SandboxConfig) and the ``model_validator(mode="before")`` adapter that
accepts legacy flat rows from M1/M2/post-M2 so no data migration is
required.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.cli_tools.schema import (
    BinaryMetadata,
    CliToolConfig,
    RuntimeConfig,
    SandboxConfig,
)


# ─────────────────────────────────────────────────────────────────────────
# Defaults / new nested shape
# ─────────────────────────────────────────────────────────────────────────


def test_cli_tool_config_minimal_defaults():
    """Empty dict round-trips to all-defaults nested shape."""
    cfg = CliToolConfig.model_validate({})
    assert cfg.binary.sha256 is None
    assert cfg.binary.size is None
    assert cfg.binary.original_name is None
    assert cfg.binary.uploaded_at is None
    assert cfg.runtime.args_template == []
    assert cfg.runtime.env_inject == {}
    assert cfg.runtime.timeout_seconds == 30
    assert cfg.runtime.persistent_home is False
    # Post-M2 defaults (rate limit, home quota) must be stable.
    assert cfg.runtime.rate_limit_per_minute == 60
    assert cfg.runtime.home_quota_mb == 500
    assert cfg.sandbox.cpu_limit == "1.0"
    assert cfg.sandbox.memory_limit == "512m"
    # v4 dropped backend/network/readonly_fs/image/egress_allowlist —
    # only cpu_limit and memory_limit remain on SandboxConfig.
    assert not hasattr(cfg.sandbox, "network")
    assert not hasattr(cfg.sandbox, "backend")
    assert not hasattr(cfg.sandbox, "egress_allowlist")


def test_cli_tool_config_dump_is_always_nested():
    """Regardless of input shape, dump must produce the new 3-key nested shape."""
    cfg = CliToolConfig.model_validate({})
    dumped = cfg.model_dump(mode="json")
    assert set(dumped.keys()) == {"binary", "runtime", "sandbox"}
    assert set(dumped["binary"].keys()) == {"sha256", "size", "original_name", "uploaded_at"}
    assert "args_template" in dumped["runtime"]
    assert "timeout_seconds" in dumped["runtime"]
    assert "persistent_home" in dumped["runtime"]
    assert "rate_limit_per_minute" in dumped["runtime"]
    assert "home_quota_mb" in dumped["runtime"]


def test_cli_tool_config_accepts_new_nested_shape():
    """The primary input shape flows through unchanged.

    Legacy sandbox keys (backend/network/readonly_fs/image/egress_allowlist)
    are silently dropped — see test_cli_tools_schema_legacy_sandbox.py for
    the dedicated legacy-compat coverage.
    """
    raw = {
        "binary": {
            "sha256": "a" * 64,
            "size": 1024,
            "original_name": "svc",
            "uploaded_at": "2026-01-01T00:00:00+00:00",
        },
        "runtime": {
            "args_template": ["$user.id"],
            "env_inject": {"K": "v"},
            "timeout_seconds": 120,
            "persistent_home": True,
            "rate_limit_per_minute": 90,
            "home_quota_mb": 1024,
        },
        "sandbox": {
            "cpu_limit": "0.5",
            "memory_limit": "256m",
        },
    }
    cfg = CliToolConfig.model_validate(raw)
    assert cfg.binary.sha256 == "a" * 64
    assert cfg.runtime.args_template == ["$user.id"]
    assert cfg.runtime.persistent_home is True
    assert cfg.runtime.rate_limit_per_minute == 90
    assert cfg.runtime.home_quota_mb == 1024
    assert cfg.sandbox.cpu_limit == "0.5"
    assert cfg.sandbox.memory_limit == "256m"


# ─────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────


def test_binary_metadata_rejects_bad_sha():
    with pytest.raises(ValidationError):
        BinaryMetadata.model_validate({"sha256": "not-hex"})


def test_runtime_rejects_negative_timeout():
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({"timeout_seconds": 0})


def test_runtime_rejects_oversize_timeout():
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({"timeout_seconds": 10_000})


def test_runtime_rejects_oversize_rate_limit():
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({"rate_limit_per_minute": 99_999})


def test_runtime_rejects_negative_home_quota():
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({"home_quota_mb": -1})


def test_sandbox_config_rejects_unknown_fields():
    """extra='forbid' catches accidental typos (add-only schema rule)."""
    with pytest.raises(ValidationError):
        SandboxConfig.model_validate({"cpu_limit": "1.0", "typo_field": "oops"})


def test_runtime_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({"args_template": [], "typo_field": "oops"})


def test_binary_metadata_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        BinaryMetadata.model_validate({"sha256": None, "surprise": 1})


# ─────────────────────────────────────────────────────────────────────────
# Legacy flat shape adapters
# ─────────────────────────────────────────────────────────────────────────


def test_accepts_m2_flat_shape():
    """M2 rows stored everything at the top level (binary_sha256,
    args_template, env_inject, timeout_seconds, persistent_home). The
    validator lifts each key into its correct subtree.
    """
    flat = {
        "binary_sha256": "a" * 64,
        "binary_size": 1024,
        "binary_original_name": "svc",
        "binary_uploaded_at": "2026-01-01T00:00:00+00:00",
        "args_template": ["$user.id", "--flag"],
        "env_inject": {"SVC_USER_PHONE": "$user.phone"},
        "timeout_seconds": 45,
        "persistent_home": True,
        "sandbox": {
            "cpu_limit": "0.5",
            "memory_limit": "256m",
            # Legacy fields (network/readonly_fs/image) — silently dropped on read.
            "network": True,
            "readonly_fs": True,
            "image": None,
        },
    }
    cfg = CliToolConfig.model_validate(flat)
    assert cfg.binary.sha256 == "a" * 64
    assert cfg.binary.size == 1024
    assert cfg.binary.original_name == "svc"
    assert cfg.binary.uploaded_at is not None
    assert cfg.runtime.args_template == ["$user.id", "--flag"]
    assert cfg.runtime.env_inject == {"SVC_USER_PHONE": "$user.phone"}
    assert cfg.runtime.timeout_seconds == 45
    assert cfg.runtime.persistent_home is True
    assert cfg.sandbox.cpu_limit == "0.5"
    assert cfg.sandbox.memory_limit == "256m"

    # Dump produces only the new nested keys — no leftover flat keys.
    dumped = cfg.model_dump(mode="json")
    assert set(dumped.keys()) == {"binary", "runtime", "sandbox"}
    assert "binary_sha256" not in dumped
    assert "args_template" not in dumped
    assert "timeout_seconds" not in dumped


def test_accepts_post_m2_flat_shape_with_rate_limit_and_quota():
    """Post-M2 flat rows add `rate_limit_per_minute` and `home_quota_mb`
    at the top level. Both must lift into `runtime` alongside the M2
    fields. This is the specific migration path for the dda8c9e baseline.
    """
    flat = {
        "binary_sha256": "a" * 64,
        "args_template": ["$user.id"],
        "env_inject": {"K": "v"},
        "timeout_seconds": 30,
        "persistent_home": True,
        "rate_limit_per_minute": 120,
        "home_quota_mb": 2048,
        "sandbox": {
            "cpu_limit": "1.0",
            "memory_limit": "512m",
            # v4-dropped legacy keys — still accepted on read so rows load.
            "network": True,
            "readonly_fs": True,
            "image": None,
            "backend": "bwrap",
            "egress_allowlist": ["api.example.com"],
        },
    }
    cfg = CliToolConfig.model_validate(flat)
    assert cfg.binary.sha256 == "a" * 64
    assert cfg.runtime.rate_limit_per_minute == 120
    assert cfg.runtime.home_quota_mb == 2048
    assert cfg.runtime.persistent_home is True
    # Legacy sandbox fields dropped on read.
    assert not hasattr(cfg.sandbox, "backend")
    assert not hasattr(cfg.sandbox, "egress_allowlist")

    dumped = cfg.model_dump(mode="json")
    assert "rate_limit_per_minute" not in dumped
    assert "home_quota_mb" not in dumped
    assert dumped["runtime"]["rate_limit_per_minute"] == 120
    assert dumped["runtime"]["home_quota_mb"] == 2048


def test_accepts_m1_pre_upload_flat_shape():
    """M1 rows predate the content-addressed upload pipeline. They
    stored ``binary`` as a hardcoded host path string and ``timeout``
    as an int. Both legacy keys are dropped; binary metadata stays
    empty so the executor refuses the tool until someone uploads.
    """
    legacy = {
        "binary": "/usr/local/bin/svc",
        "timeout": 30,
        "env_inject": {"SVC_USER_PHONE": "$user.phone"},
    }
    cfg = CliToolConfig.model_validate(legacy)
    # No real binary recorded — reading must not invent a sha.
    assert cfg.binary.sha256 is None
    assert cfg.binary.size is None
    # M1 env_inject was a top-level key — lift it into runtime.
    assert cfg.runtime.env_inject == {"SVC_USER_PHONE": "$user.phone"}
    # Legacy ``timeout`` is discarded; runtime keeps the default.
    assert cfg.runtime.timeout_seconds == 30

    dumped = cfg.model_dump(mode="json")
    assert "binary" in dumped and isinstance(dumped["binary"], dict)
    assert dumped["binary"]["sha256"] is None
    assert "timeout" not in dumped
    # The top-level legacy `binary` string must not survive the round-trip.
    assert dumped["binary"] != "/usr/local/bin/svc"


def test_mixed_shape_nested_wins_over_flat():
    """If both a nested ``binary`` dict and a flat ``binary_sha256`` key
    are present (shouldn't happen, but be defensive), the nested dict
    wins because it's the newer, explicit form."""
    mixed = {
        "binary": {"sha256": "b" * 64},
        "binary_sha256": "a" * 64,  # ignored
    }
    cfg = CliToolConfig.model_validate(mixed)
    assert cfg.binary.sha256 == "b" * 64


def test_missing_subtrees_get_defaults():
    """A partial payload with only ``binary`` fills ``runtime``/``sandbox``
    from defaults — add-only evolution rule."""
    partial = {"binary": {"sha256": "c" * 64}}
    cfg = CliToolConfig.model_validate(partial)
    assert cfg.binary.sha256 == "c" * 64
    assert cfg.runtime.timeout_seconds == 30
    assert cfg.sandbox.cpu_limit == "1.0"


def test_unknown_top_level_keys_are_dropped():
    """Belt-and-suspenders: extra='ignore' on the outer model swallows
    unknown legacy keys we never explicitly handle."""
    cfg = CliToolConfig.model_validate({"something_old": "x", "binary": {"sha256": "d" * 64}})
    assert cfg.binary.sha256 == "d" * 64
    assert "something_old" not in cfg.model_dump(mode="json")
