"""CliToolConfig v5 (binary + env) shape tests.

v5 collapses the old three-layer model (binary/runtime/sandbox) into
two concepts: system-written binary metadata + admin-editable env dict.
All runtime/sandbox knobs from the subprocess era are dropped on read.
"""
import pytest
from app.services.cli_tools.schema import BinaryMetadata, CliToolConfig


def test_new_shape_roundtrip():
    cfg = CliToolConfig.model_validate({
        "binary": {"sha256": "a" * 64, "size": 10, "original_name": "svc", "uploaded_at": None},
        "env": {"YYBPC_CLI_USER_PHONE": "$user.phone", "YYBPC_CLI_HOME": "$state.dir"},
    })
    dumped = cfg.model_dump(mode="json")
    assert set(dumped.keys()) == {"binary", "env"}
    assert dumped["env"]["YYBPC_CLI_HOME"] == "$state.dir"
    assert dumped["binary"]["sha256"] == "a" * 64


def test_empty_config_validates():
    cfg = CliToolConfig.model_validate({})
    assert cfg.binary.sha256 is None
    assert cfg.env == {}


def test_legacy_nested_runtime_env_inject_lifts_to_env():
    """Pre-v5 nested rows: runtime.env_inject becomes env; other knobs drop."""
    cfg = CliToolConfig.model_validate({
        "binary": {"sha256": "b" * 64},
        "runtime": {
            "args_template": ["$params.command"],
            "env_inject": {"SVC_USER_PHONE": "$user.phone"},
            "timeout_seconds": 30,
            "persistent_home": True,
            "rate_limit_per_minute": 60,
            "home_quota_mb": 500,
        },
        "sandbox": {"cpu_limit": "1.0", "memory_limit": "32g"},
    })
    assert cfg.env == {"SVC_USER_PHONE": "$user.phone"}
    assert cfg.binary.sha256 == "b" * 64
    dumped = cfg.model_dump(mode="json")
    assert "runtime" not in dumped and "sandbox" not in dumped


def test_legacy_m2_flat_shape():
    """M2 flat rows: binary_* lift into binary, env_inject into env."""
    cfg = CliToolConfig.model_validate({
        "binary_sha256": "c" * 64,
        "binary_size": 5,
        "env_inject": {"K": "v"},
        "args_template": ["x"],
        "timeout_seconds": 10,
    })
    assert cfg.binary.sha256 == "c" * 64
    assert cfg.binary.size == 5
    assert cfg.env == {"K": "v"}


def test_explicit_env_wins_over_legacy_env_inject():
    cfg = CliToolConfig.model_validate({
        "env": {"NEW": "1"},
        "runtime": {"env_inject": {"OLD": "2"}},
    })
    assert cfg.env == {"NEW": "1"}


def test_sha256_validation_rejects_bad_hex():
    with pytest.raises(ValueError):
        BinaryMetadata(sha256="not-hex")
