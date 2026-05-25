"""aio-sandbox backend with per-agent session isolation.

Talks to agent-infra/sandbox (https://github.com/agent-infra/sandbox).
- Shell (bash / node): /v1/shell/sessions/create + /v1/shell/exec
- Python: /v1/jupyter/sessions/create + /v1/jupyter/execute (UUID session)

Per-agent isolation strategy
----------------------------
Shell:
  Each agent gets a shell session keyed `clawith-{agent_id}`.  The sandbox
  accepts arbitrary string IDs for shell sessions, so we can use a stable
  human-readable key.  Sessions persist across HTTP calls so `cd`,
  environment variables, and Python variables survive between consecutive
  execute() calls for the same agent.

Jupyter (Python):
  The sandbox server ignores non-UUID session_id values on /v1/jupyter/execute
  and always assigns a server-generated UUID.  To maintain stateful kernels we
  therefore:
    1. Create a session explicitly via /v1/jupyter/sessions/create on first use.
    2. Store the returned UUID in an in-process dict (_jupyter_sessions) keyed
       by agent anchor string.
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
import time
from typing import Any

import httpx
from loguru import logger

from app.services.sandbox.base import (
    BaseSandboxBackend,
    ExecutionResult,
    SandboxCapabilities,
)
from app.services.sandbox.config import SandboxConfig

# Maximum stdout/stderr we surface to the caller. aio-sandbox itself caps
# raw output at 30 KB per call; we tighten that for LLM consumption.
_STDOUT_LIMIT = 10000
_STDERR_LIMIT = 5000


class AioSandboxBackend(BaseSandboxBackend):
    """aio-sandbox backend with per-agent shell + jupyter sessions."""

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
        **kwargs,
    ) -> ExecutionResult:
        start = time.time()
        # Default to a shared 'no-agent' session for callers without an agent.
        # Real agent calls always pass agent_id.
        anchor = agent_id or "default"
        # exec_dir must be absolute. Caller passes the in-container path.
        cwd = work_dir or "/data/agents"

        try:
            async with httpx.AsyncClient() as client:
                if language == "python":
                    result = await self._run_jupyter(
                        client, anchor=anchor, code=code, cwd=cwd, timeout=timeout
                    )
                elif language in ("bash", "node", "javascript"):
                    result = await self._run_shell(
                        client,
                        anchor=anchor,
                        code=code,
                        language=language,
                        cwd=cwd,
                        timeout=timeout,
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

    # ------------------------------------------------------------------ Shell path

    async def _run_shell(
        self,
        client: httpx.AsyncClient,
        *,
        anchor: str,
        code: str,
        language: str,
        cwd: str,
        timeout: int,
    ) -> ExecutionResult:
        session_id = f"clawith-{anchor}"
        cmd = self._build_shell_command(code, language)

        body, ok = await self._shell_exec(client, session_id, cmd, timeout)
        if not ok and self._is_session_missing(body):
            await self._create_shell_session(client, session_id, cwd)
            body, ok = await self._shell_exec(client, session_id, cmd, timeout)

        if not ok:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=0,
                error=body.get("message", "Shell execution failed"),
            )

        data = body.get("data", {}) or {}
        output = (data.get("output") or "")[:_STDOUT_LIMIT]
        exit_code = data.get("exit_code", 0)
        return ExecutionResult(
            success=(exit_code == 0),
            stdout=output,
            stderr="",
            exit_code=exit_code,
            duration_ms=0,
            error=None if exit_code == 0 else f"Command exited with code {exit_code}",
        )

    async def _create_shell_session(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        cwd: str,
    ) -> None:
        resp = await client.post(
            f"{self.base_url}/v1/shell/sessions/create",
            json={"id": session_id, "exec_dir": cwd},
            headers=self._headers(),
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning(
                f"[AioSandbox] create_session({session_id}) HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )

    async def _shell_exec(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        command: str,
        timeout: int,
    ) -> tuple[dict[str, Any], bool]:
        resp = await client.post(
            f"{self.base_url}/v1/shell/exec",
            json={"id": session_id, "command": command},
            headers=self._headers(),
            timeout=float(timeout + 10),
        )
        if resp.status_code != 200:
            return (
                {"message": f"HTTP {resp.status_code}: {resp.text[:200]}"},
                False,
            )
        body = resp.json()
        return body, bool(body.get("success"))

    @staticmethod
    def _build_shell_command(code: str, language: str) -> str:
        if language == "bash":
            return code
        # node / javascript: pass via stdin to avoid argv quoting headaches
        # and to support multi-line scripts cleanly.
        if language in ("node", "javascript"):
            # heredoc with random delimiter would be safer; keep simple for now.
            escaped = code.replace("'", "'\\''")
            return f"node -e '{escaped}'"
        return code

    # ------------------------------------------------------------------ Jupyter path

    async def _run_jupyter(
        self,
        client: httpx.AsyncClient,
        *,
        anchor: str,
        code: str,
        cwd: str,
        timeout: int,
    ) -> ExecutionResult:
        # Ensure we have a real UUID session for this anchor.
        session_uuid = await self._ensure_jupyter_session(client, anchor)

        body, ok = await self._jupyter_exec(client, session_uuid, code, cwd, timeout)

        # Detect stale session: server returned a *different* UUID, meaning our
        # UUID was unknown and a new kernel was quietly spawned.  Evict the
        # stale entry, record the new UUID, and carry on — the caller's code
        # ran on the new kernel so the result is still valid.
        returned_uuid = (body.get("data") or {}).get("session_id")
        if returned_uuid and returned_uuid != session_uuid:
            logger.info(
                f"[AioSandbox] jupyter session {session_uuid!r} was stale; "
                f"server assigned {returned_uuid!r}. Updating cache."
            )
            self._jupyter_sessions[anchor] = returned_uuid

        if not ok:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=0,
                error=body.get("message", "Jupyter execution failed"),
            )

        data = body.get("data", {}) or {}
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        for out in data.get("outputs", []) or []:
            otype = out.get("output_type")
            if otype == "stream" and out.get("name") == "stdout":
                stdout_parts.append(out.get("text", ""))
            elif otype == "stream" and out.get("name") == "stderr":
                stderr_parts.append(out.get("text", ""))
            elif otype == "execute_result":
                data_field = out.get("data") or {}
                stdout_parts.append(data_field.get("text/plain", ""))
            elif otype == "error":
                tb = out.get("traceback") or []
                stderr_parts.append("\n".join(tb) if tb else out.get("evalue", ""))

        status = data.get("status", "ok")
        ok_run = status == "ok" and not stderr_parts
        return ExecutionResult(
            success=ok_run,
            stdout=("".join(stdout_parts))[:_STDOUT_LIMIT],
            stderr=("".join(stderr_parts))[:_STDERR_LIMIT],
            exit_code=0 if ok_run else 1,
            duration_ms=0,
            error=None if ok_run else f"Jupyter status: {status}",
        )

    async def _ensure_jupyter_session(
        self,
        client: httpx.AsyncClient,
        anchor: str,
    ) -> str:
        """Return the server UUID for this anchor, creating one if needed."""
        if anchor in self._jupyter_sessions:
            return self._jupyter_sessions[anchor]
        session_uuid = await self._create_jupyter_session(client)
        self._jupyter_sessions[anchor] = session_uuid
        return session_uuid

    async def _create_jupyter_session(
        self,
        client: httpx.AsyncClient,
    ) -> str:
        """Create a new jupyter kernel and return its server-assigned UUID."""
        resp = await client.post(
            f"{self.base_url}/v1/jupyter/sessions/create",
            json={"kernel_name": "python3"},
            headers=self._headers(),
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning(
                f"[AioSandbox] create_jupyter_session HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
            # Return a placeholder; server will auto-create on execute anyway.
            return ""
        body = resp.json()
        return (body.get("data") or {}).get("session_id", "")

    async def _jupyter_exec(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        code: str,
        cwd: str,
        timeout: int,
    ) -> tuple[dict[str, Any], bool]:
        resp = await client.post(
            f"{self.base_url}/v1/jupyter/execute",
            json={"code": code, "session_id": session_id, "cwd": cwd, "timeout": timeout},
            headers=self._headers(),
            timeout=float(timeout + 10),
        )
        if resp.status_code != 200:
            return (
                {"message": f"HTTP {resp.status_code}: {resp.text[:200]}"},
                False,
            )
        body = resp.json()
        return body, bool(body.get("success"))

    # ------------------------------------------------------------------ Helpers

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.config.api_key:
            h["Authorization"] = f"Bearer {self.config.api_key}"
        return h

    @staticmethod
    def _is_session_missing(body: dict[str, Any]) -> bool:
        msg = (body.get("message") or "").lower()
        return "session not found" in msg or ("session" in msg and "not found" in msg)

    @staticmethod
    def _error_result(error_msg: str, start: float, exit_code: int = 1) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            stdout="",
            stderr="",
            exit_code=exit_code,
            duration_ms=int((time.time() - start) * 1000),
            error=error_msg,
        )
