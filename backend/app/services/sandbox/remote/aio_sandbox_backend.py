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
  Each anchor gets a shell session keyed `clawith-{anchor}`.  The sandbox
  accepts arbitrary string IDs for shell sessions, so we can use a stable
  human-readable key.  Sessions persist across HTTP calls so `cd`,
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
import hashlib
import json
import time
from collections import OrderedDict
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
from app.services.sandbox.remote.cdp_browser import CdpConnection, open_and_extract

# Maximum stdout/stderr we surface to the caller. aio-sandbox itself caps
# raw output at 30 KB per call; we tighten that for LLM consumption.
_STDOUT_LIMIT = 10000
_STDERR_LIMIT = 5000


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


# Wrapper-dir leaf for an anchor. A hash keeps the leaf shell-safe (the anchor
# contains ':' which is the PATH separator) and stable across calls of the same
# conversation. bash form is "$HOME/.clawith-bin/<leaf>"; python expanduser form
# is "~/.clawith-bin/<leaf>" — both resolve to the same per-conversation dir.
_WRAPPER_BIN_ROOT = ".clawith-bin"


def _anchor_bindir(anchor: str, *, python: bool = False) -> str:
    leaf = hashlib.sha256(anchor.encode()).hexdigest()[:16]
    head = "~" if python else "$HOME"
    return f"{head}/{_WRAPPER_BIN_ROOT}/{leaf}"


class AioSandboxBackend(BaseSandboxBackend):
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
        # Anchors that have EVER had CLI wrappers injected. Once true, every
        # subsequent exec for that anchor must reset the wrapper dir (even an
        # inject-less exec) so a prior sender's wrapper can't linger and be run
        # under a stale identity. Pure non-CLI anchors stay out of this set so
        # they never pay the reset (and python execs keep clean line numbers).
        self._anchor_had_wrappers: set[str] = set()
        # anchor -> CDP browserContextId for the per-conversation isolated
        # browser context. Lives on this cached instance like _jupyter_sessions;
        # will be disposed in _evict_anchor (Task 5).
        self._browser_contexts: dict[str, str] = {}

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
                # Once an anchor has had wrappers, every later exec must reset the
                # wrapper dir so a prior sender's wrapper can't be reused.
                if (inject or {}).get("wrappers"):
                    self._anchor_had_wrappers.add(anchor)
                reset_wrappers = anchor in self._anchor_had_wrappers
                if language == "python":
                    result = await self._run_jupyter(
                        client, anchor=anchor, code=code, cwd=cwd, timeout=timeout,
                        inject=inject, reset_wrappers=reset_wrappers,
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
                        reset_wrappers=reset_wrappers,
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
        """Best-effort delete of an evicted anchor's sandbox sessions + wrapper dir.

        Failures are non-fatal: a session we fail to delete is reclaimed when
        the sandbox container restarts, and the anchor itself recovers via the
        existing "Session not found" recreate path if it ever becomes active
        again.
        """
        # Remove the per-conversation wrapper dir FIRST (while the session's bash
        # is still alive to run it) so a prior sender's cleartext identity wrapper
        # doesn't linger on disk. Best-effort; bounded residue if it fails.
        try:
            await self._shell_exec(
                client,
                f"clawith-{anchor}",
                f'rm -rf "{_anchor_bindir(anchor)}"',
                5,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AioSandbox] evict wrapper dir for {anchor!r} failed: {e}")
        try:
            await client.delete(
                f"{self.base_url}/v1/shell/sessions/clawith-{anchor}",
                headers=self._headers(),
                timeout=5.0,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AioSandbox] evict shell session for {anchor!r} failed: {e}")
        self._anchor_had_wrappers.discard(anchor)
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
        inject: dict | None = None,
        reset_wrappers: bool = False,
    ) -> ExecutionResult:
        session_id = f"clawith-{anchor}"
        cmd = self._compose_shell_command(
            cwd=cwd, code=code, language=language, inject=inject,
            bindir=_anchor_bindir(anchor), reset_wrappers=reset_wrappers,
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
        inject: dict | None = None,
        bindir: str | None = None,
        reset_wrappers: bool = False,
    ) -> str:
        """Compose the per-exec command and deliver it **verbatim**.

        The full script (cwd/HOME/CI exports → CLI wrapper writes → PATH prepend
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
        - **No 串台 (group IM safe)**: identity is NOT exported into the session.
          It rides inside each CLI wrapper's exec line (``exec env K='v' bin``),
          written into the per-conversation ``bindir`` and prepended to PATH.
          Every exec rewrites the wrapper with the *current* sender's identity,
          so a prior sender's identity can never persist in the session env
          (correct-by-construction). If ``inject`` couldn't be built no wrapper
          is written → the CLI is simply ``command not found``, never run under
          a stale identity (fail-safe). See sandbox_inject for the full model.

        Force-reset cwd AND HOME to the agent root on every call. The shell
        session persists across calls (so exported env vars / background
        processes survive), but the working directory + HOME are statelessly
        reset to align with execute_code (subprocess) semantics. The wrapper
        writes come AFTER the HOME reset so ``bindir`` (``$HOME/.clawith-bin/...``)
        lands under the agent root.

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
        import base64

        from app.services.cli_tools.sandbox_inject import build_wrapper_write_sh

        quoted_cwd = "'" + cwd.replace("'", "'\\''") + "'"
        script_lines: list[str] = []
        # cwd + HOME reset FIRST so the per-conversation bindir
        # (``$HOME/.clawith-bin/...``) lands under the agent root.
        script_lines.append(
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
        wrappers = (inject or {}).get("wrappers") or []
        if bindir and (wrappers or reset_wrappers):
            bindir_q = f'"{bindir}"'
            # Clear any PRIOR sender's wrappers first (they persist in the
            # `source`d session). The wrapper set must reflect THIS exec's
            # sender: no inject → empty dir → `svc` is command-not-found, never
            # a stale identity (fail-safe against group-IM impersonation).
            script_lines.append(f"rm -rf {bindir_q} && mkdir -p {bindir_q}")
            for w in wrappers:
                # Each wrapper carries its OWN tool's identity (env prefix on the
                # exec line) — never the session env. Rewritten every exec.
                script_lines.append(
                    build_wrapper_write_sh(
                        name=w["name"],
                        binary_path=w["binary_path"],
                        env=w.get("env") or {},
                        bindir=bindir_q,
                    )
                )
            # Prepend the per-conversation bindir so `svc` / pipes /
            # subprocess.run(['svc']) all resolve to the current wrapper. (PATH
            # grows by one entry per exec, like the BASE npm-global prepend.)
            script_lines.append(f'export PATH="{bindir}:$PATH"')
        user_cmd = cls._build_shell_command(code, language)
        script = "\n".join(script_lines) + "\n" + user_cmd
        # Deliver verbatim: single-line base64 transport (no newlines for the
        # sandbox command-splitter to mangle) → decoded script is sourced into
        # the session shell exactly as written (comments / heredocs / multi-line
        # preserved); identity never touches the session env (it's in the
        # wrappers), so it cannot leak across senders / conversations.
        b64 = base64.b64encode(script.encode()).decode()
        return f"source <(echo {b64} | base64 -d)"

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
        inject: dict | None = None,
        reset_wrappers: bool = False,
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
        if wrappers or reset_wrappers:
            # (Re)write the CLI PATH wrappers into the per-conversation bindir and
            # prepend it to PATH so `subprocess.run(['svc', ...])` finds svc and
            # inherits its identity (which rides INSIDE the wrapper, not the
            # kernel's os.environ — so a persistent kernel can't leak a prior
            # sender's identity). The prelude CLEARS the bindir first, so an
            # inject-less exec (reset_wrappers, no wrappers) removes a prior
            # sender's wrapper instead of letting it persist and be reused.
            # Prepended to the SAME cell as the user code: the sandbox's jupyter
            # does not persist this setup across execute calls reliably, and
            # re-running every exec keeps the current sender's identity correct.
            # (Shifts user traceback line numbers by the prelude length —
            # accepted trade-off for correct svc identity.)
            from app.services.cli_tools.sandbox_inject import build_python_prelude

            prelude = build_python_prelude(
                wrappers, _anchor_bindir(anchor, python=True)
            )
            code = prelude + "\n" + code

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

    async def _browser_ws_url(self, client: httpx.AsyncClient) -> str:
        """Resolve the browser-level CDP WebSocket URL, reachable from the backend.

        aio-sandbox exposes the bundled Chrome's CDP through its 8080 endpoint.
        `/v1/browser/info` returns a `cdp_url`; `/json/version` returns a
        `webSocketDebuggerUrl`. Either may carry an in-container host
        (localhost/127.0.0.1/0.0.0.0) that the backend cannot reach, so we
        always rewrite the host:port to our own `self.base_url`.
        """
        cdp_url = ""
        try:
            resp = await client.get(
                f"{self.base_url}/v1/browser/info",
                headers=self._headers(),
                timeout=5.0,
            )
            if resp.status_code == 200:
                cdp_url = (resp.json().get("data") or resp.json()).get("cdp_url", "") or ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AioSandbox] /v1/browser/info failed: {e}")
        if not cdp_url:
            resp = await client.get(
                f"{self.base_url}/json/version",
                headers=self._headers(),
                timeout=5.0,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"browser CDP not reachable: /json/version HTTP {resp.status_code}"
                )
            cdp_url = resp.json().get("webSocketDebuggerUrl", "") or ""
        if not cdp_url:
            raise RuntimeError("browser CDP endpoint returned no ws url")
        # Rewrite host:port to the backend-reachable base_url; keep ws path.
        base = urlsplit(self.base_url)
        cdp = urlsplit(cdp_url)
        scheme = "wss" if base.scheme == "https" else "ws"
        return urlunsplit((scheme, base.netloc, cdp.path, cdp.query, ""))

    async def _ensure_browser_context(self, conn, anchor: str, *, timeout: float) -> str:
        """Get-or-create the CDP browserContextId for an anchor."""
        ctx = self._browser_contexts.get(anchor)
        if ctx:
            return ctx
        created = await conn.call(
            "Target.createBrowserContext", {"disposeOnDetach": False}, timeout=timeout
        )
        ctx = created["browserContextId"]
        self._browser_contexts[anchor] = ctx
        return ctx

    async def browse(
        self,
        *,
        agent_id: str | None,
        conversation_id: str | None,
        url: str,
        extract: bool = True,
        screenshot: bool = False,
        timeout: int = 30,
    ) -> dict[str, Any]:
        anchor = compute_session_anchor(agent_id, conversation_id)
        try:
            async with httpx.AsyncClient() as client:
                ws_url = await self._browser_ws_url(client)
            async with websockets.connect(ws_url, max_size=20_000_000) as ws_conn:
                conn = CdpConnection(ws_conn)
                ctx = await self._ensure_browser_context(conn, anchor, timeout=float(timeout))
                out = await open_and_extract(
                    conn,
                    browser_context_id=ctx,
                    url=url,
                    want_text=extract,
                    want_screenshot=screenshot,
                    text_limit=_STDOUT_LIMIT,
                    timeout=float(timeout),
                )
            return {"success": True, "error": None, **out}
        except Exception as e:  # noqa: BLE001
            logger.exception("[AioSandbox] browse error")
            return {
                "success": False,
                "error": f"browse failed: {str(e)[:200]}",
                "url": url,
                "title": "",
                "text": "",
                "screenshot_b64": None,
                "truncated": False,
            }

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
