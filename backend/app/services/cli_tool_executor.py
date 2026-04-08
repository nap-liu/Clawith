"""CLI tool executor for Clawith.

Executes registered CLI binaries as agent tools with platform-injected
user context via environment variables.

Tool config example:
{
    "binary": "/app/svc",
    "env_inject": {
        "SVC_USER_PHONE": "$user.phone",
        "SVC_USER_ID": "$user.id"
    },
    "timeout": 30
}
"""

import asyncio
import os
import shlex
import uuid
from pathlib import Path
from typing import Optional

from loguru import logger


# Characters that could enable command injection
_DANGEROUS_CHARS = [';', '|', '&', '`', '$(', '${', '\n', '\r']


async def execute_cli_tool(
    tool_config: dict,
    arguments: dict,
    user_id: Optional[uuid.UUID] = None,
    work_dir: Optional[str] = None,
) -> str:
    """Execute a CLI tool with injected user context.

    Args:
        tool_config: Tool's config from DB (binary, env_inject, timeout)
        arguments: Tool call arguments from agent (command string)
        user_id: Current conversation user's ID
        work_dir: Working directory for the process
    """
    binary = tool_config.get("binary")
    if not binary:
        return "❌ CLI tool config missing 'binary' path"

    if not Path(binary).exists():
        return f"❌ CLI binary not found: {binary}"

    command = arguments.get("command", "").strip()
    if not command:
        return "❌ No command provided"

    timeout = tool_config.get("timeout", 30)

    # 1. Security: reject dangerous characters
    for char in _DANGEROUS_CHARS:
        if char in command:
            logger.warning(f"[CLI Tool] Blocked dangerous char in command: {repr(char)}")
            return f"❌ Command contains unsafe character: {repr(char)}"

    # 2. Resolve env_inject placeholders
    env_inject = tool_config.get("env_inject", {})
    resolved_env = {}
    if env_inject and user_id:
        user_context = await _resolve_user_context(user_id)
        for env_key, placeholder in env_inject.items():
            value = _resolve_placeholder(placeholder, user_context)
            if value is not None:
                resolved_env[env_key] = str(value)
                logger.debug(f"[CLI Tool] Injected env: {env_key}=***")

    # 3. Build safe environment
    safe_env = dict(os.environ)
    if work_dir:
        safe_env["HOME"] = work_dir
    safe_env.update(resolved_env)

    # 4. Parse command into args array (prevents shell injection)
    try:
        args = shlex.split(command)
    except ValueError as e:
        return f"❌ Invalid command syntax: {e}"

    logger.info(f"[CLI Tool] Executing: {binary} {' '.join(args[:5])}...")

    # 5. Execute
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, *args,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"❌ Command timed out after {timeout}s"

        output = stdout.decode("utf-8").strip()
        if proc.returncode != 0 and not output:
            error = stderr.decode("utf-8").strip()
            return f"❌ Command failed (exit {proc.returncode}): {error[:500]}"

        return output

    except Exception as e:
        logger.exception(f"[CLI Tool] Execution error")
        return f"❌ Execution error: {str(e)[:200]}"


async def _resolve_user_context(user_id: uuid.UUID) -> dict:
    """Look up user info from DB for placeholder resolution."""
    try:
        from app.database import async_session
        from sqlalchemy import text

        async with async_session() as db:
            result = await db.execute(
                text("SELECT i.phone, i.email FROM users u JOIN identities i ON u.identity_id = i.id WHERE u.id = :uid"),
                {"uid": str(user_id)},
            )
            row = result.first()

            return {
                "user.id": str(user_id),
                "user.phone": row[0] if row else None,
                "user.email": row[1] if row else None,
            }
    except Exception as e:
        logger.warning(f"[CLI Tool] Failed to resolve user context: {e}")
        return {"user.id": str(user_id)}


def _resolve_placeholder(placeholder: str, context: dict) -> Optional[str]:
    """Resolve $user.phone style placeholders."""
    if isinstance(placeholder, str) and placeholder.startswith("$"):
        key = placeholder[1:]
        return context.get(key)
    return placeholder
