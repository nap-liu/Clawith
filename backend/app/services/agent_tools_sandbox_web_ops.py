"""Sandbox selection, browser tools, and CLI injection support."""

import base64
import json
import re
import uuid
from pathlib import Path
from typing import Optional

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.services.agent_tools_config_runtime import _get_tool_config
from app.services.tool_enablement import agent_tool_enabled, tool_visibility_clause
from app.services.turn_tool_settings import effective_assignment, current_tool_settings

_STDOUT_RPA_LIMIT = 20000



# Dangerous patterns to block (for legacy fallback)
_DANGEROUS_BASH_ALWAYS = [
    "rm -rf /",
    "rm -rf ~",
    "sudo ",
    "mkfs",
    "dd if=",
    ":(){ :",
    "chmod 777 /",
    "chown ",
    "shutdown",
    "reboot",
]

_DANGEROUS_BASH_NETWORK = [
    "curl ",
    "wget ",
    "nc ",
    "ncat ",
    "ssh ",
    "scp ",
]

_DANGEROUS_PYTHON_IMPORTS_ALWAYS = [
    "shutil.rmtree",
    "os.system",
    "os.popen",
    "os.exec",
    "os.spawn",
]

_DANGEROUS_PYTHON_IMPORTS_NETWORK = [
    "socket",
    "http.client",
    "urllib.request",
    "requests",
    "ftplib",
    "smtplib",
    "telnetlib",
    "ctypes",
]

_DANGEROUS_NODE_ALWAYS = [
    "fs.rmSync",
    "fs.rmdirSync",
    "process.exit",
]

_DANGEROUS_NODE_NETWORK = [
    "require('http')",
    "require('https')",
    "require('net')",
]


def _check_code_safety(language: str, code: str, allow_network: bool = False) -> str | None:
    """Check code for dangerous patterns. Returns error message if unsafe, None if ok."""
    code_lower = code.lower()

    if language == "bash":
        for pattern in _DANGEROUS_BASH_ALWAYS:
            if pattern.lower() in code_lower:
                return f"❌ Blocked: dangerous command detected ({pattern.strip()})"
        if not allow_network:
            for pattern in _DANGEROUS_BASH_NETWORK:
                if pattern.lower() in code_lower:
                    return f"❌ Blocked: network command not allowed ({pattern.strip()})"
        if "../../" in code:
            return "❌ Blocked: directory traversal not allowed"

    elif language == "python":
        for pattern in _DANGEROUS_PYTHON_IMPORTS_ALWAYS:
            if pattern.lower() in code_lower:
                return f"❌ Blocked: unsafe operation detected ({pattern})"
        if not allow_network:
            for pattern in _DANGEROUS_PYTHON_IMPORTS_NETWORK:
                if pattern.lower() in code_lower:
                    return f"❌ Blocked: network operation not allowed ({pattern})"

    elif language == "node":
        for pattern in _DANGEROUS_NODE_ALWAYS:
            if pattern.lower() in code_lower:
                return f"❌ Blocked: unsafe operation detected ({pattern})"
        if not allow_network:
            for pattern in _DANGEROUS_NODE_NETWORK:
                if pattern.lower() in code_lower:
                    return f"❌ Blocked: network operation not allowed ({pattern})"

    return None


async def build_cli_injection(
    agent_id: Optional[uuid.UUID],
    user_id: Optional[uuid.UUID],
    only_tool_names: Optional[set[str]] = None,
) -> Optional[dict]:
    """Build the per-exec CLI injection used to create signed contexts.

    Returns ``{"wrappers": [{"name", "binary_path", "env": {...}}, ...]}`` or
    None when the agent has no enabled CLI tool with a binary (the common
    case — zero overhead for non-CLI deployments). The backend signs each
    tool's resolved ``env`` into a short-lived, process-scoped context. Static
    launchers under ``$HOME/.local/bin`` contain no identity, so identity cannot
    persist across senders in a shared conversation (see sandbox_inject).

    Identity binding follows the call origin, via the `user_id` the caller
    threads in:
      - web / IM channels → the live conversation user;
      - trigger / cron / Aware loop (heartbeat passes ``agent.creator_id``)
        → the agent's creator — the digital employee acts on its owner's
        behalf, using the owner's data permissions (cron reports rely on this);
      - A2A consult (passes the source agent's ``owner_id``) → the source
        agent's creator.
    The resolved identity (phone / tokens / $state.dir) rides in each execution
    context. Only when ``user_id`` is None or the User row is missing are the
    identity entries ($user.*/$state.*) dropped. A launcher invoked outside the
    current signed execution scope fails closed rather than inheriting a prior
    sender's identity.

    ``only_tool_names`` restricts to those CLI tool names (standalone CLI-tool
    LLM functions inject just their own tool); None → all visible CLI tools
    (execute_code_aio).
    """
    try:
        from app.models.tool import Tool, AgentTool
        from app.models.user import User
        from app.services.cli_tools.placeholders import PlaceholderContext
        from app.services.cli_tools.sandbox_inject import _FUNC_NAME_RE, _TOOL_NAME_RE, render_env
        from app.services.cli_tools.schema import CliToolConfig
        from app.services.cli_tools.state_storage import StateStorage

        async with async_session() as db:
            agent_tenant_id = None
            assignments = {}
            assigned_tool_ids: list[uuid.UUID] = []
            if agent_id:
                tid_r = await db.execute(select(AgentModel.tenant_id).where(AgentModel.id == agent_id))
                _raw_tid = tid_r.scalar_one_or_none()
                # Coerce to UUID object: production (Postgres) returns UUID,
                # test stubs (SQLite TEXT column) return a plain string.
                if isinstance(_raw_tid, str):
                    _raw_tid = uuid.UUID(_raw_tid)
                agent_tenant_id = _raw_tid
                at_r = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))
                assignments = {str(at.tool_id): at for at in at_r.scalars().all()}
                assigned_tool_ids = [uuid.UUID(tid) for tid in assignments]

            cli_q = select(Tool).where(
                Tool.type == "cli", Tool.enabled.is_(True),
                tool_visibility_clause(agent_tenant_id, assigned_tool_ids),
            )
            if only_tool_names:
                cli_q = cli_q.where(Tool.name.in_(only_tool_names))
            tools_r = await db.execute(cli_q)
            cli_tools = list(tools_r.scalars().all())
            if not cli_tools:
                return None

            user_ctx: dict[str, str] = {}
            if user_id:
                user = await db.get(User, user_id)
                if user:
                    user_ctx = {
                        "id": str(user.id),
                        "phone": str(user.primary_mobile or ""),
                        "email": str(user.email or ""),
                    }

        from app.services.cli_tools.storage import BINARY_ROOT, BinaryStorage

        binary_storage = BinaryStorage(root=BINARY_ROOT)
        state_storage = StateStorage()
        wrappers: list[dict] = []
        for tool in cli_tools:
            if tool.name == "toolscall":
                logger.warning("[CLI Inject] skip reserved platform command: toolscall")
                continue
            at = effective_assignment(agent_id, tool, assignments.get(str(tool.id)))
            if not agent_tool_enabled(at):
                continue
            # Skip tools whose name isn't a safe PATH/function name, or whose env
            # keys aren't strict shell identifiers (env keys → `export KEY=`, so
            # they stay dash-free; the tool name may carry a dash, e.g. my-cli).
            scope = current_tool_settings(agent_id)
            cfg = CliToolConfig.model_validate(scope.configs.get(tool.name, {}) if scope else tool.config or {})
            if not _TOOL_NAME_RE.fullmatch(tool.name) or any(
                not _FUNC_NAME_RE.fullmatch(k) for k in cfg.env
            ):
                logger.warning(f"[CLI Inject] skip tool {tool.name}: unsafe name or env key")
                continue
            if not cfg.binary.sha256:
                continue  # no binary uploaded yet
            tenant_key = str(tool.tenant_id) if tool.tenant_id is not None else "_global"
            binary_path = binary_storage.resolve(tenant_key, str(tool.id), cfg.binary.sha256)

            state_ctx: dict[str, str] = {}
            needs_state = any(v == "$state.dir" for v in cfg.env.values())
            if needs_state and user_id:
                leaf = state_storage.ensure_home(tenant_id=tool.tenant_id, tool_id=tool.id, user_id=user_id)
                state_ctx = {"dir": str(leaf)}

            ctx = PlaceholderContext(
                user=user_ctx,
                agent={"id": str(agent_id)} if agent_id else {},
                tenant={"id": tenant_key if tenant_key != "_global" else ""},
                state=state_ctx,
            )
            # Keep each tool's resolved env separate. The AIO backend signs it
            # into that tool's process-local context; it is never merged into a
            # persistent shell/Jupyter environment.
            wrappers.append(
                {
                    "name": tool.name,
                    "binary_path": str(binary_path),
                    "env": render_env(cfg.env, ctx),
                }
            )
        if not wrappers:
            return None
        return {"wrappers": wrappers}
    except Exception:
        logger.exception("[CLI Inject] injection build failed; continuing without CLI")
        return None


async def _resolve_sandbox_backend(agent_id: Optional[uuid.UUID], tool_name: str):
    """Resolve the aio-sandbox backend + config for a browser tool.

    Reads the tool's own config row, falling back to the platform sandbox
    config. Raises ValueError if no sandbox is configured.
    """
    from app.config import get_sandbox_config
    from app.services.sandbox.config import SandboxConfig
    from app.services.sandbox.registry import get_sandbox_backend

    fallback_config = get_sandbox_config()
    tool_config = await _get_tool_config(agent_id, tool_name)
    sandbox_config = SandboxConfig.from_dict(tool_config, fallback_config) if tool_config else fallback_config
    return get_sandbox_backend(sandbox_config), sandbox_config


async def _browse(
    agent_id: Optional[uuid.UUID],
    ws: Path,
    arguments: dict,
    *,
    user_id: Optional[uuid.UUID] = None,
    session_id: Optional[str] = None,
) -> str:
    """Browse a URL via the aio-sandbox backend and return an LLM-facing summary.

    Resolves the sandbox backend from the 'browse' tool config (falling back to
    the platform-level config), calls AioSandboxBackend.browse(), writes any
    screenshot into the agent workspace, and returns a text summary.
    """
    url = (arguments.get("url") or "").strip()
    if not url:
        return "❌ browse: 'url' is required."
    extract = arguments.get("extract", True)
    screenshot = bool(arguments.get("screenshot", False))

    try:
        backend, sandbox_config = await _resolve_sandbox_backend(agent_id, "browse")
    except ValueError as e:
        return f"❌ browse: sandbox not configured: {str(e)[:200]}"

    result = await backend.browse(
        agent_id=str(agent_id) if agent_id else None,
        conversation_id=session_id or None,
        url=url,
        extract=bool(extract),
        screenshot=screenshot,
        timeout=sandbox_config.max_timeout,
    )
    if not result.get("success"):
        return f"❌ browse failed: {result.get('error') or 'unknown error'}"

    lines = [f"# {result.get('title') or url}", f"URL: {result.get('url') or url}"]
    if screenshot and result.get("screenshot_b64"):
        slug = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[:40] or "page"
        name = f"screenshot-{slug}.png"
        try:
            (ws / name).write_bytes(base64.b64decode(result["screenshot_b64"]))
            lines.append(f"Screenshot saved to workspace: {name}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"(screenshot save failed: {str(e)[:100]})")
    if extract:
        text = result.get("text") or "(no extractable text)"
        if result.get("truncated"):
            text += "\n…[truncated]"
        lines.append("")
        lines.append(text)
    return "\n".join(lines)


async def _web_open(
    agent_id: Optional[uuid.UUID],
    ws: Path,
    arguments: dict,
    *,
    user_id: Optional[uuid.UUID] = None,
    session_id: Optional[str] = None,
) -> str:
    """Navigate the conversation's persistent RPA page to a URL."""
    url = (arguments.get("url") or "").strip()
    if not url:
        return "❌ web_open: 'url' is required."
    try:
        backend, cfg = await _resolve_sandbox_backend(agent_id, "web_open")
    except ValueError as e:
        return f"❌ web_open: sandbox not configured: {str(e)[:200]}"
    result = await backend.web_open(
        agent_id=str(agent_id) if agent_id else None,
        conversation_id=session_id or None,
        url=url,
        timeout=cfg.max_timeout,
    )
    if not result.get("success"):
        return f"❌ web_open failed: {result.get('error') or 'unknown error'}"
    return f"Opened {result.get('url') or url} — {result.get('title') or '(no title)'}"


async def _web_eval(
    agent_id: Optional[uuid.UUID],
    ws: Path,
    arguments: dict,
    *,
    user_id: Optional[uuid.UUID] = None,
    session_id: Optional[str] = None,
) -> str:
    """Run arbitrary JS in the conversation's persistent RPA page."""
    expression = (arguments.get("expression") or "").strip()
    if not expression:
        return "❌ web_eval: 'expression' (JavaScript to run) is required."
    try:
        backend, cfg = await _resolve_sandbox_backend(agent_id, "web_eval")
    except ValueError as e:
        return f"❌ web_eval: sandbox not configured: {str(e)[:200]}"
    result = await backend.web_eval(
        agent_id=str(agent_id) if agent_id else None,
        conversation_id=session_id or None,
        expression=expression,
        timeout=cfg.max_timeout,
    )
    if not result.get("success"):
        return f"❌ web_eval failed: {result.get('error') or 'unknown error'}"
    value = result.get("result")
    if value is None:
        return "(no value)"
    if isinstance(value, str):
        return (value or "(empty string)")[:_STDOUT_RPA_LIMIT]
    try:
        return json.dumps(value, ensure_ascii=False)[:_STDOUT_RPA_LIMIT]
    except Exception:  # noqa: BLE001
        return str(value)[:_STDOUT_RPA_LIMIT]


async def _web_cdp(
    agent_id: Optional[uuid.UUID],
    ws: Path,
    arguments: dict,
    *,
    user_id: Optional[uuid.UUID] = None,
    session_id: Optional[str] = None,
) -> str:
    """Send a raw CDP command to the conversation's persistent RPA page."""
    method = (arguments.get("method") or "").strip()
    if not method:
        return "❌ web_cdp: 'method' (e.g. 'Input.dispatchMouseEvent') is required."
    params = arguments.get("params")
    if params is not None and not isinstance(params, dict):
        return "❌ web_cdp: 'params' must be an object."
    try:
        backend, cfg = await _resolve_sandbox_backend(agent_id, "web_cdp")
    except ValueError as e:
        return f"❌ web_cdp: sandbox not configured: {str(e)[:200]}"
    result = await backend.web_cdp(
        agent_id=str(agent_id) if agent_id else None,
        conversation_id=session_id or None,
        method=method,
        params=params,
        timeout=cfg.max_timeout,
    )
    if not result.get("success"):
        return f"❌ web_cdp failed: {result.get('error') or 'unknown error'}"
    try:
        return json.dumps(result.get("result"), ensure_ascii=False)[:_STDOUT_RPA_LIMIT]
    except Exception:  # noqa: BLE001
        return str(result.get("result"))[:_STDOUT_RPA_LIMIT]


async def _web_screenshot(
    agent_id: Optional[uuid.UUID],
    ws: Path,
    arguments: dict,
    *,
    user_id: Optional[uuid.UUID] = None,
    session_id: Optional[str] = None,
) -> str | dict:
    """Capture a PNG of the conversation's persistent RPA page into the workspace."""
    try:
        backend, cfg = await _resolve_sandbox_backend(agent_id, "web_screenshot")
    except ValueError as e:
        return f"❌ web_screenshot: sandbox not configured: {str(e)[:200]}"
    result = await backend.web_screenshot(
        agent_id=str(agent_id) if agent_id else None,
        conversation_id=session_id or None,
        timeout=cfg.max_timeout,
    )
    if not result.get("success"):
        return f"❌ web_screenshot failed: {result.get('error') or 'unknown error'}"
    b64 = result.get("screenshot_b64")
    if not b64:
        return "❌ web_screenshot: no image returned."
    name = f"workspace/web-screenshot-{uuid.uuid4().hex[:8]}.png"
    try:
        target = ws / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(b64))
    except Exception as e:  # noqa: BLE001
        return f"❌ web_screenshot: save failed: {str(e)[:100]}"
    return {
        "content": [
            {"type": "text", "text": f"Screenshot saved to workspace: {name}"},
            {"type": "image", "mimeType": "image/png", "data": b64},
        ],
        "isError": False,
    }

__all__ = [name for name in globals() if not name.startswith("__")]
