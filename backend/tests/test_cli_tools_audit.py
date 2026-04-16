"""Audit sink contract for cli_tool execution.

Compliance baseline (spec §5.4, task K-audit)
--------------------------------------------
Every `execute_cli_tool()` call MUST deliver exactly one audit payload
to the caller's `audit_sink`, regardless of whether the run succeeded,
raised a classified `CliToolError`, or blew up with an unexpected
exception. The executor stays DB-free; the DB write happens in the
caller (api/cli_tools.py test-run + services/agent_tools.py dispatch).

Tests here lock in:
    1. Sink is called on every terminal branch (ok, validation_error,
       timeout, binary_failed, sandbox_failed, internal_error).
    2. The outcome label on the payload matches the executor's metrics
       outcome — dashboards and audit must not disagree.
    3. PII defense: rendered argv never appears in plaintext, stdout
       never appears (len only), stderr is tail-limited.
    4. An exception raised by the sink itself never breaks execution.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.cli_tool_executor import (
    CliExecutionAudit,
    execute_cli_tool,
)
from app.services.cli_tools.errors import CliToolError, CliToolErrorClass
from app.services.sandbox.local.binary_runner import BinaryRunResult


# ── Fixtures mirror test_cli_tools_metrics / test_cli_tool_executor_v2 ──

def _tool(*, tenant_id, config=None, name="svc", enabled=True, parameters_schema=None):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.tenant_id = tenant_id
    t.enabled = enabled
    t.name = name
    t.config = config or {"binary_sha256": "a" * 64}
    t.parameters_schema = parameters_schema or {}
    return t


def _agent(tenant_id):
    a = MagicMock()
    a.id = uuid.uuid4()
    a.tenant_id = tenant_id
    return a


def _mock_storage(exists: bool = True) -> MagicMock:
    storage = MagicMock()
    path = MagicMock()
    path.is_file.return_value = exists
    storage.resolve.return_value = path
    return storage


def _mock_runner(result: BinaryRunResult | None = None) -> MagicMock:
    runner = MagicMock()
    runner.default_image = "default-image"
    runner.run = AsyncMock(return_value=result or BinaryRunResult(
        exit_code=0, stdout="ok", stderr="", duration_ms=12,
    ))
    return runner


class _Sink:
    """Recording async callable — captures every delivered audit payload.

    We use a real class (vs AsyncMock) so `isinstance(a, CliExecutionAudit)`
    assertions work without threading Mock spec plumbing.
    """

    def __init__(self) -> None:
        self.calls: list[CliExecutionAudit] = []

    async def __call__(self, audit: CliExecutionAudit) -> None:
        self.calls.append(audit)


# ── Happy path ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_sink_called_on_success():
    """Success → one audit, outcome='ok', exit_code=0, args_hash populated."""
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, name="success_tool",
                 config={"binary_sha256": "a" * 64, "args_template": ["--flag", "$user.id"]})
    agent = _agent(tenant)
    sink = _Sink()

    await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u-ok", "phone": "", "email": ""},
        storage=_mock_storage(),
        runner=_mock_runner(BinaryRunResult(
            exit_code=0, stdout="output-body", stderr="", duration_ms=42,
        )),
        audit_sink=sink,
    )

    assert len(sink.calls) == 1
    audit = sink.calls[0]
    assert isinstance(audit, CliExecutionAudit)
    assert audit.outcome == "ok"
    assert audit.exit_code == 0
    assert audit.duration_ms == 42
    assert audit.tool_name == "success_tool"
    assert audit.tenant_id == str(tenant)
    assert audit.user_id == "u-ok"
    assert audit.agent_id == str(agent.id)
    # args_template had one token → rendered argv has one entry, hash non-empty
    assert audit.args_hash != ""
    assert len(audit.args_hash) == 16  # sha256 hex prefix
    assert audit.args_len >= 1
    # stdout length recorded but body absent everywhere in the payload
    assert audit.stdout_len == len("output-body")


# ── Pre-render failure (no args yet) ────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_sink_called_on_validation_error():
    """Schema failure fires audit before argv is rendered; hash stays empty."""
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant, name="validation_tool",
        parameters_schema={
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
    )
    agent = _agent(tenant)
    sink = _Sink()

    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={"n": "not-int"},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(), runner=_mock_runner(),
            audit_sink=sink,
        )
    assert exc_info.value.error_class is CliToolErrorClass.VALIDATION_ERROR
    assert len(sink.calls) == 1
    audit = sink.calls[0]
    assert audit.outcome == "validation_error"
    # Executor aborts before _render_command runs, so there is no argv
    # to hash. The dataclass deliberately keeps these empty (not "None")
    # so downstream analytics can `WHERE args_hash = ''` to find these.
    assert audit.args_hash == ""
    assert audit.args_len == 0
    assert audit.exit_code is None
    # Runner never ran → stdout/stderr absent
    assert audit.stdout_len == 0
    assert audit.stderr_tail == ""


# ── Runner-reported failure modes ───────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_sink_called_on_timeout():
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, name="timeout_tool")
    agent = _agent(tenant)
    sink = _Sink()

    with pytest.raises(CliToolError):
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(),
            runner=_mock_runner(BinaryRunResult(
                exit_code=-1, stdout="", stderr="partial",
                duration_ms=5000, timed_out=True,
            )),
            audit_sink=sink,
        )

    assert len(sink.calls) == 1
    audit = sink.calls[0]
    assert audit.outcome == "timeout"
    # Runner produced a result, so executor recorded its numbers
    assert audit.exit_code == -1
    assert audit.duration_ms == 5000


@pytest.mark.asyncio
async def test_audit_sink_called_on_binary_failed():
    """Non-zero exit → stderr tail carried for triage."""
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, name="bin_fail_tool")
    agent = _agent(tenant)
    sink = _Sink()

    # Stderr longer than 200 chars to exercise the slice
    long_err = "X" * 500 + "TAIL-MARK"

    with pytest.raises(CliToolError):
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(),
            runner=_mock_runner(BinaryRunResult(
                exit_code=2, stdout="", stderr=long_err, duration_ms=100,
            )),
            audit_sink=sink,
        )

    assert len(sink.calls) == 1
    audit = sink.calls[0]
    assert audit.outcome == "binary_failed"
    assert audit.exit_code == 2
    # Tail is bounded at 200 bytes and contains the tail marker
    assert len(audit.stderr_tail) == 200
    assert audit.stderr_tail.endswith("TAIL-MARK")


@pytest.mark.asyncio
async def test_audit_sink_called_on_sandbox_failed():
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, name="sandbox_fail_tool")
    agent = _agent(tenant)
    sink = _Sink()

    with pytest.raises(CliToolError):
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(),
            runner=_mock_runner(BinaryRunResult(
                exit_code=1, stdout="", stderr="", duration_ms=0,
                sandbox_failed=True, error="image missing",
            )),
            audit_sink=sink,
        )

    assert len(sink.calls) == 1
    assert sink.calls[0].outcome == "sandbox_failed"


@pytest.mark.asyncio
async def test_audit_sink_called_on_internal_error():
    """A non-CliToolError exception still produces one audit row with
    outcome='internal_error' — otherwise executor bugs would erase the
    compliance trail."""
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, name="internal_boom_tool")
    agent = _agent(tenant)
    sink = _Sink()

    runner = MagicMock()
    runner.default_image = "default"
    runner.run = AsyncMock(side_effect=RuntimeError("kaboom"))

    with pytest.raises(RuntimeError):
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(), runner=runner,
            audit_sink=sink,
        )

    assert len(sink.calls) == 1
    assert sink.calls[0].outcome == "internal_error"


# ── Resilience: sink exceptions must not bubble up ──────────────────────

@pytest.mark.asyncio
async def test_audit_sink_exception_does_not_fail_execution():
    """If the audit sink raises, executor still returns the user's result.

    Compliance tolerates a dropped (logged) audit row; breaking the user's
    tool call would be a worse outcome and surface as a new class of bug.
    """
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, name="sink_explodes_tool")
    agent = _agent(tenant)

    async def exploding_sink(_audit):  # pragma: no cover - body always raises
        raise RuntimeError("postgres is down")

    # Success path: return value must match what the runner said.
    result = await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(),
        runner=_mock_runner(BinaryRunResult(
            exit_code=0, stdout="fine", stderr="", duration_ms=1,
        )),
        audit_sink=exploding_sink,
    )
    assert result.exit_code == 0
    assert result.stdout == "fine"


@pytest.mark.asyncio
async def test_audit_sink_exception_does_not_replace_original_error():
    """On a failing run, the user still sees the CliToolError — the sink
    exception is swallowed, not re-raised."""
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, name="dual_fail_tool")
    agent = _agent(tenant)

    async def exploding_sink(_audit):  # pragma: no cover - body always raises
        raise RuntimeError("postgres is down")

    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(),
            runner=_mock_runner(BinaryRunResult(
                exit_code=2, stdout="", stderr="bad", duration_ms=1,
            )),
            audit_sink=exploding_sink,
        )
    # Original error class preserved — NOT masked by the sink's RuntimeError.
    assert exc_info.value.error_class is CliToolErrorClass.BINARY_FAILED


# ── PII defense ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_never_contains_plaintext_args_or_stdout():
    """Hard guarantee: the audit payload carries only a hash of argv and
    only the length of stdout — never the real strings. This test picks
    markers unlikely to appear elsewhere and asserts they are absent from
    every field of the dataclass.
    """
    tenant = uuid.uuid4()
    # Phone-shaped sentinel that would be a PII leak if it appeared in
    # audit. We pass it through user_context → $user.phone in the args
    # template so _render_command inlines it into argv, then confirm it
    # is nowhere in the audit payload.
    PHONE = "13800138000"
    STDOUT = "sensitive-response-payload-abc123"

    tool = _tool(
        tenant_id=tenant, name="pii_tool",
        config={
            "binary_sha256": "a" * 64,
            "args_template": ["--phone", "$user.phone"],
        },
    )
    agent = _agent(tenant)
    sink = _Sink()

    await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u-pii", "phone": PHONE, "email": ""},
        storage=_mock_storage(),
        runner=_mock_runner(BinaryRunResult(
            exit_code=0, stdout=STDOUT, stderr="", duration_ms=1,
        )),
        audit_sink=sink,
    )

    assert len(sink.calls) == 1
    audit = sink.calls[0]
    # Walk every string field and assert neither marker appears.
    haystack = " ".join(
        str(v) for v in (
            audit.tool_id, audit.tool_name, audit.tenant_id,
            audit.agent_id, audit.user_id, audit.args_hash,
            audit.stderr_tail, audit.trace_id,
        ) if v is not None
    )
    assert PHONE not in haystack, "phone leaked into audit payload"
    assert STDOUT not in haystack, "stdout leaked into audit payload"
    # stdout only appears as a length
    assert audit.stdout_len == len(STDOUT)
    # And argv info is only present as a hash (fixed 16 hex chars)
    assert audit.args_hash and len(audit.args_hash) == 16
    int(audit.args_hash, 16)  # hex sanity — raises if not hex


# ── Sink is optional (backward compat) ─────────────────────────────────

@pytest.mark.asyncio
async def test_execute_still_works_without_sink():
    """Callers that don't opt in to auditing must continue to work.

    This guards against a future refactor that accidentally makes
    `audit_sink` required; the executor is used from background scripts
    / tests that have no DB.
    """
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, name="no_sink_tool")
    agent = _agent(tenant)

    result = await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(), runner=_mock_runner(),
    )
    assert result.exit_code == 0
