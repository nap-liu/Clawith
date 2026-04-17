"""Tests for the egress allowlist — schema validation + env pass-through.

Current implementation is phase 1: the list is validated at schema time
and forwarded to the sandbox as the ``CLAWITH_EGRESS_ALLOWLIST`` env
var. There is no kernel-level enforcement yet (see
``docs/superpowers/TODO-egress-enforcement.md``). These tests pin down
the parts we DO commit to:

- empty list preserves pre-feature behaviour (no env var injected)
- schema refuses anything that isn't a lowercase hostname
- populated list surfaces to the runner via env — no silent drops
- the env-var name is stable (downstream CLIs depend on it)
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.services.cli_tool_executor import execute_cli_tool
from app.services.cli_tools.schema import CliToolConfig, SandboxConfig
from app.services.sandbox.local.binary_runner import BinaryRunResult


# ─── helpers (match the style in test_cli_tool_executor_v2.py) ──────────


def _tool(*, tenant_id, config, parameters_schema=None, enabled=True):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.tenant_id = tenant_id
    t.enabled = enabled
    t.config = config
    t.parameters_schema = parameters_schema or {}
    return t


def _agent(tenant_id):
    a = MagicMock()
    a.id = uuid.uuid4()
    a.tenant_id = tenant_id
    return a


def _mock_storage() -> MagicMock:
    storage = MagicMock()
    path = MagicMock()
    path.is_file.return_value = True
    storage.resolve.return_value = path
    return storage


def _mock_runner() -> MagicMock:
    """Stub runner that records the run() kwargs so we can inspect env."""
    runner = MagicMock()
    runner.default_image = "default-image"
    runner.run = AsyncMock(return_value=BinaryRunResult(
        exit_code=0, stdout="ok", stderr="", duration_ms=1,
    ))
    return runner


# ─── schema-level tests ────────────────────────────────────────────────


def test_empty_allowlist_means_all_allowed():
    """Default behaviour: no allowlist → no env var injected.

    This is the "existing behavior" case: everyone who already set
    network=True keeps unrestricted egress after this feature ships.
    """
    cfg = SandboxConfig()
    assert cfg.egress_allowlist == []

    # Round-trip via the parent schema to confirm the default survives
    # JSON serialisation (add-only schema rule).
    parent = CliToolConfig.model_validate({"sandbox": {"network": True}})
    assert parent.sandbox.egress_allowlist == []


def test_allowlist_accepts_valid_hostnames():
    cfg = SandboxConfig.model_validate({
        "network": True,
        "egress_allowlist": ["api.example.com", "registry-1.example.com", "a.b"],
    })
    assert cfg.egress_allowlist == [
        "api.example.com", "registry-1.example.com", "a.b",
    ]


@pytest.mark.parametrize("bad", [
    # Shell/format-string injection attempts — all must raise.
    "api.example.com;rm -rf /",
    "api.example.com rm",           # space
    "api.example.com\nexample.org", # newline injection
    "Api.Example.com",              # uppercase — IDN/punycode is the caller's job
    "api_example.com",              # underscore isn't hostname-legal
    "api.example.com/",             # path component leaking in
    "",                             # empty string
    "   ",                          # whitespace only
    " api.example.com",             # leading space
    "api.example.com ",             # trailing space
    "api.example.com\x00",          # NUL injection
])
def test_allowlist_rejects_invalid_host_chars(bad):
    """Any entry that isn't strict [a-z0-9.-]+ must be rejected.

    The value eventually flows into env vars / (phase-2) rule files; a
    single permissive character opens rule / env-var injection attacks.
    """
    with pytest.raises(ValidationError):
        SandboxConfig.model_validate({
            "network": True,
            "egress_allowlist": [bad],
        })


def test_allowlist_rejects_non_string_entries():
    with pytest.raises(ValidationError):
        SandboxConfig.model_validate({
            "network": True,
            "egress_allowlist": [123],  # type: ignore[list-item]
        })


# ─── executor-level tests (pass-through behaviour) ─────────────────────


@pytest.mark.asyncio
async def test_allowlist_passed_to_sandbox_env():
    """Non-empty allowlist → CLAWITH_EGRESS_ALLOWLIST reaches the runner."""
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={
            "binary_sha256": "a" * 64,
            "sandbox": {
                "network": True,
                "egress_allowlist": ["api.example.com", "registry.example.com"],
            },
        },
    )
    agent = _agent(tenant)
    runner = _mock_runner()

    await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(), runner=runner,
    )

    env = runner.run.call_args.kwargs["env"]
    assert "CLAWITH_EGRESS_ALLOWLIST" in env
    # Comma-separated, preserves order, no whitespace around items — the
    # downstream CLI parses with a plain `.split(",")`.
    assert env["CLAWITH_EGRESS_ALLOWLIST"] == "api.example.com,registry.example.com"


@pytest.mark.asyncio
async def test_empty_allowlist_does_not_inject_env_var():
    """Absent allowlist → the env var is NOT set.

    This matters for tools that legitimately want to see "no variable" as
    the "all hosts permitted" signal, rather than having to handle an
    empty-string case.
    """
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={
            "binary_sha256": "a" * 64,
            "sandbox": {"network": True},  # no egress_allowlist key
        },
    )
    agent = _agent(tenant)
    runner = _mock_runner()

    await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(), runner=runner,
    )

    env = runner.run.call_args.kwargs["env"]
    assert "CLAWITH_EGRESS_ALLOWLIST" not in env


@pytest.mark.asyncio
async def test_allowlist_env_key_name_stable():
    """The public contract is the env-var name ``CLAWITH_EGRESS_ALLOWLIST``.

    Downstream CLIs (our shipped wrappers around httpx / requests) read
    it by that exact name. Renaming or dropping the CLAWITH_ prefix is a
    breaking change for every tool in the field.
    """
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={
            "binary_sha256": "a" * 64,
            "sandbox": {
                "network": True,
                "egress_allowlist": ["example.com"],
            },
        },
    )
    agent = _agent(tenant)
    runner = _mock_runner()

    await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(), runner=runner,
    )

    env = runner.run.call_args.kwargs["env"]
    # Key is spelled exactly like this. Do not "simplify" to EGRESS_ALLOWLIST
    # without a coordinated CLI-side rollout.
    assert "CLAWITH_EGRESS_ALLOWLIST" in env
    assert not any(
        k != "CLAWITH_EGRESS_ALLOWLIST" and k.endswith("EGRESS_ALLOWLIST")
        for k in env
    ), "no alternate spellings of the egress env var"
