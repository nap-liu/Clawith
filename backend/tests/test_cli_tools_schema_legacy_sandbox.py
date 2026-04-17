"""Legacy SandboxConfig rows must still load after the v4 field removal."""

from __future__ import annotations

import pytest

from app.services.cli_tools.schema import CliToolConfig


def test_legacy_row_with_backend_field_loads():
    raw = {
        "binary": {
            "sha256": "a" * 64,
            "size": 10,
            "original_name": "svc",
            "uploaded_at": "2026-04-16T10:00:00Z",
        },
        "runtime": {"args_template": [], "env_inject": {}, "timeout_seconds": 30},
        "sandbox": {
            "backend": "docker",
            "cpu_limit": "0.5",
            "memory_limit": "256m",
            "network": False,
            "readonly_fs": True,
            "image": "clawith-cli-sandbox:stable",
            "egress_allowlist": ["example.com"],
        },
    }
    cfg = CliToolConfig.model_validate(raw)
    assert cfg.sandbox.cpu_limit == "0.5"
    assert cfg.sandbox.memory_limit == "256m"
    # Legacy fields dropped:
    assert not hasattr(cfg.sandbox, "backend")
    assert not hasattr(cfg.sandbox, "network")
    assert not hasattr(cfg.sandbox, "egress_allowlist")


def test_round_trip_dumps_only_kept_fields():
    raw = {"sandbox": {"backend": "bwrap"}}
    cfg = CliToolConfig.model_validate(raw)
    dumped = cfg.model_dump()
    assert dumped["sandbox"] == {"cpu_limit": "1.0", "memory_limit": "512m"}


def test_typo_on_new_write_still_errors():
    with pytest.raises(Exception):  # pydantic ValidationError
        CliToolConfig.model_validate({"sandbox": {"cpu_limnit": "1.0"}})
