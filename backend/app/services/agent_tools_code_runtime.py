"""Code execution and CLI dispatch tools."""

import json
import os
import uuid
from pathlib import Path
from typing import Optional

from loguru import logger

from app.services.agent_tools_config_runtime import _get_tool_config
from app.services.agent_tools_file_support import (
    _agent_workspace_root,
    _canonicalize_execute_code_upload_paths,
)
from app.services.agent_tools_sandbox_web_ops import (
    _check_code_safety,
    build_cli_injection,
)

_REMOTE_SANDBOX_TOOL_NAMES: frozenset[str] = frozenset({"execute_code_e2b", "execute_code_aio"})


async def _execute_code(
    agent_id: Optional[uuid.UUID],
    ws: Path,
    arguments: dict,
    *,
    tool_name: str = "execute_code",
    user_id: Optional[uuid.UUID] = None,
    cli_injection: Optional[dict] = None,
    session_id: Optional[str] = None,
    turn_anchor_id: uuid.UUID | None = None,
    tools_for_llm: list[dict] | None = None,
    work_dir_override: Path | None = None,
    hardened_workspace: bool = False,
    venv_path_override: Path | None = None,
    runtime_temp_path_override: Path | None = None,
    on_output=None,
) -> str:
    """Execute code using the configured sandbox backend.

    Args:
        agent_id: The agent's UUID (used to fetch per-agent tool config).
        ws: Agent workspace root path.
        arguments: Tool call arguments (action, language, code,
                   execution_mode, timeout, job_id, tail_lines).
        tool_name: The originating tool name — 'execute_code' (local subprocess),
                   'execute_code_e2b' (E2B cloud), or 'execute_code_aio'
                   (self-hosted AIO sandbox).  Used to look up the correct
                   per-agent tool config entry in the database.
        session_id: The ChatSession id of the calling conversation. Threaded to
                    the backend as ``conversation_id`` so shell/jupyter sessions
                    are isolated per conversation, not per agent.
    """
    action = str(arguments.get("action") or "execute").strip().lower()
    execution_mode = str(arguments.get("execution_mode") or "foreground").strip().lower()
    language = arguments.get("language")
    code = arguments.get("code", "")
    if action == "execute" and code:
        code, canonicalized_uploads = _canonicalize_execute_code_upload_paths(ws, code)
        if canonicalized_uploads:
            logger.info(
                f"[Sandbox] Canonicalized {len(canonicalized_uploads)} "
                f"execute_code upload path(s): {canonicalized_uploads}"
            )

    valid_actions = {"execute", "list_jobs", "job_status", "job_logs", "job_stop"}
    if action not in valid_actions:
        return f"❌ Unsupported execute_code_aio action: {action}"
    if action != "execute" and tool_name != "execute_code_aio":
        return "❌ Background Job management is available only in execute_code_aio"
    if action == "execute" and not language:
        return "❌ language is required: python, bash, or node"
    if action == "execute" and not code.strip():
        return "❌ No code provided"
    if action == "execute" and language not in ("python", "bash", "node"):
        return f"❌ Unsupported language: {language}. Use: python, bash, or node"
    if execution_mode not in {"foreground", "background"}:
        return "❌ execution_mode must be foreground or background"
    if execution_mode == "background" and tool_name != "execute_code_aio":
        return "❌ Managed background execution is available only in execute_code_aio"

    # Working directory is the agent's root directory (must be absolute).
    # This allows code to access skills/, workspace/, memory/ etc. directly.
    #
    # Remote sandboxes (aio/e2b) run in a SEPARATE container and need a path
    # that exists INSIDE that container — not the host-only temp workspace the
    # storage-abstraction layer materializes for local subprocess execution
    # (that dir lives only on the backend host, so the sandbox's `cd`/jupyter
    # cwd hits "No such file or directory" / "Working directory does not
    # exist"). The sandbox shares the agent's REAL workspace root via the
    # /data/agents bind mount, so point work_dir there. The mkdir below lands
    # on that shared mount → the dir is real on both sides. Falls back to the
    # passed-in ws when agent_id is unknown.
    if work_dir_override is not None:
        work_dir = work_dir_override.resolve()
    elif tool_name in _REMOTE_SANDBOX_TOOL_NAMES and agent_id is not None:
        work_dir = _agent_workspace_root(agent_id).resolve()
    else:
        work_dir = ws.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    # These tools are an explicit choice of a non-subprocess sandbox; if their
    # config or runtime is broken, surface the error rather than silently
    # falling back to local subprocess execution.
    is_explicit_remote_sandbox = tool_name in _REMOTE_SANDBOX_TOOL_NAMES

    try:
        # Import here to avoid circular imports
        from app.config import get_sandbox_config
        from app.services.sandbox.config import SandboxConfig
        from app.services.sandbox.registry import get_sandbox_backend

        # Get sandbox config: prefer per-agent tool config from DB,
        # fall back to the platform-level env-var config.
        fallback_config = get_sandbox_config()
        tool_config = await _get_tool_config(agent_id, tool_name)

        if tool_config:
            sandbox_config = SandboxConfig.from_dict(tool_config, fallback_config)
        else:
            sandbox_config = fallback_config
            logger.info(f"[Sandbox] No per-agent config found for '{tool_name}', using fallback")

        if hardened_workspace:
            # Project repository code uses the existing hardened local sandbox
            # so its only writable mount is the isolated public copy. Ordinary
            # timeout, memory and network settings still come from the tool.
            from app.services.sandbox.config import SandboxType

            sandbox_config = sandbox_config.model_copy(
                update={
                    "type": SandboxType.SUBPROCESS,
                    "hardened": True,
                    "allow_unsafe_fallback_when_bwrap_missing": False,
                }
            )

        backend = get_sandbox_backend(sandbox_config)

        if action != "execute":
            if not agent_id or not session_id:
                return "❌ Background Job management requires an active chat session"
            payload = await backend.manage_background_jobs(
                action=action,
                agent_id=str(agent_id),
                conversation_id=session_id,
                job_id=arguments.get("job_id"),
                tail_lines=int(arguments.get("tail_lines") or 100),
                work_dir=str(work_dir),
            )
            icon = "✅" if payload.get("success") else "❌"
            return f"{icon} AIO background jobs:\n" + json.dumps(payload, ensure_ascii=False, indent=2)

        if execution_mode == "background":
            if not agent_id or not session_id:
                return "❌ Background execution requires an active chat session"
            background_default = int((tool_config or {}).get("background_default_timeout", 900))
            background_cap = int((tool_config or {}).get("background_max_timeout", 3600))
            requested_timeout = int(arguments.get("timeout") or background_default)
            timeout = max(1, min(requested_timeout, background_cap))
        else:
            requested_timeout = int(arguments.get("timeout") or 30)
            timeout = max(1, min(requested_timeout, sandbox_config.max_timeout))

        logger.info(
            f"[Sandbox] Executing code with backend: {backend.__class__.__name__} (tool={tool_name}, timeout={timeout}s)"
        )
        injection = None
        if cli_injection is not None:
            # Caller already built it (e.g. _execute_cli_tool, scoped to its own
            # tool) — avoid a second DB round-trip.
            injection = cli_injection
        elif tool_name == "execute_code_aio" and not hardened_workspace:
            # All languages (bash/node/python) get native CLI wrappers. The
            # current turn's ToolCall bridge follows the Agent-level switch.
            injection = await build_cli_injection(agent_id, user_id)
            from app.services.toolscall.capability import (
                toolscall_enabled_for_agent,
            )

            toolscall_enabled = toolscall_enabled_for_agent(tool_config)
            if toolscall_enabled and agent_id and user_id and tools_for_llm is not None:
                from app.services.toolscall.capability import build_toolscall_wrapper

                try:
                    toolscall_wrapper = await build_toolscall_wrapper(
                        agent_id=agent_id,
                        user_id=user_id,
                        session_id=session_id or "",
                        turn_anchor_id=turn_anchor_id,
                        tools_for_llm=tools_for_llm,
                        aio_base_url=str(getattr(backend, "base_url", "")),
                        ttl_seconds=timeout + 60,
                        native_tool_names={
                            str(wrapper.get("name") or "")
                            for wrapper in (injection or {}).get("wrappers", [])
                            if wrapper.get("name")
                        },
                    )
                except Exception:
                    # Tool composition is an execution convenience. A metadata
                    # lookup failure must not turn unrelated sandbox code into
                    # a failed ToolCall; the missing command remains explicit
                    # if the submitted code actually tries to invoke it.
                    logger.exception("[Toolscall] launcher setup failed; continuing without it")
                    toolscall_wrapper = None
                if toolscall_wrapper:
                    injection = injection or {}
                    injection.setdefault("platform_wrappers", []).append(toolscall_wrapper)

        if execution_mode == "background":
            payload = await backend.start_background_job(
                code=code,
                language=language,
                timeout=timeout,
                work_dir=str(work_dir),
                agent_id=str(agent_id),
                conversation_id=session_id,
                inject=injection,
            )
            icon = "✅" if payload.get("success") else "❌"
            return f"{icon} AIO background job:\n" + json.dumps(payload, ensure_ascii=False, indent=2)

        result = await backend.execute(
            code=code,
            language=language,
            timeout=timeout,
            work_dir=str(work_dir),
            agent_id=None if hardened_workspace else (str(agent_id) if agent_id else None),
            conversation_id=session_id or None,
            inject=injection,
            venv_path_override=(str(venv_path_override) if venv_path_override else None),
            runtime_temp_path_override=(
                str(runtime_temp_path_override) if runtime_temp_path_override else None
            ),
            on_output=on_output,
        )

        # Format result for user display
        return backend._format_result(result)

    except ValueError as e:
        # Sandbox disabled or misconfigured
        if hardened_workspace or is_explicit_remote_sandbox:
            # Project repository execution and explicit remote sandboxes are
            # fail-closed. Falling back would run project code directly on the
            # backend host with access beyond the isolated public snapshot.
            return f"❌ Sandbox configuration error: {str(e)[:300]}\nPlease check the tool settings."
        logger.warning(f"[Sandbox] Config issue, falling back to legacy subprocess: {e}")
        return await _execute_code_legacy(
            ws,
            arguments,
            allow_network=fallback_config.allow_network,
            max_timeout=fallback_config.max_timeout,
            on_output=on_output,
        )

    except Exception as e:
        logger.exception(f"[Sandbox] Execution failed for agent {agent_id} (tool={tool_name})")
        if hardened_workspace or is_explicit_remote_sandbox:
            # Never turn an isolation failure into unsandboxed host execution.
            return f"❌ Sandbox execution error: {str(e)[:200]}"
        # For local tool: try legacy subprocess as last resort
        try:
            return await _execute_code_legacy(
                ws,
                arguments,
                allow_network=sandbox_config.allow_network,
                max_timeout=sandbox_config.max_timeout,
                on_output=on_output,
            )
        except Exception:
            logger.exception(f"[Sandbox] Fallback also failed for agent {agent_id}")
            return f"❌ Execution error: {str(e)[:200]}"


async def _is_cli_tool_name(agent_id: Optional[uuid.UUID], tool_name: str) -> bool:
    """True if ``tool_name`` is an enabled type='cli' tool in the agent's tenant.

    ``tool.name`` is globally unique, so a tenant-scoped name match is a reliable
    existence signal. Used by ``_execute_cli_tool`` to tell "not a CLI tool"
    (→ fall through to MCP) apart from "is a CLI tool but couldn't be built"
    (→ surface a clear error instead of a confusing 'Unknown tool').
    """
    try:
        from app.database import async_session
        from app.models.agent import Agent as AgentModel
        from app.models.tool import Tool
        from sqlalchemy import or_, select

        async with async_session() as db:
            tenant_id = None
            if agent_id:
                r = await db.execute(select(AgentModel.tenant_id).where(AgentModel.id == agent_id))
                tenant_id = r.scalar_one_or_none()
            q = (
                select(Tool.id)
                .where(
                    Tool.name == tool_name,
                    Tool.type == "cli",
                    Tool.enabled == True,  # noqa: E712
                    or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
                )
                .limit(1)
            )
            return (await db.execute(q)).scalar_one_or_none() is not None
    except Exception:
        logger.exception(f"[CLI] _is_cli_tool_name check failed for {tool_name}")
        return False


async def _execute_cli_tool(
    agent_id: Optional[uuid.UUID],
    ws: Path,
    tool_name: str,
    arguments: dict,
    *,
    user_id: Optional[uuid.UUID] = None,
    session_id: Optional[str] = None,
) -> Optional[str]:
    """Run a standalone CLI tool (type='cli') as its own LLM function.

    Returns None when ``tool_name`` is not a CLI tool for this agent — the
    dispatcher then falls through to MCP. Otherwise runs the user-supplied
    ``command`` as bash in the aio sandbox with ONLY this tool injected
    (wrapper + identity), reusing _execute_code's sandbox / work_dir / timeout /
    error path.
    """
    injection = await build_cli_injection(agent_id, user_id, only_tool_names={tool_name})
    if not injection:
        # None means: tool_name isn't a CLI tool (→ MCP), OR it IS a surfaced CLI
        # tool that couldn't be built now (binary deleted mid-conversation, or a
        # transient error swallowed by build_cli_injection). Distinguish so the
        # latter gets a clear error, not a confusing 'Unknown tool'.
        if await _is_cli_tool_name(agent_id, tool_name):
            return (
                f"❌ CLI tool '{tool_name}' is currently unavailable "
                f"(binary not found or could not be prepared). Check the tool's binary upload."
            )
        return None  # genuinely not a CLI tool → let dispatcher try MCP
    command = (arguments.get("command") or "").strip()
    if not command:
        return f"❌ Missing required parameter 'command' for CLI tool '{tool_name}'."
    return await _execute_code(
        agent_id,
        ws,
        {"language": "bash", "code": command},
        tool_name="execute_code_aio",
        user_id=user_id,
        cli_injection=injection,
        session_id=session_id,
    )


async def _execute_code_legacy(
    ws: Path, arguments: dict, allow_network: bool = False, max_timeout: int = 60, on_output=None
) -> str:
    """Legacy subprocess-based code execution (fallback)."""
    import asyncio

    language = arguments.get("language", "python")
    code = arguments.get("code", "")
    timeout = min(arguments.get("timeout", 30), max_timeout)

    if not code.strip():
        return "❌ No code provided"

    if language not in ("python", "bash", "node"):
        return f"❌ Unsupported language: {language}. Use: python, bash, or node"

    # Security check
    safety_error = _check_code_safety(language, code, allow_network)
    if safety_error:
        return safety_error

    # Working directory is the agent's root directory (must be absolute)
    # This allows code to access skills/, workspace/, memory/ etc. directly
    work_dir = ws.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    # Determine command and file extension
    if language == "python":
        ext = ".py"
        cmd_prefix = ["python3"]
    elif language == "bash":
        ext = ".sh"
        cmd_prefix = ["bash"]
    elif language == "node":
        ext = ".js"
        cmd_prefix = ["node"]
    else:
        return f"❌ Unsupported language: {language}"

    # Write code to a temp file inside workspace
    script_path = work_dir / f"_exec_tmp{ext}"
    try:
        script_path.write_text(code, encoding="utf-8")

        # Inherit parent environment but override HOME to workspace
        safe_env = dict(os.environ)
        safe_env["HOME"] = str(work_dir)
        safe_env["PYTHONDONTWRITEBYTECODE"] = "1"

        proc = await asyncio.create_subprocess_exec(
            *cmd_prefix,
            str(script_path),
            cwd=str(work_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
        )

        stdout_data = bytearray()
        stderr_data = bytearray()

        async def read_stream(stream, out, label="stdout"):
            capture_limit = MAX_EXEC_STDERR_CAPTURE_BYTES if label == "stderr" else MAX_EXEC_STDOUT_CAPTURE_BYTES
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                remaining = capture_limit - len(out)
                if remaining > 0:
                    out.extend(chunk[:remaining])
                # Real-time streaming: push each chunk to the WebSocket
                if on_output:
                    try:
                        text = chunk.decode("utf-8", errors="replace")
                        await on_output(text, label)
                    except Exception:
                        pass

        task1 = asyncio.create_task(read_stream(proc.stdout, stdout_data, "stdout"))
        task2 = asyncio.create_task(read_stream(proc.stderr, stderr_data, "stderr"))

        is_timeout = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            is_timeout = True

        await asyncio.gather(task1, task2)
        stdout = bytes(stdout_data)
        stderr = bytes(stderr_data)

        stdout_str = stdout.decode("utf-8", errors="replace")[:10000] if stdout else ""
        stderr_str = stderr.decode("utf-8", errors="replace")[:5000] if stderr else ""

        result_parts = []
        if stdout_str.strip():
            result_parts.append(f"📤 Output:\n{stdout_str}")
        if stderr_str.strip():
            result_parts.append(f"⚠️ Stderr:\n{stderr_str}")

        if is_timeout:
            result_parts.append(
                f"❌ Code execution timed out after {timeout}s. If you expect this code to take longer, try calling the tool again with a higher 'timeout' parameter (up to 3600s)."
            )
            return "\n\n".join(result_parts)

        if proc.returncode != 0:
            result_parts.append(f"Exit code: {proc.returncode}")

        if not result_parts:
            return "✅ Code executed successfully (no output)"

        return "\n\n".join(result_parts)

    except Exception as e:
        return f"❌ Execution error: {str(e)[:200]}"
    finally:
        # Clean up temp script
        try:
            script_path.unlink(missing_ok=True)
        except Exception:
            pass

__all__ = [name for name in globals() if not name.startswith("__")]
