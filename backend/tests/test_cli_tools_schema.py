"""Schema tests for Tool.config (CLI-tool shape)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.cli_tools.schema import CliToolConfig, SandboxConfig


def test_cli_tool_config_minimal():
    cfg = CliToolConfig()
    assert cfg.binary_sha256 is None
    assert cfg.args_template == []
    assert cfg.env_inject == {}
    assert cfg.timeout_seconds == 30
    assert cfg.sandbox.cpu_limit == "1.0"
    assert cfg.sandbox.memory_limit == "512m"
    assert cfg.sandbox.network is False
    assert cfg.sandbox.image is None  # follow :stable


def test_cli_tool_config_round_trip_via_dict():
    raw = {
        "binary_sha256": "a" * 64,
        "binary_size": 1024,
        "binary_original_name": "my-tool",
        "args_template": ["--user", "{user.id}"],
        "env_inject": {"KEY": "value"},
        "timeout_seconds": 60,
        "sandbox": {
            "cpu_limit": "0.5",
            "memory_limit": "256m",
            "network": True,
            "image": "pinned-tag",
        },
    }
    cfg = CliToolConfig.model_validate(raw)
    dumped = cfg.model_dump(mode="json")
    assert dumped["binary_sha256"] == "a" * 64
    assert dumped["args_template"] == ["--user", "{user.id}"]
    assert dumped["env_inject"] == {"KEY": "value"}
    assert dumped["timeout_seconds"] == 60
    assert dumped["sandbox"]["network"] is True
    assert dumped["sandbox"]["image"] == "pinned-tag"
    # readonly_fs defaults to True — preserved even when not in the raw payload.
    assert dumped["sandbox"]["readonly_fs"] is True
    # binary_uploaded_at defaults to None when omitted.
    assert dumped["binary_uploaded_at"] is None


def test_cli_tool_config_rejects_bad_sha():
    with pytest.raises(ValidationError):
        CliToolConfig.model_validate({"binary_sha256": "not-hex"})


def test_cli_tool_config_rejects_negative_timeout():
    with pytest.raises(ValidationError):
        CliToolConfig.model_validate({"timeout_seconds": 0})


def test_sandbox_config_rejects_unknown_fields():
    """extra='forbid' catches accidental typos (add-only schema rule)."""
    with pytest.raises(ValidationError):
        SandboxConfig.model_validate({"cpu_limit": "1.0", "typo_field": "oops"})


def test_cli_tool_config_tolerates_legacy_keys():
    """Pre-M1 rows stored `binary` (hardcoded host path) and `timeout`
    (superseded by `timeout_seconds`). Reading must never break — we drop
    the legacy keys silently and let the next write clean them up.
    """
    legacy = {
        "binary": "/usr/local/bin/svc",
        "timeout": 30,
        "binary_sha256": "a" * 64,
        "env_inject": {"SVC_USER_PHONE": "$user.phone"},
    }
    cfg = CliToolConfig.model_validate(legacy)
    assert cfg.binary_sha256 == "a" * 64
    assert cfg.env_inject == {"SVC_USER_PHONE": "$user.phone"}
    assert cfg.timeout_seconds == 30  # default, untouched by legacy `timeout`
    assert "binary" not in cfg.model_dump()
    assert "timeout" not in cfg.model_dump()
