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
import json
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
                        inject_prefix=kwargs.get("inject_prefix"),
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
        inject_prefix: str | None = None,
    ) -> ExecutionResult:
        session_id = f"clawith-{anchor}"
        cmd = self._compose_shell_command(
            cwd=cwd, code=code, language=language, inject_prefix=inject_prefix
        )

        body, ok = await self._shell_exec(client, session_id, cmd, timeout)
        # Both "session not found" (ok=false) and the v1.0.0.152 zombie state
        # (ok=true + status:"terminated") need a recreate before the user's
        # command actually runs on a fresh bash.
        if self._is_session_missing(body):
            await self._create_shell_session(client, session_id, cwd)
            body, ok = await self._shell_exec(client, session_id, cmd, timeout)

        # Always pull whatever the server gave us. Even on `ok=False` the
        # `data.output` field often contains the actionable stderr text — don't
        # let it get swallowed by the generic top-level message.
        data = body.get("data", {}) or {}
        output = (data.get("output") or "")[:_STDOUT_LIMIT]
        server_message = (body.get("message") or "").strip()

        # Server returned status:"running" → our `timeout` window elapsed but
        # the underlying bash process is still alive and will hold the session
        # forever (v1.0.0.152 doesn't implement `hard_timeout`, verified by
        # direct curl probes). Queued follow-up commands would pile up behind
        # it. The only way to release the session is to DELETE it; the next
        # call into `_run_shell` will see "Session not found" via
        # `_is_session_missing` and auto-recreate a fresh session.
        if data.get("status") == "running":
            try:
                await client.delete(
                    f"{self.base_url}/v1/shell/sessions/{session_id}",
                    headers=self._headers(),
                    timeout=5.0,
                )
                # v1.0.0.152 quirk: after DELETE the session stays in the
                # sessions list with status="terminated" — subsequent exec
                # calls return success=True with exit_code=-1 and empty
                # output (the LLM-visible "-1 cascade" bug). Atomically
                # recreate the session here so the next call uses a fresh
                # bash subprocess.
                await self._create_shell_session(client, session_id, cwd)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[AioSandbox] DELETE+recreate after timeout for {session_id} failed: {e}"
                )
            return ExecutionResult(
                success=False,
                stdout=output,
                stderr="",
                exit_code=124,
                duration_ms=0,
                error=(
                    f"Command timed out after {timeout}s and was killed. The "
                    f"shell session has been reset — any exported env vars and "
                    f"background processes are gone; the next call starts "
                    f"fresh. If the command was waiting for stdin (an "
                    f"interactive prompt), retry with non-interactive flags "
                    f"like --yes / -y / --non-interactive. If the command "
                    f"legitimately needs longer than {timeout}s, pass a larger "
                    f"timeout in the tool arguments."
                ),
            )

        if not ok:
            # Build the most informative error we can. If we have command output
            # surface it as stderr so the LLM sees the real failure. The server's
            # top-level message (which is sometimes a server-side Python exception
            # like "'ErrorObservation' object has no attribute 'exit_code'") goes
            # in `error` so the LLM can tell it apart from its own code's stderr.
            err = server_message or "Shell execution failed"
            if "'ErrorObservation'" in err or "AttributeError" in err:
                err = (
                    f"sandbox server-side error: {err}. "
                    f"Try a simpler command, split into multiple steps, "
                    f"or fall back to the python tool."
                )
            return ExecutionResult(
                success=False,
                stdout="",
                stderr=output,
                exit_code=1,
                duration_ms=0,
                error=err,
            )

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
        # Pass `timeout` so the server returns control after that many seconds
        # with status:"running" instead of holding the HTTP connection open
        # until our httpx timeout fires. v1.0.0.152 does NOT honor
        # `hard_timeout` (verified by direct curl probe), so the only way to
        # actually release a stuck command is to DELETE the session — see
        # the status:"running" branch in _run_shell. Without sending
        # `timeout` here, the server would wait forever for hang commands.
        resp = await client.post(
            f"{self.base_url}/v1/shell/exec",
            json={
                "id": session_id,
                "command": command,
                "timeout": float(timeout),
            },
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

    @classmethod
    def _compose_shell_command(
        cls,
        *,
        cwd: str,
        code: str,
        language: str,
        inject_prefix: str | None,
    ) -> str:
        """Compose the full per-exec command: env exports → CLI function
        injection block → user command.

        CLI functions are injected fresh on every exec so identity env
        (bound inside each function body) always reflects the *current*
        conversation user — see services/cli_tools/sandbox_inject.py.
        The prefix may span multiple physical lines (env values can embed
        newlines); it is always concatenated whole, never line-filtered.

        Force-reset cwd AND HOME to the agent root on every call. The shell
        session persists across calls (so exported env vars / background
        processes survive), but the working directory + HOME are statelessly
        reset to align with execute_code (subprocess) semantics.

        HOME is the critical one for SSH / git / npm / pip --user / etc. —
        the underlying sandbox container has HOME=/home/gem which would be
        shared by every agent, so `~/.ssh/id_*` written by one agent would
        be readable by another. Pinning HOME=<agent root> makes `ssh user@host`,
        `git config --global ...`, `~/.ssh/known_hosts`, etc. all land in the
        agent's own private workspace, mirroring a per-user Linux box.
        Use a literal-quoted path so unusual chars in agent_id can't escape.

        Package-isolation env vars: PIP_USER=1 makes `pip install xxx`
        land in $HOME/.local/... per-agent; NPM_CONFIG_PREFIX redirects
        `npm install -g xxx` to $HOME/.npm-global/... per-agent (plain
        `npm install xxx` was already per-cwd which is per-agent here).
        PATH augmented so any globally-installed npm bin (e.g. `tsc`,
        `vite`) is found.

        Non-interactive shell env vars: signals every well-behaved CI-aware
        tool (npm, yarn, pnpm, npx, prompts, apt, debconf, git over https)
        to skip prompts and pick safe defaults. This is the standard CI
        contract — not a hack. Tools that ignore these (rare) will still
        hit the status:running timeout branch and trigger a session reset.
        """
        quoted_cwd = "'" + cwd.replace("'", "'\\''") + "'"
        exports = (
            f"cd {quoted_cwd} && "
            f"export HOME={quoted_cwd} && "
            f"export PIP_USER=1 && "
            f'export NPM_CONFIG_PREFIX="$HOME/.npm-global" && '
            f'export PATH="$HOME/.npm-global/bin:$PATH" && '
            f"export CI=true && "
            f"export NPM_CONFIG_YES=true && "
            f"export DEBIAN_FRONTEND=noninteractive && "
            f"export GIT_TERMINAL_PROMPT=0 && "
            f"export NO_COLOR=1"
        )
        user_cmd = cls._build_shell_command(code, language)
        if inject_prefix:
            # Injection block (function defs) and user command on separate
            # lines. aio-sandbox >= 1.9.3 splits newline-separated commands and
            # re-joins them with ';' at its shell-exec layer, so natural
            # newlines are correct (this is also what makes agent multi-line
            # bash work). NOTE: 1.0.0.152 lacked this and returned
            # ErrorObservation on any '\n' — prod runs 1.9.3.
            return f"{exports} && {inject_prefix}\n{user_cmd}"
        return f"{exports} && {user_cmd}"

    @staticmethod
    def _build_shell_command(code: str, language: str) -> str:
        if language == "bash":
            return code
        if language in ("node", "javascript"):
            # Use a here-doc with a random delimiter so we never have to escape
            # the user's code. A `node -e '...'` form needs every `'` in the
            # code replaced with `'\''`, and that mess leaks back into the
            # LLM's view of stdout, confusing it about whether its script was
            # transmitted correctly.
            import secrets
            delim = "AIOSB_NODE_" + secrets.token_hex(16).upper()
            return f"node <<'{delim}'\n{code}\n{delim}"
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
        # Ensure we have a real UUID session for this anchor, and that the
        # kernel's HOME env is pinned to the agent root (the underlying
        # container has HOME=/home/gem which would be shared across agents;
        # we want per-agent ~/.ssh / ~/.gitconfig semantics, matching the
        # shell tool). The HOME pin runs as a silent setup call the very
        # first time we create a kernel for this anchor — subsequent user
        # cells therefore start at line 1 with clean traceback line numbers.
        session_uuid = await self._ensure_jupyter_session(client, anchor, cwd)

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

        # Always try to parse outputs — even when the top-level `success=false`
        # the server frequently embeds the real Python traceback under
        # `data.outputs[].error.traceback`. Surfacing that detail is what makes
        # the difference between "❌ Error: Code execution error" (useless) and
        # the actual NameError / SyntaxError the LLM can act on.
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
                ename = out.get("ename", "")
                evalue = out.get("evalue", "")
                tb = out.get("traceback") or []
                if tb:
                    stderr_parts.append("\n".join(tb))
                else:
                    stderr_parts.append(f"{ename}: {evalue}".strip(": "))

        status = data.get("status", "ok")
        has_error_output = any(
            out.get("output_type") == "error"
            for out in data.get("outputs", []) or []
        )
        ok_run = ok and status == "ok" and not has_error_output

        # Compose a never-useless error message:
        # 1. If we extracted any traceback / stderr, surface its first line so the
        #    LLM gets the actionable hint without parsing structured fields.
        # 2. If the server completely failed and gave us nothing parseable, dump
        #    the raw response body — anything is better than the bare
        #    "Code execution error" string the LLM saw before.
        error_msg = None
        if not ok_run:
            if stderr_parts:
                # Jupyter tracebacks start with a row of dashes (a visual
                # separator) and end with "ExceptionName: message" — the
                # last line is the actionable summary, the first line is
                # decorative. Pick the last non-empty, non-dashes line.
                lines = [l for l in "\n".join(stderr_parts).splitlines() if l.strip()]
                candidates = [l for l in lines if set(l.strip()) - {"-", "="}]
                error_msg = ((candidates or lines)[-1] if (candidates or lines) else "Unknown error")[:300]
            elif not ok:
                raw = body.get("message") or json.dumps(body)[:500]
                error_msg = f"sandbox returned no traceback. Raw response: {raw[:300]}"
            else:
                error_msg = f"jupyter status={status!r} (no outputs)"

        return ExecutionResult(
            success=ok_run,
            stdout=("".join(stdout_parts))[:_STDOUT_LIMIT],
            stderr=("".join(stderr_parts))[:_STDERR_LIMIT],
            exit_code=0 if ok_run else 1,
            duration_ms=0,
            error=error_msg,
        )

    async def _ensure_jupyter_session(
        self,
        client: httpx.AsyncClient,
        anchor: str,
        cwd: str,
    ) -> str:
        """Return the server UUID for this anchor, creating one if needed.

        On first creation we silently pin os.environ['HOME'] = cwd so the
        kernel (and any subprocess.run() it spawns) sees the agent root as
        $HOME — ssh, git, npm, etc. then find per-agent ~/.ssh, ~/.gitconfig
        etc. instead of the container-shared /home/gem. The setup runs as a
        dedicated execute call before any user code, so user cells keep
        clean line numbers (their first cell is still `In[1]` line 1).
        """
        if anchor in self._jupyter_sessions:
            return self._jupyter_sessions[anchor]
        session_uuid = await self._create_jupyter_session(client)
        if session_uuid:  # only cache real UUIDs; empty string means create failed
            self._jupyter_sessions[anchor] = session_uuid
            # Silent per-agent isolation setup. Failures are non-fatal — worst
            # case the kernel falls back to the container-shared HOME/site
            # paths. Mirrors the shell-side export block AND forces sys.path
            # to include the per-agent user-site, because Python computes
            # user-site at interpreter startup (before our HOME override
            # takes effect); without this `pip install` from a shell cell
            # would land in <cwd>/.local but `import` from a python cell
            # would still look at /home/gem/.local. Explicit sys.path.insert
            # at the front guarantees per-agent versions win.
            user_site_py310 = cwd + "/.local/lib/python3.10/site-packages"
            setup = (
                "import os, sys\n"
                f"os.environ['HOME'] = {cwd!r}\n"
                "os.environ['PIP_USER'] = '1'\n"
                "os.environ['NO_COLOR'] = '1'\n"
                f"os.environ['PYTHONUSERBASE'] = {(cwd + '/.local')!r}\n"
                f"os.environ['NPM_CONFIG_PREFIX'] = {(cwd + '/.npm-global')!r}\n"
                "os.environ['PATH'] = "
                f"{(cwd + '/.npm-global/bin')!r} + os.pathsep + os.environ.get('PATH', '')\n"
                f"_ag_us = {user_site_py310!r}\n"
                "os.makedirs(_ag_us, exist_ok=True)\n"
                "if _ag_us not in sys.path: sys.path.insert(0, _ag_us)\n"
                # IPython colorizes tracebacks regardless of NO_COLOR; ask the
                # kernel itself to switch its color scheme to plain text so
                # exception output is readable without client-side stripping.
                "try:\n"
                "    _ip = get_ipython()\n"
                "    if _ip is not None: _ip.run_line_magic('colors', 'NoColor')\n"
                "except Exception: pass"
            )
            try:
                await self._jupyter_exec(client, session_uuid, setup, cwd, 10)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[AioSandbox] jupyter env setup failed: {e}")
        return session_uuid

    async def _create_jupyter_session(
        self,
        client: httpx.AsyncClient,
    ) -> str:
        """Create a new jupyter kernel and return its server-assigned UUID.

        Pinned to `python3.10` so the kernel uses the same Python interpreter
        as the system `pip` binary. Without this pin the default `python3`
        kernelspec is ambiguous on the aio-sandbox image (multiple Python
        versions installed under /opt/) and a `pip install` from a shell
        cell could land in a site dir the jupyter kernel doesn't import from.
        """
        resp = await client.post(
            f"{self.base_url}/v1/jupyter/sessions/create",
            json={"kernel_name": "python3.10"},
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
        if "session not found" in msg:
            return True
        # v1.0.0.152 quirk: after DELETE (or a kill-from-elsewhere) the
        # session lingers in the list with status="terminated" and exec
        # calls against it return success:true + exit_code:-1 + empty
        # output. Treat that as missing so the recreate path runs and the
        # next command lands on a fresh bash subprocess.
        data = body.get("data") or {}
        if data.get("status") == "terminated":
            return True
        return False

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
