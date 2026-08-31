from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional
import uuid

from loguru import logger


async def _agentbay_file_transfer(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Transfer a file between workspace and an AgentBay environment, or between two environments.

    Supported transfer directions:
      - workspace  → env:      upload_file(local_workspace_path, remote_path)   [single SDK call]
      - env        → workspace: download_file(remote_path, local_workspace_path) [single SDK call]
      - env A      → env B:    download to /tmp/<uuid>, upload to env B, cleanup /tmp [transparent]

    The 'local' side of the SDK calls is always the platform backend server,
    which has access to the agent workspace directory.
    """
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    from_type = arguments.get("from_type", "")
    from_path = arguments.get("from_path", "")
    to_type = arguments.get("to_type", "")
    to_path = arguments.get("to_path", "")
    session_id = arguments.pop("_session_id", "")

    if not all([from_type, from_path, to_type, to_path]):
        return "Missing required parameters: from_type, from_path, to_type, to_path"

    # Reject no-op transfers
    if from_type == "workspace" and to_type == "workspace":
        return "Cannot transfer workspace → workspace. Use write_file or workspace tools instead."
    if from_type == to_type and from_type != "workspace":
        return f"Same environment ({from_type}) transfer: use agentbay_command_exec with 'cp' to copy files within the same environment."

    env_types = {"browser", "computer", "code"}

    # ── Helper: resolve and validate a workspace-relative path ──────────────
    def resolve_workspace(rel_path: str) -> tuple[str | None, str]:
        """Return (absolute_local_path_str, error_message). error_message is '' on success."""
        local = (ws / rel_path).resolve()
        if not str(local).startswith(str(ws.resolve())):
            return None, "Permission denied: path must be inside the agent workspace"
        return str(local), ""

    try:
        # ── Case 1: workspace → env ──────────────────────────────────────────
        if from_type == "workspace" and to_type in env_types:
            local_path, err = resolve_workspace(from_path)
            if err:
                return err
            import os

            if not os.path.exists(local_path):
                return f"File not found in workspace: {from_path}"
            client = await get_agentbay_client_for_agent(agent_id, to_type, session_id=session_id)
            result = await asyncio.to_thread(client._session.file_system.upload_file, local_path, to_path)
            if result.success:
                msg = f"Transferred workspace/{from_path} → [{to_type}]{to_path} ({result.bytes_sent} bytes)"
                # After uploading to the computer desktop directory, notify the GNOME
                # file manager so the file icon appears immediately without manual refresh.
                desktop_dir = "/home/wuying/桌面"
                if to_type == "computer" and to_path.startswith(desktop_dir):
                    try:
                        await asyncio.to_thread(
                            client._session.command.exec, f"DISPLAY=:0 gio info '{to_path}' 2>/dev/null || true"
                        )
                    except Exception:
                        pass  # Non-critical: desktop refresh failure doesn't affect transfer result
                return msg
            return f"Upload failed: {result.error_message}"

        # ── Case 2: env → workspace ──────────────────────────────────────────
        elif from_type in env_types and to_type == "workspace":
            local_path, err = resolve_workspace(to_path)
            if err:
                return err
            import os

            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            client = await get_agentbay_client_for_agent(agent_id, from_type, session_id=session_id)
            result = await asyncio.to_thread(client._session.file_system.download_file, from_path, local_path)
            if result.success:
                return (
                    f"Transferred [{from_type}]{from_path} → workspace/{to_path} "
                    f"({result.bytes_received} bytes). "
                    f"File available in workspace at: {to_path}"
                )
            return f"Download failed: {result.error_message}"

        # ── Case 3: env A → env B (transparent /tmp/ intermediary) ──────────
        elif from_type in env_types and to_type in env_types:
            import uuid as _uuid
            import os

            tmp_path = f"/tmp/agentbay_transfer_{_uuid.uuid4().hex}"
            try:
                # Step 1: download from source env to backend /tmp/
                src_client = await get_agentbay_client_for_agent(agent_id, from_type, session_id=session_id)
                dl_result = await asyncio.to_thread(src_client._session.file_system.download_file, from_path, tmp_path)
                if not dl_result.success:
                    return f"Transfer failed (download from {from_type}): {dl_result.error_message}"

                # Step 2: upload from backend /tmp/ to destination env
                dst_client = await get_agentbay_client_for_agent(agent_id, to_type, session_id=session_id)
                ul_result = await asyncio.to_thread(dst_client._session.file_system.upload_file, tmp_path, to_path)
                if not ul_result.success:
                    return f"Transfer failed (upload to {to_type}): {ul_result.error_message}"

                return f"Transferred [{from_type}]{from_path} → [{to_type}]{to_path} ({dl_result.bytes_received} bytes)"
            finally:
                # Always clean up the temporary file regardless of success or failure
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass  # Non-critical: ignore cleanup errors

        else:
            return f"Unsupported transfer: {from_type} → {to_type}"

    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] File transfer failed for agent {agent_id}")
        return f"File transfer failed: {str(e)[:200]}"


__all__ = [
    "_agentbay_file_transfer",
]
