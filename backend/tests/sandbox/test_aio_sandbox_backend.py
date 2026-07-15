"""Integration smoke tests for AioSandboxBackend.

Requires a running aio-sandbox container reachable at $SANDBOX_API_URL.
Tests are skipped when SANDBOX_API_URL is unset to keep CI green.

To run locally:
    export SANDBOX_API_URL=http://localhost:8091
    pytest backend/tests/sandbox/test_aio_sandbox_backend.py -v
"""
import os
import uuid

import httpx
import pytest

from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend

pytestmark = pytest.mark.skipif(
    not os.environ.get("SANDBOX_API_URL"),
    reason="SANDBOX_API_URL not set; integration tests skipped",
)


@pytest.fixture
def backend() -> AioSandboxBackend:
    cfg = SandboxConfig(
        type=SandboxType.AIO_SANDBOX,
        api_url=os.environ["SANDBOX_API_URL"],
        default_timeout=30,
        max_timeout=60,
    )
    return AioSandboxBackend(cfg)


@pytest.fixture
def agent_id() -> str:
    """Fresh UUID per test execution to prevent cross-test session collisions."""
    return f"test-{uuid.uuid4().hex[:8]}"


async def test_health_check_returns_true_for_running_sandbox(backend):
    assert await backend.health_check() is True


async def test_bash_hello_world_with_agent_id(backend, agent_id):
    result = await backend.execute(
        code="echo hello-from-$(whoami)",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert result.success is True
    # Match the prefix only — the sandbox image's default user may change.
    assert "hello-from-" in result.stdout
    assert result.exit_code == 0


async def test_python_uses_jupyter_session_state(backend, agent_id):
    """Variables set in one call persist to the next via session_id."""
    r1 = await backend.execute(
        code="x = 42",
        language="python",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert r1.success is True

    r2 = await backend.execute(
        code="print(x * 2)",
        language="python",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert r2.success is True
    assert "84" in r2.stdout


async def test_shell_cwd_resets_to_work_dir_each_call(backend, agent_id):
    """cwd is statelessly reset to work_dir on every call (matches
    execute_code/subprocess semantics so the LLM has a single mental model)."""
    # Within a single call, `cd` still takes effect for chained commands.
    r1 = await backend.execute(
        code="cd /tmp && pwd",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert "/tmp" in r1.stdout, "cd should work within a single call"

    # Next call: cwd is back to work_dir, NOT carried over from the previous call.
    r2 = await backend.execute(
        code="pwd",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert r2.stdout.strip() == "/data/agents", (
        f"each call must reset cwd to work_dir; got {r2.stdout!r}"
    )


async def test_shell_env_vars_persist_across_calls(backend, agent_id):
    """Exported env vars DO persist across calls — the shell session itself
    is reused, only cwd is reset. Background processes also survive."""
    await backend.execute(
        code="export AIOSB_MARKER=persisted-value",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    r = await backend.execute(
        code='echo "marker=$AIOSB_MARKER"',
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert "marker=persisted-value" in r.stdout, (
        f"env vars should survive across calls; got {r.stdout!r}"
    )


async def test_shell_timeout_kills_tree_without_poisoning_stateful_session(
    backend, agent_id
):
    """The existing timeout is a process deadline, not just an HTTP deadline."""
    token = uuid.uuid4().hex[:8]
    bg_pidfile = f"/tmp/clawith-timeout-bg-{token}.pid"
    fg_pidfile = f"/tmp/clawith-timeout-fg-{token}.pid"
    session_id = f"clawith-{agent_id}"
    try:
        setup = await backend.execute(
            code=(
                "export AIOSB_TIMEOUT_MARKER=preserved; "
                f"nohup sleep 120 >/tmp/clawith-timeout-bg-{token}.log 2>&1 & "
                f"echo $! > {bg_pidfile}"
            ),
            language="bash",
            timeout=10,
            work_dir="/data/agents",
            agent_id=agent_id,
        )
        assert setup.success is True

        timed_out = await backend.execute(
            code=(
                "bash -c 'trap \"\" HUP INT TERM; "
                f"echo $$ > {fg_pidfile}; while :; do sleep 1; done'"
            ),
            language="bash",
            timeout=1,
            work_dir="/data/agents",
            agent_id=agent_id,
        )
        assert timed_out.success is False
        assert timed_out.exit_code == 124
        assert "timed out after 1s" in (timed_out.error or "")

        follow = await backend.execute(
            code=(
                f"if kill -0 $(cat {fg_pidfile}) 2>/dev/null; then exit 97; fi; "
                f"kill -0 $(cat {bg_pidfile}); "
                'printf "marker=%s followup-ok" "$AIOSB_TIMEOUT_MARKER"'
            ),
            language="bash",
            timeout=10,
            work_dir="/data/agents",
            agent_id=agent_id,
        )
        assert follow.success is True
        assert "marker=preserved followup-ok" in follow.stdout
    finally:
        async with httpx.AsyncClient() as client:
            await client.delete(
                f"{os.environ['SANDBOX_API_URL']}/v1/shell/sessions/{session_id}",
                timeout=10.0,
            )


async def test_two_agents_have_isolated_shell_sessions(backend):
    a1 = f"agentA-{uuid.uuid4().hex[:8]}"
    a2 = f"agentB-{uuid.uuid4().hex[:8]}"

    await backend.execute(
        code="export MARKER=from-A",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=a1,
    )
    r = await backend.execute(
        code='echo "marker=$MARKER"',
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=a2,
    )
    assert "marker=" in r.stdout
    assert "from-A" not in r.stdout  # B must NOT see A's env


async def test_unknown_session_id_auto_recreates(backend, agent_id):
    """If sandbox restarts and our session vanishes, execute() auto-recreates.

    Step 1: first execute() must create a named shell session
            `clawith-{agent_id}` (server-side identity tied to agent).
    Step 2: we manually delete that session via HTTP to simulate sandbox
            restart / GC.
    Step 3: next execute() must transparently recreate the session and
            succeed.

    The pre-DELETE existence check is what makes this test a meaningful
    TDD signal: a stub that doesn't create named sessions will fail at
    step 1, not vacuously pass through step 3.
    """
    await backend.execute(
        code="echo first",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )

    session_id = f"clawith-{agent_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{os.environ['SANDBOX_API_URL']}/v1/shell/sessions",
            timeout=5.0,
        )
        sessions = resp.json().get("data", {}).get("sessions", {})
        assert session_id in sessions, (
            f"Backend must create named session {session_id!r}; "
            f"sandbox only knows: {list(sessions.keys())[:10]}"
        )
        await client.delete(
            f"{os.environ['SANDBOX_API_URL']}/v1/shell/sessions/{session_id}",
            timeout=5.0,
        )

    r = await backend.execute(
        code="echo second",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert r.success is True
    assert "second" in r.stdout


# --------------------------------------------------------- per-session isolation


async def test_same_agent_two_conversations_have_isolated_shell_env(backend, agent_id):
    await backend.execute(
        code="export MARKER=from-conv1",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
        conversation_id="conv1",
    )
    same = await backend.execute(
        code='echo "marker=$MARKER"',
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
        conversation_id="conv1",
    )
    other = await backend.execute(
        code='echo "marker=$MARKER"',
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
        conversation_id="conv2",
    )
    assert "marker=from-conv1" in same.stdout  # persists inside the conversation
    assert "from-conv1" not in other.stdout  # never leaks to a sibling conversation


async def test_same_agent_two_conversations_have_isolated_python_kernels(backend, agent_id):
    await backend.execute(
        code="leak_probe = 'from-conv1'",
        language="python",
        timeout=30,
        work_dir="/data/agents",
        agent_id=agent_id,
        conversation_id="conv1",
    )
    same = await backend.execute(
        code="print(leak_probe)",
        language="python",
        timeout=30,
        work_dir="/data/agents",
        agent_id=agent_id,
        conversation_id="conv1",
    )
    other = await backend.execute(
        code="print(leak_probe)",
        language="python",
        timeout=30,
        work_dir="/data/agents",
        agent_id=agent_id,
        conversation_id="conv2",
    )
    assert "from-conv1" in same.stdout  # kernel state persists per conversation
    assert other.success is False  # sibling conversation gets NameError
    assert "NameError" in (other.stderr or "") + (other.error or "")


async def test_conversation_and_agent_fallback_sessions_do_not_collide(backend, agent_id):
    await backend.execute(
        code="export MARKER=from-fallback",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
    )
    r = await backend.execute(
        code='echo "marker=$MARKER"',
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
        conversation_id="conv1",
    )
    assert "from-fallback" not in r.stdout


async def test_lru_eviction_resets_oldest_conversation(backend, agent_id):
    backend._max_anchors_per_agent = 2
    await backend.execute(
        code="export MARKER=oldest",
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
        conversation_id="c1",
    )
    for conv in ("c2", "c3"):  # pushes c1 out of the LRU → its session is deleted
        await backend.execute(
            code="true",
            language="bash",
            timeout=10,
            work_dir="/data/agents",
            agent_id=agent_id,
            conversation_id=conv,
        )
    r = await backend.execute(
        code='echo "marker=$MARKER"',
        language="bash",
        timeout=10,
        work_dir="/data/agents",
        agent_id=agent_id,
        conversation_id="c1",
    )
    # c1 was evicted server-side; reactivating it recreates a FRESH session.
    assert r.success is True
    assert "oldest" not in r.stdout


# ----------------------------------------------- identity isolation (wrapper-based)
#
# These exercise the real failure the per-session work + wrapper-identity design
# fixes: in a shared (group IM) conversation, a later sender must never inherit a
# prior sender's CLI identity. We simulate a CLI tool with a fake "svc" binary
# that just echoes its identity env, and drive _compose_shell_command-level
# injection through backend.execute(inject=...).


async def _install_fake_svc(backend, agent_id) -> str:
    """Install a stand-in CLI binary INSIDE the sandbox (the test container's
    tmp is invisible to it). It prints whatever identity env it was given.
    Returns the in-sandbox path to use as a wrapper binary_path."""
    path = "/data/agents/_fake_svc.sh"
    install = (
        "printf '#!/bin/sh\\necho \"who=${YYBPC_CLI_USER_PHONE:-NONE}\"\\n' > "
        f"{path} && chmod 755 {path}"
    )
    r = await backend.execute(
        code=install, language="bash", timeout=15, work_dir="/data/agents",
        agent_id=agent_id,
    )
    assert r.success, f"failed to install fake svc: {r.stdout!r} {r.error!r}"
    return path


async def test_group_conversation_second_sender_without_inject_sees_no_prior_identity(
    backend, agent_id, tmp_path
):
    """A(with identity) then B(inject=None) in the SAME conversation: B must NOT
    inherit A's identity. This is the group-IM impersonation the design fixes."""
    binpath = await _install_fake_svc(backend, agent_id)
    conv = "groupchat-1"
    inject_a = {"wrappers": [{"name": "svc", "binary_path": binpath,
                              "env": {"YYBPC_CLI_USER_PHONE": "AAA111"}}]}
    # Sender A runs svc with identity AAA111.
    ra = await backend.execute(
        code="svc", language="bash", timeout=15, work_dir="/data/agents",
        agent_id=agent_id, conversation_id=conv, inject=inject_a,
    )
    assert "who=AAA111" in ra.stdout
    # Sender B's exec has NO injection (e.g. unmapped user / build blip).
    # svc must now be command-not-found OR identity-less — never AAA111.
    rb = await backend.execute(
        code="svc 2>&1 || echo SVC_GONE", language="bash", timeout=15,
        work_dir="/data/agents", agent_id=agent_id, conversation_id=conv, inject=None,
    )
    assert "AAA111" not in rb.stdout, f"B inherited A's identity: {rb.stdout!r}"


async def test_group_conversation_second_sender_overrides_identity(
    backend, agent_id, tmp_path
):
    """A then B (both with their own identity) in one conversation: B sees ONLY
    B's identity (the wrapper is rewritten with the current sender)."""
    binpath = await _install_fake_svc(backend, agent_id)
    conv = "groupchat-2"
    inject_a = {"wrappers": [{"name": "svc", "binary_path": binpath,
                              "env": {"YYBPC_CLI_USER_PHONE": "AAA111"}}]}
    inject_b = {"wrappers": [{"name": "svc", "binary_path": binpath,
                              "env": {"YYBPC_CLI_USER_PHONE": "BBB222"}}]}
    await backend.execute(code="svc", language="bash", timeout=15,
                          work_dir="/data/agents", agent_id=agent_id,
                          conversation_id=conv, inject=inject_a)
    rb = await backend.execute(code="svc", language="bash", timeout=15,
                               work_dir="/data/agents", agent_id=agent_id,
                               conversation_id=conv, inject=inject_b)
    assert "who=BBB222" in rb.stdout
    assert "AAA111" not in rb.stdout


async def test_identity_not_visible_in_session_env(backend, agent_id, tmp_path):
    """Identity rides in the wrapper, never the session env: `env` / `echo $VAR`
    in the same conversation must not reveal the phone."""
    binpath = await _install_fake_svc(backend, agent_id)
    conv = "groupchat-3"
    inject = {"wrappers": [{"name": "svc", "binary_path": binpath,
                            "env": {"YYBPC_CLI_USER_PHONE": "SECRET999"}}]}
    await backend.execute(code="svc", language="bash", timeout=15,
                          work_dir="/data/agents", agent_id=agent_id,
                          conversation_id=conv, inject=inject)
    r = await backend.execute(code='echo "leak=[$YYBPC_CLI_USER_PHONE]"; env | grep -c SECRET999 || true',
                              language="bash", timeout=15, work_dir="/data/agents",
                              agent_id=agent_id, conversation_id=conv, inject=inject)
    assert "SECRET999" not in r.stdout, f"identity leaked into session env: {r.stdout!r}"
    assert "leak=[]" in r.stdout


async def test_user_export_still_persists_across_calls_with_inject(backend, agent_id, tmp_path):
    """The session semantic we must NOT regress: the user's own export persists
    across calls in the same conversation, even though identity does not."""
    binpath = await _install_fake_svc(backend, agent_id)
    conv = "groupchat-4"
    inject = {"wrappers": [{"name": "svc", "binary_path": binpath,
                            "env": {"YYBPC_CLI_USER_PHONE": "X"}}]}
    await backend.execute(code="export MY_OWN=persisted-42", language="bash", timeout=15,
                          work_dir="/data/agents", agent_id=agent_id,
                          conversation_id=conv, inject=inject)
    r = await backend.execute(code='echo "mine=$MY_OWN"', language="bash", timeout=15,
                              work_dir="/data/agents", agent_id=agent_id,
                              conversation_id=conv, inject=inject)
    assert "mine=persisted-42" in r.stdout


# ---------------------------------------------------------- managed background Jobs


async def _stop_all_jobs(backend, agent_id: str, conversation_id: str) -> None:
    listed = await backend.manage_background_jobs(
        action="list_jobs",
        agent_id=agent_id,
        conversation_id=conversation_id,
    )
    for job in listed.get("jobs", []):
        await backend.manage_background_jobs(
            action="job_stop",
            agent_id=agent_id,
            conversation_id=conversation_id,
            job_id=job["job_id"],
            work_dir="/data/agents",
        )


async def test_multiple_jobs_are_independently_managed_per_chat_session(
    backend, agent_id
):
    conversation_id = f"jobs-{uuid.uuid4().hex[:8]}"
    try:
        first = await backend.start_background_job(
            code="echo first-ready; sleep 20",
            language="bash",
            timeout=30,
            work_dir="/data/agents",
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        second = await backend.start_background_job(
            code="echo second-ready; sleep 20",
            language="bash",
            timeout=30,
            work_dir="/data/agents",
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        assert first["success"] is True
        assert second["success"] is True
        assert first["job_id"] != second["job_id"]

        listed = await backend.manage_background_jobs(
            action="list_jobs",
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        assert {item["job_id"] for item in listed["jobs"]} == {
            first["job_id"],
            second["job_id"],
        }

        stopped = await backend.manage_background_jobs(
            action="job_stop",
            agent_id=agent_id,
            conversation_id=conversation_id,
            job_id=first["job_id"],
            work_dir="/data/agents",
        )
        assert stopped["success"] is True

        survivor = await backend.manage_background_jobs(
            action="job_status",
            agent_id=agent_id,
            conversation_id=conversation_id,
            job_id=second["job_id"],
        )
        assert survivor["success"] is True
        assert survivor["status"] == "running"
    finally:
        await _stop_all_jobs(backend, agent_id, conversation_id)


async def test_background_and_foreground_share_localhost_and_agent_files(
    backend, agent_id
):
    conversation_id = f"interop-{uuid.uuid4().hex[:8]}"
    token = uuid.uuid4().hex
    port = 20000 + int(token[:4], 16) % 20000
    marker = f"aio-job-{token}.txt"
    code = f"printf '%s' {marker} > {marker}; python -u -m http.server {port} --bind 127.0.0.1"
    try:
        job = await backend.start_background_job(
            code=code,
            language="bash",
            timeout=30,
            work_dir="/data/agents",
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        assert job["success"] is True

        foreground = await backend.execute(
            code=(
                "body=''; "
                "for i in $(seq 1 30); do "
                f"body=$(curl -fsS http://127.0.0.1:{port}/{marker}) && "
                "break; sleep .1; done; test -n \"$body\" && printf '%s' \"$body\""
            ),
            language="bash",
            timeout=10,
            work_dir="/data/agents",
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        assert foreground.success is True
        assert marker in foreground.stdout
    finally:
        await _stop_all_jobs(backend, agent_id, conversation_id)


async def test_running_job_logs_are_available_before_process_exits(backend, agent_id):
    conversation_id = f"logs-{uuid.uuid4().hex[:8]}"
    try:
        job = await backend.start_background_job(
            code="echo authorization-code-123; sleep 20",
            language="bash",
            timeout=30,
            work_dir="/data/agents",
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        assert job["success"] is True

        logs = await backend.manage_background_jobs(
            action="job_logs",
            agent_id=agent_id,
            conversation_id=conversation_id,
            job_id=job["job_id"],
            work_dir="/data/agents",
        )
        assert logs["status"] == "running"
        assert "authorization-code-123" in logs["output"]
    finally:
        await _stop_all_jobs(backend, agent_id, conversation_id)
