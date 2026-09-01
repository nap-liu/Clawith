"""Shell and Jupyter execution methods for the AIO sandbox backend."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from app.services.sandbox.base import ExecutionResult
from app.services.sandbox.remote.aio_sandbox_backend import (
    _SHELL_TIMEOUT_RESPONSE_GRACE_SECONDS,
    _STDERR_LIMIT,
    _STDOUT_LIMIT,
    _foreground_session_id,
)


class AioSandboxExecutionMixin:
    async def _run_shell(
        self,
        client: httpx.AsyncClient,
        *,
        anchor: str,
        code: str,
        language: str,
        cwd: str,
        timeout: int,
        inject: dict | None = None,
    ) -> ExecutionResult:
        session_id = _foreground_session_id(anchor)
        cmd = self._compose_shell_command(
            cwd=cwd, code=code, language=language, inject=inject,
            context_ttl_seconds=timeout + 60,
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

        if data.get("status") == "hard_timeout":
            return ExecutionResult(
                success=False,
                stdout=output,
                stderr="",
                exit_code=124,
                duration_ms=0,
                error=f"Command timed out after {timeout}s and was killed.",
            )

        # `running` is not proof that hard termination failed: the AIO shell may
        # be reporting an already-active command / execution-lock conflict. A
        # session DELETE is destructive and also kills intentionally persistent
        # background processes, so preserve the session and surface a precise
        # retry/interrupt instruction. Only an explicit shell-corruption status
        # may justify deleting a stateful session.
        if data.get("status") == "running":
            return ExecutionResult(
                success=False,
                stdout=output,
                stderr="",
                exit_code=124,
                duration_ms=0,
                error=(
                    "SESSION_BUSY: the foreground shell still has an active "
                    "command. The session was preserved. If this is a service, "
                    "listener, authorization wait or other long-lived process, "
                    "retry with execution_mode='background'. Otherwise wait for "
                    "or stop the existing foreground command before retrying."
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
        command_timeout = float(timeout)
        resp = await client.post(
            f"{self.base_url}/v1/shell/exec",
            json={
                "id": session_id,
                "command": command,
                "timeout": command_timeout,
            },
            headers=self._headers(),
            timeout=command_timeout + _SHELL_TIMEOUT_RESPONSE_GRACE_SECONDS,
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
        inject: dict | None = None,
        background_job: bool = False,
        output_log: str | None = None,
        context_ttl_seconds: int = 3660,
    ) -> str:
        """Compose the per-exec command and deliver it **verbatim**.

        The full script (cwd/HOME/CI exports → CLI launcher setup → PATH setup
        → user code) is materialized and *sourced into the session shell* via a
        single-line base64 transport: ``source <(echo <b64> | base64 -d)``.
        This is critical:

        - **Correctness**: the sandbox's shell-exec layer splits multi-line
          commands on newlines (and re-joins with ';'), which silently breaks
          bash comments (a leading '#' swallows the rest of the joined line ->
          NO OUTPUT) and multi-line quoted strings. Running a materialized
          script sidesteps the splitter entirely — comments, heredocs,
          multi-line jq filters, loops all run exactly as written.
        - **Persistence**: ``source`` (not a child bash) keeps the documented
          session semantics — the user's ``export``/``cd``-within-script state
          survives to the next call of the *same conversation*. A child bash
          (used 2026-06-11..12) silently dropped every user export.
        - **No 串台 (group IM safe)**: ordinary CLI launchers contain no identity;
          ``toolscall`` additionally gets a unique execution-scope directory.
          Signed contexts are exported as function-local variables only while
          the current user script runs.

        Force-reset cwd AND HOME to the agent root on every call. The shell
        session persists across calls (so exported env vars / background
        processes survive), but the working directory + HOME are statelessly
        reset to align with execute_code (subprocess) semantics. Launcher setup
        comes after the HOME reset so ``$HOME/.local/bin`` is agent-private.

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
        PATH includes the standard local bin and globally-installed npm bin
        exactly once.

        Non-interactive shell env vars: signals every well-behaved CI-aware
        tool (npm, yarn, pnpm, npx, prompts, apt, debconf, git over https)
        to skip prompts and pick safe defaults. This is the standard CI
        contract — not a hack. Tools that ignore these (rare) will still
        receive an explicit busy/timeout result without deleting the session.
        """
        import base64

        from app.services.cli_tools.sandbox_inject import (
            build_launcher_write_sh,
            prepare_launchers,
            shell_quote,
        )

        quoted_cwd = "'" + cwd.replace("'", "'\\''") + "'"
        script_lines: list[str] = []
        # cwd + HOME reset FIRST so all user-local paths land under agent HOME.
        script_lines.append(
            f"cd {quoted_cwd} && "
            f"export HOME={quoted_cwd} && "
            f"export PIP_USER=1 && "
            f'export NPM_CONFIG_PREFIX="$HOME/.npm-global" && '
            f'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac && '
            f'case ":$PATH:" in *":$HOME/.npm-global/bin:"*) ;; *) export PATH="$HOME/.npm-global/bin:$PATH" ;; esac && '
            f"export CI=true && "
            f"export NPM_CONFIG_YES=true && "
            f"export DEBIAN_FRONTEND=noninteractive && "
            f"export GIT_TERMINAL_PROMPT=0 && "
            f"export NO_COLOR=1"
        )
        wrappers = (inject or {}).get("wrappers") or []
        platform_wrappers = (inject or {}).get("platform_wrappers") or []
        launchers = prepare_launchers(
            [*wrappers, *platform_wrappers], ttl_seconds=context_ttl_seconds
        ) if wrappers or platform_wrappers else []
        if launchers:
            setup_lines = [build_launcher_write_sh(item) for item in launchers]
            script_lines.extend([
                "__aio_setup_cli() {\n  "
                + "\n  ".join(f"{line} || return $?" for line in setup_lines)
                + "\n}",
                "__aio_setup_cli\n__aio_setup_status=$?\nunset -f __aio_setup_cli\n"
                "[ $__aio_setup_status -eq 0 ] || return $__aio_setup_status",
            ])
        user_cmd = cls._build_shell_command(
            code, language, background_job=background_job
        )
        if output_log:
            # The old per-job wrapper directory used to create this parent as a
            # side effect. Logs now have their own cache namespace, so create it
            # explicitly before the scoped user process starts.
            script_lines.append(f'mkdir -p "$(dirname "{output_log}")"')
            # AIO 1.9.3 does not expose incremental stdout through /shell/view.
            # Mirror both streams into the per-Job runtime directory, which is
            # on the same Agent HOME bind mount visible to backend and sandbox.
            # Process substitution preserves the user's real exit status.
            user_cmd = (
                "{\n"
                f"{user_cmd}\n"
                f'}} > >(tee -a "{output_log}") 2>&1'
            )
        if launchers:
            user_b64 = base64.b64encode(user_cmd.encode()).decode()
            scope_lines: list[str] = []
            cleanup_lines: list[str] = []
            scoped_bindirs: list[str] = []
            for item in launchers:
                scope_lines.append(
                    f"local -x {item['context_env']}={shell_quote(item['context_token'])}"
                )
                launcher_relpath = item.get("launcher_relpath")
                if launcher_relpath:
                    bindir = launcher_relpath.rsplit("/", 1)[0]
                    if bindir not in scoped_bindirs:
                        scoped_bindirs.append(bindir)
                scope_root = item.get("scope_root_relpath")
                if scope_root:
                    cleanup_lines.extend(
                        [
                            f'rm -f "$HOME/{scope_root}/bin/toolscall"',
                            (
                                f'rmdir "$HOME/{scope_root}/bin" '
                                f'"$HOME/{scope_root}" 2>/dev/null || true'
                            ),
                        ]
                    )
            if scoped_bindirs:
                path_prefix = ":".join(
                    f"$HOME/{bindir}" for bindir in scoped_bindirs
                )
                scope_lines.append(f'local -x PATH="{path_prefix}:$PATH"')
            local_contexts = "\n  ".join(scope_lines)
            cleanup = "\n  ".join(cleanup_lines)
            if cleanup:
                cleanup = "\n  __aio_user_status=$?\n  " + cleanup + "\n  return $__aio_user_status"
            script_lines.append(
                "__aio_exec_scope() {\n  " + local_contexts + "\n  "
                f"source <(echo {user_b64} | base64 -d)\n"
                + cleanup
                + "\n"
                "}\n__aio_exec_scope\n__aio_exec_status=$?\n"
                "unset -f __aio_exec_scope\nreturn $__aio_exec_status"
            )
            script = "\n".join(script_lines)
        else:
            script = "\n".join(script_lines) + "\n" + user_cmd
        # Deliver verbatim: single-line base64 transport (no newlines for the
        # sandbox command-splitter to mangle) → decoded script is sourced into
        # the session shell exactly as written (comments / heredocs / multi-line
        # preserved); identity only exists in function-local signed contexts,
        # so it cannot leak across senders / conversations.
        b64 = base64.b64encode(script.encode()).decode()
        return f"source <(echo {b64} | base64 -d)"

    @staticmethod
    def _build_shell_command(
        code: str, language: str, *, background_job: bool = False
    ) -> str:
        if background_job and language == "python":
            # Foreground Python remains a persistent Jupyter kernel. Background
            # Python is a normal process in the job's dedicated AIO shell so it
            # has the same lifecycle/status/log handling as bash and node jobs.
            import secrets

            delim = "AIOSB_PYTHON_" + secrets.token_hex(16).upper()
            return f"python -u <<'{delim}'\n{code}\n{delim}"
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
        inject: dict | None = None,
    ) -> ExecutionResult:
        # Ensure we have a real UUID session for this anchor, and that the
        # kernel's HOME env is pinned to the agent root (the underlying
        # container has HOME=/home/gem which would be shared across agents;
        # we want per-agent ~/.ssh / ~/.gitconfig semantics, matching the
        # shell tool). The HOME pin runs as a silent setup call the very
        # first time we create a kernel for this anchor — subsequent user
        # cells therefore start at line 1 with clean traceback line numbers.
        session_uuid = await self._ensure_jupyter_session(client, anchor, cwd)

        wrappers = (inject or {}).get("wrappers") or []
        platform_wrappers = (inject or {}).get("platform_wrappers") or []
        all_wrappers = [*wrappers, *platform_wrappers]
        if all_wrappers:
            from app.services.cli_tools.sandbox_inject import build_python_execution

            code = build_python_execution(
                all_wrappers, code, ttl_seconds=timeout + 60
            )

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
        is_timeout = status == "timeout"
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
                lines = [line for line in "\n".join(stderr_parts).splitlines() if line.strip()]
                candidates = [line for line in lines if set(line.strip()) - {"-", "="}]
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
            exit_code=0 if ok_run else (124 if is_timeout else 1),
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
