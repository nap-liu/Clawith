from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional
import uuid

from loguru import logger


async def _agentbay_code_execute(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """在 AgentBay 代码空间执行代码。"""
    if not agent_id:
        return "❌ AgentBay 工具需要 agent 上下文"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    language = arguments.get("language", "python")
    code = arguments.get("code", "")
    timeout = arguments.get("timeout", 30)

    if not code.strip():
        return "❌ 请提供要执行的代码"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        result = await client.code_execute(language, code, timeout)

        # 格式化返回结果
        parts = [f"✅ 代码执行完成 ({language})"]
        if result.get("stdout"):
            parts.append(f"📤 输出:\n{result['stdout']}")
        if result.get("stderr"):
            parts.append(f"⚠️ 错误输出:\n{result['stderr']}")
        if result.get("exit_code") != 0:
            parts.append(f"退出码: {result['exit_code']}")

        return "\n\n".join(parts)

    except RuntimeError as e:
        return f"❌ {str(e)}。请先在 Agent 设置中配置 AgentBay 通道。"
    except Exception as e:
        logger.exception(f"[AgentBay] Code execution failed for agent {agent_id}")
        return f"❌ 代码执行失败: {str(e)[:200]}"


async def _agentbay_code_write_file(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Write a text file in the AgentBay Code Sandbox."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    remote_path = arguments.get("remote_path") or arguments.get("path") or ""
    content = arguments.get("content")
    mode = arguments.get("mode", "overwrite")

    if not remote_path.strip():
        return "Missing required argument 'remote_path'"
    if content is None:
        return "Missing required argument 'content'"
    if mode not in ("overwrite", "append"):
        return "Invalid mode. Use 'overwrite' or 'append'."

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        result = await asyncio.to_thread(
            client._session.file_system.write_file,
            remote_path,
            str(content),
            mode,
        )
        if result.success:
            byte_count = len(str(content).encode("utf-8"))
            return f"File written in AgentBay Code Sandbox: {remote_path} ({byte_count} bytes, mode={mode})"
        return f"Write failed: {result.error_message}"
    except RuntimeError as e:
        return f"{str(e)}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Code write file failed for agent {agent_id}")
        return f"Write file failed: {str(e)[:200]}"


async def _agentbay_code_read_file(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Read a text file from the AgentBay Code Sandbox."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    remote_path = arguments.get("remote_path") or arguments.get("path") or ""
    if not remote_path.strip():
        return "Missing required argument 'remote_path'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        result = await asyncio.to_thread(
            client._session.file_system.read_file,
            remote_path,
        )
        if result.success:
            content = getattr(result, "content", "") or ""
            return f"File read from AgentBay Code Sandbox: {remote_path}\n\n{content[:12000]}"
        return f"Read failed: {result.error_message}"
    except RuntimeError as e:
        return f"{str(e)}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Code read file failed for agent {agent_id}")
        return f"Read file failed: {str(e)[:200]}"


async def _agentbay_code_edit_file(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Edit a text file in the AgentBay Code Sandbox."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    remote_path = arguments.get("remote_path") or arguments.get("path") or ""
    edits = arguments.get("edits")
    dry_run = bool(arguments.get("dry_run", False))

    if not remote_path.strip():
        return "Missing required argument 'remote_path'"
    if not isinstance(edits, list) or not edits:
        return "Missing required argument 'edits'"

    normalized_edits = []
    for edit in edits:
        if not isinstance(edit, dict):
            return "Each edit must be an object with oldText and newText."
        old_text = edit.get("oldText")
        new_text = edit.get("newText")
        if old_text is None or new_text is None:
            return "Each edit must include oldText and newText."
        normalized_edits.append({"oldText": str(old_text), "newText": str(new_text)})

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        result = await asyncio.to_thread(
            client._session.file_system.edit_file,
            remote_path,
            normalized_edits,
            dry_run,
        )
        if result.success:
            action = "Previewed edits for" if dry_run else "Edited"
            return f"{action} AgentBay Code Sandbox file: {remote_path} ({len(normalized_edits)} replacement(s))"
        return f"Edit failed: {result.error_message}"
    except RuntimeError as e:
        return f"{str(e)}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Code edit file failed for agent {agent_id}")
        return f"Edit file failed: {str(e)[:200]}"


__all__ = [
    "_agentbay_code_execute",
    "_agentbay_code_write_file",
    "_agentbay_code_read_file",
    "_agentbay_code_edit_file",
]
