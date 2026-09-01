"""aio-sandbox backend with per-session (conversation) isolation.

Talks to agent-infra/sandbox (https://github.com/agent-infra/sandbox).
- Shell (bash / node): /v1/shell/sessions/create + /v1/shell/exec
- Python: /v1/jupyter/sessions/create + /v1/jupyter/execute (UUID session)

Per-session isolation strategy
------------------------------
Anchor:
  ``compute_session_anchor(agent_id, conversation_id)`` — conversation-scoped
  (``{agent_id}:{conversation_id}``) when the caller has a ChatSession, with
  a per-agent fallback (``{agent_id}``) for session-less callers. Different
  conversations of the same agent therefore never share shell env / cwd
  residue / background processes or jupyter kernel variables. The agent's
  *filesystem* (work_dir, HOME) intentionally stays per-agent — isolation
  applies to the execution environment, not the files.

Shell:
  Each anchor gets a shell session keyed ``aio-fg-<namespace>``.  Sessions
  persist across HTTP calls so `cd`,
  environment variables, and Python variables survive between consecutive
  execute() calls for the same conversation.

Lifecycle:
  Conversation anchors are tracked in a per-agent LRU (see _register_anchor);
  past the cap the oldest conversation's shell session and jupyter kernel are
  deleted best-effort so sandbox-side processes don't grow unboundedly with
  conversation count.

Jupyter (Python):
  The sandbox server ignores non-UUID session_id values on /v1/jupyter/execute
  and always assigns a server-generated UUID.  To maintain stateful kernels we
  therefore:
    1. Create a session explicitly via /v1/jupyter/sessions/create on first use.
    2. Store the returned UUID in an in-process dict (_jupyter_sessions) keyed
       by anchor string.
    3. Pass that UUID on every subsequent /v1/jupyter/execute call.
  If the sandbox container restarts (or the kernel is GC'd) the UUID becomes
  stale; the server silently creates a fresh kernel rather than returning
  "Session not found".  We detect staleness by checking execution_count == 1
  on a non-first call (or any NameError result), but the simplest reliable
  trigger is: re-create when the response session_id != what we sent.

Shell "Session not found" recovery:
  Shell sessions DO return {"success": false, "message": "Session not found"}
  when the session was GC'd.  We auto-recreate and retry once.

`exec_dir` / `cwd` is set from `work_dir` (caller passes the absolute
in-container path for the agent's workspace).

Configuration
-------------
- SANDBOX_API_URL: base URL (e.g. http://aio-sandbox:8080)
- SANDBOX_API_KEY: optional bearer token

Failure modes that propagate to ExecutionResult
-----------------------------------------------
- HTTPError on create / exec → ExecutionResult(success=False, exit_code=1)
- Timeout → ExecutionResult(success=False, exit_code=124)
- Two recreate attempts both failing → ExecutionResult(success=False, exit_code=1)
"""
import asyncio
import hashlib
import json
import re
import shutil
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from loguru import logger

from app.services.sandbox.base import (
    BaseSandboxBackend,
    ExecutionResult,
    SandboxCapabilities,
)
from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.remote.cdp_browser import (
    CdpConnection,
    CdpError,
    attach_page,
    capture_screenshot,
    eval_js,
    navigate_page,
    open_and_extract,
    page_title,
)

# Maximum stdout/stderr we surface to the caller. aio-sandbox itself caps
# raw output at 30 KB per call; we tighten that for LLM consumption.
_STDOUT_LIMIT = 10000
_STDERR_LIMIT = 5000

# Allow the patched sandbox a bounded interval after the caller's existing
# `timeout` deadline to kill/reap the foreground process group and serialize
# the final response. The public platform contract remains one timeout value.
_SHELL_TIMEOUT_RESPONSE_GRACE_SECONDS = 10.0

# CDP domains that escape per-conversation isolation in the SHARED container:
# Target.* can enumerate/attach to other conversations' contexts; Browser.* is
# a process-global (e.g. Browser.close would kill Chrome for everyone). The
# agent-facing web_cdp tool rejects these; the backend may still call Target.*
# internally to manage its own context/page.
_BROWSER_GLOBAL_CDP_DOMAINS = ("Target.", "Browser.")


def _is_browser_global_method(method: str) -> bool:
    return method.startswith(_BROWSER_GLOBAL_CDP_DOMAINS)


def compute_session_anchor(
    agent_id: str | None, conversation_id: str | None
) -> str:
    """Key that isolates shell/jupyter sessions inside aio-sandbox.

    Conversation-scoped when a conversation_id is available, with an
    agent-level fallback that can never collide with a conversation key
    (the ':' separator only appears in conversation-scoped anchors).
    """
    agent_part = agent_id or "default"
    if conversation_id:
        return f"{agent_part}:{conversation_id}"
    return agent_part


# Managed Job logs use a standard cache directory. Executables live only in
# ``$HOME/.local/bin`` and are shared by foreground and background executions.
_SESSION_RUNTIME_ROOT = ".cache/aio/jobs"
_JOB_ID_RE = re.compile(r"^job_[0-9a-f]{12}$")
_JOB_INITIAL_OUTPUT_WAIT_SECONDS = 2.0


def compute_session_namespace(anchor: str) -> str:
    """Return the stable filesystem/session namespace shared by one chat session."""
    return hashlib.sha256(anchor.encode()).hexdigest()[:16]


def _job_log_path(anchor: str, job_id: str) -> str:
    return (
        f"$HOME/{_SESSION_RUNTIME_ROOT}/{compute_session_namespace(anchor)}"
        f"/{job_id}/output.log"
    )


def _job_session_prefix(anchor: str) -> str:
    return f"aio-job-{compute_session_namespace(anchor)}-"


def _foreground_session_id(anchor: str) -> str:
    return f"aio-fg-{compute_session_namespace(anchor)}"


def _job_session_id(anchor: str, job_id: str) -> str:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise ValueError("Invalid background job ID")
    return f"{_job_session_prefix(anchor)}{job_id}"


from app.services.sandbox.remote.aio_sandbox_browser import AioSandboxBrowserMixin
from app.services.sandbox.remote.aio_sandbox_execution import AioSandboxExecutionMixin


class AioSandboxBackend(
    AioSandboxExecutionMixin,
    AioSandboxBrowserMixin,
    BaseSandboxBackend,
):
    """aio-sandbox backend with per-session (conversation) shell + jupyter sessions."""

    name = "aio_sandbox"

    def __init__(self, config: SandboxConfig):
        self.config = config
        self.base_url = (config.api_url or "").rstrip("/")
        if not self.base_url:
            raise ValueError(
                "aio-sandbox URL is required. Set SANDBOX_API_URL "
                "(e.g. http://aio-sandbox:8080)."
            )
        # Maps anchor → server-assigned UUID for jupyter kernels.
        # In-process cache; lives as long as the backend instance.
        self._jupyter_sessions: dict[str, str] = {}
        # Per-agent LRU of live *conversation* anchors. With per-session
        # isolation, anchors grow with conversations; without a cap the
        # sandbox accumulates one bash (and possibly one jupyter kernel,
        # ~50-100 MB) per conversation forever. Agent-level fallback anchors
        # (no ':') are one-per-agent and exempt. In-process approximation:
        # multiple workers each keep their own count — the goal is bounding
        # growth, not enforcing an exact quota.
        self._anchor_lru: dict[str, OrderedDict[str, None]] = {}
        self._max_anchors_per_agent = 8
        # anchor -> CDP browserContextId for the per-conversation isolated
        # browser context. Lives on this cached instance like _jupyter_sessions;
        # will be disposed in _evict_anchor (Task 5).
        self._browser_contexts: dict[str, str] = {}
        # anchor -> CDP targetId for the PERSISTENT RPA page. Lives in the
        # anchor's browserContext and is never closed between calls, so the
        # page's URL/DOM/cookies/login survive across web_* tool calls. Disposed
        # together with the context in _evict_anchor.
        self._browser_pages: dict[str, str] = {}

    # ------------------------------------------------------------------ Public API

    def get_capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            supported_languages=["python", "bash", "node", "javascript"],
            max_timeout=self.config.max_timeout,
            max_memory_mb=512,
            network_available=True,
            filesystem_available=True,
        )

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.base_url}/v1/sandbox",
                    timeout=5.0,
                )
                return resp.status_code == 200
        except Exception:
            return False

    async def execute(
        self,
        code: str,
        language: str,
        timeout: int = 30,
        work_dir: str | None = None,
        agent_id: str | None = None,
        conversation_id: str | None = None,
        **kwargs,
    ) -> ExecutionResult:
        start = time.time()
        anchor = compute_session_anchor(agent_id, conversation_id)
        # exec_dir must be absolute. Caller passes the in-container path.
        cwd = work_dir or "/data/agents"

        try:
            async with httpx.AsyncClient() as client:
                for stale in self._register_anchor(agent_id or "default", anchor):
                    await self._evict_anchor(client, stale)
                inject = kwargs.get("inject")
                if language == "python":
                    result = await self._run_jupyter(
                        client, anchor=anchor, code=code, cwd=cwd, timeout=timeout,
                        inject=inject,
                    )
                elif language in ("bash", "node", "javascript"):
                    result = await self._run_shell(
                        client,
                        anchor=anchor,
                        code=code,
                        language=language,
                        cwd=cwd,
                        timeout=timeout,
                        inject=inject,
                    )
                else:
                    return self._error_result(
                        f"Unsupported language: {language}. Use python, bash, or node.",
                        start,
                    )
        except httpx.TimeoutException:
            return self._error_result(
                f"Code execution timed out after {timeout}s",
                start,
                exit_code=124,
            )
        except Exception as e:
            logger.exception("[AioSandbox] Execution error")
            return self._error_result(f"aio-sandbox error: {str(e)[:200]}", start)

        result.duration_ms = int((time.time() - start) * 1000)
        return result

    # --------------------------------------------------------- Background jobs

    async def start_background_job(
        self,
        *,
        code: str,
        language: str,
        timeout: int,
        work_dir: str,
        agent_id: str,
        conversation_id: str,
        inject: dict | None = None,
    ) -> dict[str, Any]:
        """Start one independently managed job inside the chat session namespace.

        A job gets its own AIO shell/tmux session so it cannot occupy the
        foreground shell's execution lock.  It still runs in the same AIO
        container and therefore shares PID/network/mount/IPC namespaces, HOME,
        files, ports and Unix sockets with foreground commands and sibling jobs.
        """
        if not conversation_id:
            return {
                "success": False,
                "status": "rejected",
                "error": "Background jobs require a chat session.",
            }
        if language not in ("python", "bash", "node", "javascript"):
            return {
                "success": False,
                "status": "rejected",
                "error": f"Unsupported background language: {language}",
            }

        anchor = compute_session_anchor(agent_id, conversation_id)
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        session_id = _job_session_id(anchor, job_id)
        command = self._compose_shell_command(
            cwd=work_dir,
            code=code,
            language=language,
            inject=inject,
            background_job=True,
            output_log=_job_log_path(anchor, job_id),
            context_ttl_seconds=timeout + 60,
        )

        async with httpx.AsyncClient() as client:
            try:
                await self._create_shell_session(client, session_id, work_dir)
                body, ok = await self._shell_exec_async(
                    client, session_id, command, timeout
                )
                if not ok:
                    await self._delete_job_session(client, session_id)
                    return {
                        "success": False,
                        "status": "failed",
                        "job_id": job_id,
                        "error": (body.get("message") or "Failed to start job")[:500],
                    }

                snapshot: dict[str, Any] = {
                    "success": True,
                    "status": "running",
                    "job_id": job_id,
                    "timeout": timeout,
                    "output": "",
                }
                deadline = time.monotonic() + _JOB_INITIAL_OUTPUT_WAIT_SECONDS
                while time.monotonic() < deadline:
                    current = await self._view_job(client, session_id)
                    if current is not None:
                        snapshot.update(current)
                        snapshot["output"] = self._read_job_log(
                            work_dir, anchor, job_id
                        ) or str(current.get("output") or "")
                        snapshot["success"] = True
                        snapshot["job_id"] = job_id
                        snapshot["timeout"] = timeout
                        if current.get("output") or current.get("status") != "running":
                            break
                    await asyncio.sleep(0.1)
                return snapshot
            except httpx.TimeoutException:
                # A lost start response is ambiguous. The deterministic session
                # ID lets us probe before reporting failure instead of submitting
                # the command a second time and creating a duplicate process.
                current = await self._view_job(client, session_id)
                if current is not None:
                    return {
                        "success": True,
                        "job_id": job_id,
                        "timeout": timeout,
                        **current,
                    }
                return {
                    "success": False,
                    "status": "failed",
                    "job_id": job_id,
                    "error": "Timed out while starting the background job.",
                }
            except Exception as exc:  # noqa: BLE001
                logger.exception("[AioSandbox] Background job start failed")
                return {
                    "success": False,
                    "status": "failed",
                    "job_id": job_id,
                    "error": f"Background job start failed: {str(exc)[:300]}",
                }

    async def manage_background_jobs(
        self,
        *,
        action: str,
        agent_id: str,
        conversation_id: str,
        job_id: str | None = None,
        tail_lines: int = 100,
        work_dir: str | None = None,
    ) -> dict[str, Any]:
        """List, inspect or stop jobs owned by one chat session."""
        if not conversation_id:
            return {
                "success": False,
                "status": "rejected",
                "error": "Background jobs require a chat session.",
            }
        anchor = compute_session_anchor(agent_id, conversation_id)
        action = action.strip().lower()

        async with httpx.AsyncClient() as client:
            if action == "list_jobs":
                sessions = await self._list_shell_sessions(client)
                prefix = _job_session_prefix(anchor)
                jobs: list[dict[str, Any]] = []
                for session_id, info in sessions.items():
                    if not session_id.startswith(prefix):
                        continue
                    candidate = session_id[len(prefix) :]
                    if not _JOB_ID_RE.fullmatch(candidate):
                        continue
                    jobs.append(
                        {
                            "job_id": candidate,
                            "status": info.get("status", "running"),
                            "created_at": info.get("created_at"),
                            "age_seconds": info.get("age_seconds"),
                        }
                    )
                jobs.sort(key=lambda item: str(item.get("created_at") or ""))
                return {"success": True, "status": "ok", "jobs": jobs}

            if action not in {"job_status", "job_logs", "job_stop"}:
                return {
                    "success": False,
                    "status": "rejected",
                    "error": f"Unsupported job action: {action}",
                }
            if not job_id or not _JOB_ID_RE.fullmatch(job_id):
                return {
                    "success": False,
                    "status": "rejected",
                    "error": "A valid job_id is required.",
                }

            session_id = _job_session_id(anchor, job_id)
            snapshot = await self._view_job(client, session_id)
            if snapshot is None:
                return {
                    "success": False,
                    "status": "not_found",
                    "job_id": job_id,
                    "error": "Background job not found in this chat session.",
                }

            if action == "job_status":
                snapshot.pop("output", None)
                return {"success": True, "job_id": job_id, **snapshot}
            if action == "job_logs":
                output = self._read_job_log(
                    work_dir, anchor, job_id
                ) or str(snapshot.get("output") or "")
                lines = output.splitlines()
                snapshot["output"] = "\n".join(lines[-max(1, min(tail_lines, 500)) :])
                return {"success": True, "job_id": job_id, **snapshot}

            # A dedicated job session is the lifecycle boundary, so deleting it
            # kills only that job's process tree and leaves foreground/siblings.
            stopped = await self._delete_job_session(client, session_id)
            output = self._read_job_log(
                work_dir, anchor, job_id
            ) or str(snapshot.get("output") or "")
            if stopped and work_dir:
                self._cleanup_job_runtime_dir(work_dir, anchor, job_id)
            return {
                "success": stopped,
                "status": "stopped" if stopped else "not_found",
                "job_id": job_id,
                "output": output[-_STDOUT_LIMIT:],
            }

    async def _shell_exec_async(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        command: str,
        timeout: int,
    ) -> tuple[dict[str, Any], bool]:
        resp = await client.post(
            f"{self.base_url}/v1/shell/exec",
            json={
                "id": session_id,
                "command": command,
                "async_mode": True,
                # For a background job the existing timeout is its maximum
                # lifetime. The HTTP request itself returns immediately.
                "timeout": float(timeout),
            },
            headers=self._headers(),
            timeout=10.0,
        )
        if resp.status_code != 200:
            return {"message": f"HTTP {resp.status_code}: {resp.text[:200]}"}, False
        body = resp.json()
        return body, bool(body.get("success"))

    async def _view_job(
        self, client: httpx.AsyncClient, session_id: str
    ) -> dict[str, Any] | None:
        resp = await client.post(
            f"{self.base_url}/v1/shell/view",
            json={"id": session_id},
            headers=self._headers(),
            timeout=5.0,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError(f"Job view HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if not body.get("success"):
            return None
        data = body.get("data") or {}
        status = data.get("status") or "running"
        if status == "hard_timeout":
            status = "timed_out"
        elif status == "terminated":
            status = "failed"
        return {
            "status": status,
            "exit_code": data.get("exit_code"),
            "output": str(data.get("output") or "")[-_STDOUT_LIMIT:],
        }

    async def _list_shell_sessions(
        self, client: httpx.AsyncClient
    ) -> dict[str, dict[str, Any]]:
        resp = await client.get(
            f"{self.base_url}/v1/shell/sessions",
            headers=self._headers(),
            timeout=5.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Session list HTTP {resp.status_code}: {resp.text[:200]}")
        return (resp.json().get("data") or {}).get("sessions") or {}

    async def _delete_job_session(
        self, client: httpx.AsyncClient, session_id: str
    ) -> bool:
        resp = await client.delete(
            f"{self.base_url}/v1/shell/sessions/{session_id}",
            headers=self._headers(),
            timeout=10.0,
        )
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise RuntimeError(f"Job stop HTTP {resp.status_code}: {resp.text[:200]}")
        return bool(resp.json().get("success"))

    @staticmethod
    def _job_runtime_dir(work_dir: str, anchor: str, job_id: str) -> Path | None:
        if not work_dir or not _JOB_ID_RE.fullmatch(job_id):
            return None
        root = Path(work_dir).resolve()
        target = (
            root
            / _SESSION_RUNTIME_ROOT
            / compute_session_namespace(anchor)
            / job_id
        ).resolve()
        return target if target.is_relative_to(root) else None

    @classmethod
    def _read_job_log(cls, work_dir: str | None, anchor: str, job_id: str) -> str:
        if not work_dir:
            return ""
        try:
            runtime_dir = cls._job_runtime_dir(work_dir, anchor, job_id)
            log_path = runtime_dir / "output.log" if runtime_dir else None
            if not log_path or not log_path.is_file():
                return ""
            with log_path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - _STDOUT_LIMIT))
                return handle.read(_STDOUT_LIMIT).decode(errors="replace")
        except OSError as exc:
            logger.warning(f"[AioSandbox] Job log read failed: {exc}")
            return ""

    @classmethod
    def _cleanup_job_runtime_dir(
        cls, work_dir: str, anchor: str, job_id: str
    ) -> None:
        try:
            target = cls._job_runtime_dir(work_dir, anchor, job_id)
            if target and target.exists():
                shutil.rmtree(target)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[AioSandbox] Job runtime cleanup failed: {exc}")

    # ------------------------------------------------------------------ Anchor LRU

    def _register_anchor(self, agent_part: str, anchor: str) -> list[str]:
        """Track a conversation anchor; return anchors evicted past the cap.

        Agent-level fallback anchors (no ':') are exempt — they are
        one-per-agent by construction so they neither count nor get evicted.
        """
        if ":" not in anchor:
            return []
        lru = self._anchor_lru.setdefault(agent_part, OrderedDict())
        if anchor in lru:
            lru.move_to_end(anchor)
            return []
        lru[anchor] = None
        evicted: list[str] = []
        while len(lru) > self._max_anchors_per_agent:
            evicted.append(lru.popitem(last=False)[0])
        return evicted

    async def _evict_anchor(self, client: httpx.AsyncClient, anchor: str) -> None:
        """Best-effort delete of an evicted anchor's sandbox sessions.

        Failures are non-fatal: a session we fail to delete is reclaimed when
        the sandbox container restarts, and the anchor itself recovers via the
        existing "Session not found" recreate path if it ever becomes active
        again.
        """
        try:
            await client.delete(
                f"{self.base_url}/v1/shell/sessions/{_foreground_session_id(anchor)}",
                headers=self._headers(),
                timeout=5.0,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AioSandbox] evict shell session for {anchor!r} failed: {e}")
        kernel_uuid = self._jupyter_sessions.pop(anchor, None)
        if kernel_uuid:
            try:
                await client.delete(
                    f"{self.base_url}/v1/jupyter/sessions/{kernel_uuid}",
                    headers=self._headers(),
                    timeout=5.0,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[AioSandbox] evict jupyter kernel for {anchor!r} failed: {e}")
        browser_ctx = self._browser_contexts.pop(anchor, None)
        # The persistent page lives inside browser_ctx; disposing the context
        # below kills its target, so just drop the stale id.
        self._browser_pages.pop(anchor, None)
        if browser_ctx:
            try:
                ws_url = await self._browser_ws_url(client)
                async with websockets.connect(ws_url, max_size=1_000_000) as ws_conn:
                    await CdpConnection(ws_conn).call(
                        "Target.disposeBrowserContext",
                        {"browserContextId": browser_ctx},
                        timeout=5.0,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[AioSandbox] evict browser context for {anchor!r} failed: {e}"
                )
