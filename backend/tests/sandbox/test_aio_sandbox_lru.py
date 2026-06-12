"""Per-agent LRU of conversation anchors in AioSandboxBackend.

Per-session isolation means anchors (and their sandbox-side shell sessions /
jupyter kernels) grow with conversations. The backend caps live conversation
anchors per agent: registering a new one beyond the cap evicts the least
recently used, deleting its sandbox sessions best-effort.

Agent-level fallback anchors (no conversation) are one-per-agent and exempt.
"""
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.sandbox.base import ExecutionResult
from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend

_OK = ExecutionResult(
    success=True, stdout="", stderr="", exit_code=0, duration_ms=0, error=None
)


def _backend(cap: int = 2) -> AioSandboxBackend:
    cfg = SandboxConfig(
        type=SandboxType.AIO_SANDBOX,
        api_url="http://fake:8080",
        default_timeout=30,
        max_timeout=60,
    )
    b = AioSandboxBackend(cfg)
    b._max_anchors_per_agent = cap
    return b


# --------------------------------------------------------------- bookkeeping


def test_register_returns_no_eviction_under_cap():
    b = _backend(cap=2)
    assert b._register_anchor("A", "A:c1") == []
    assert b._register_anchor("A", "A:c2") == []


def test_register_evicts_least_recently_used_over_cap():
    b = _backend(cap=2)
    b._register_anchor("A", "A:c1")
    b._register_anchor("A", "A:c2")
    assert b._register_anchor("A", "A:c3") == ["A:c1"]


def test_retouching_anchor_refreshes_recency():
    b = _backend(cap=2)
    b._register_anchor("A", "A:c1")
    b._register_anchor("A", "A:c2")
    b._register_anchor("A", "A:c1")  # c1 becomes most recent
    assert b._register_anchor("A", "A:c3") == ["A:c2"]


def test_agents_have_independent_budgets():
    b = _backend(cap=2)
    b._register_anchor("A", "A:c1")
    b._register_anchor("A", "A:c2")
    assert b._register_anchor("B", "B:c1") == []


def test_agent_level_anchor_is_not_tracked():
    b = _backend(cap=2)
    b._register_anchor("A", "A")  # fallback anchor: exempt from LRU
    b._register_anchor("A", "A:c1")
    b._register_anchor("A", "A:c2")
    # The fallback anchor neither counts toward nor is ever evicted.
    assert b._register_anchor("A", "A:c3") == ["A:c1"]


# --------------------------------------------------------------- eviction IO


async def test_evict_anchor_deletes_shell_session_and_jupyter_kernel():
    b = _backend()
    b._jupyter_sessions["A:c1"] = "uuid-1"
    client = MagicMock()
    client.delete = AsyncMock()

    await b._evict_anchor(client, "A:c1")

    deleted = [c.args[0] for c in client.delete.await_args_list]
    assert "http://fake:8080/v1/shell/sessions/clawith-A:c1" in deleted
    assert "http://fake:8080/v1/jupyter/sessions/uuid-1" in deleted
    assert "A:c1" not in b._jupyter_sessions


async def test_evict_anchor_cleans_per_conversation_wrapper_dir():
    """Eviction best-effort removes the anchor's wrapper dir so a prior sender's
    cleartext identity wrapper doesn't linger on disk (audit hardening)."""
    b = _backend()
    sent = []

    async def fake_exec(client, sid, cmd, timeout):
        sent.append((sid, cmd))
        return {}, True

    b._shell_exec = fake_exec
    client = MagicMock()
    client.delete = AsyncMock()

    await b._evict_anchor(client, "A:c1")

    assert any("rm -rf" in cmd and ".clawith-bin" in cmd for _, cmd in sent), (
        f"expected a wrapper-dir cleanup rm, got {sent!r}"
    )


async def test_evict_anchor_without_kernel_only_deletes_shell():
    b = _backend()
    client = MagicMock()
    client.delete = AsyncMock()

    await b._evict_anchor(client, "A:c1")

    deleted = [c.args[0] for c in client.delete.await_args_list]
    assert deleted == ["http://fake:8080/v1/shell/sessions/clawith-A:c1"]


async def test_evict_anchor_swallows_http_errors():
    b = _backend()
    client = MagicMock()
    client.delete = AsyncMock(side_effect=RuntimeError("boom"))
    await b._evict_anchor(client, "A:c1")  # must not raise


# --------------------------------------------------------------- execute() wiring


async def test_execute_evicts_oldest_conversation_session():
    b = _backend(cap=2)
    b._run_shell = AsyncMock(return_value=_OK)

    client = MagicMock()
    client.delete = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "app.services.sandbox.remote.aio_sandbox_backend.httpx.AsyncClient",
        return_value=cm,
    ):
        for conv in ("c1", "c2", "c3"):
            await b.execute(
                code="echo hi", language="bash", agent_id="A", conversation_id=conv
            )

    deleted = [c.args[0] for c in client.delete.await_args_list]
    assert deleted == ["http://fake:8080/v1/shell/sessions/clawith-A:c1"]


async def test_execute_without_conversation_never_evicts():
    b = _backend(cap=2)
    b._run_shell = AsyncMock(return_value=_OK)

    client = MagicMock()
    client.delete = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "app.services.sandbox.remote.aio_sandbox_backend.httpx.AsyncClient",
        return_value=cm,
    ):
        for _ in range(5):
            await b.execute(code="echo hi", language="bash", agent_id="A")

    client.delete.assert_not_awaited()
