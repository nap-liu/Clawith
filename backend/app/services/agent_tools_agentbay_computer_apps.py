from __future__ import annotations

from pathlib import Path
from typing import Optional
import uuid

from loguru import logger


def _agentbay_normalize_text(value) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _agentbay_app_field(app: dict, *keys: str) -> str:
    for key in keys:
        value = app.get(key)
        if value:
            return str(value)
    return ""


def _agentbay_format_apps(apps: list, limit: int = 40) -> str:
    import json

    if not apps:
        return "[]"
    compact_apps = []
    for app in apps[:limit]:
        if isinstance(app, dict):
            compact_apps.append(
                {
                    key: app.get(key)
                    for key in (
                        "name",
                        "start_cmd",
                        "startCmd",
                        "work_directory",
                        "workDirectory",
                        "stop_cmd",
                        "stopCmd",
                    )
                    if app.get(key)
                }
            )
        else:
            compact_apps.append(str(app))
    rendered = json.dumps(compact_apps, ensure_ascii=False, indent=2)
    if len(apps) > limit:
        rendered += f"\n... {len(apps) - limit} more app(s) omitted"
    return rendered[:5000]


def _agentbay_find_installed_app_match(query: str, apps: list) -> tuple[dict | None, float]:
    from difflib import SequenceMatcher

    query_norm = _agentbay_normalize_text(query.split()[0] if query else query)
    if not query_norm:
        return None, 0.0

    best_app = None
    best_score = 0.0
    for app in apps:
        if not isinstance(app, dict):
            continue
        fields = [
            _agentbay_app_field(app, "name"),
            _agentbay_app_field(app, "start_cmd", "startCmd"),
            _agentbay_app_field(app, "work_directory", "workDirectory"),
        ]
        for field in fields:
            field_norm = _agentbay_normalize_text(field)
            if not field_norm:
                continue
            if query_norm == field_norm:
                score = 1.0
            elif query_norm in field_norm or field_norm in query_norm:
                score = 0.9
            else:
                score = SequenceMatcher(None, query_norm, field_norm).ratio()
            if score > best_score:
                best_app, best_score = app, score

    return best_app, best_score


def _agentbay_uncertain_start_error(error_message: str) -> bool:
    text = (error_message or "").lower()
    return "may have launched" in text or "no processes found" in text


async def _agentbay_visible_apps_note(client) -> str:
    try:
        visible = await client.computer_list_visible_apps()
        if visible.get("success"):
            apps = visible.get("apps", [])
            return (
                f"Visible applications after the launch attempt ({len(apps)}):\n{_agentbay_format_apps(apps, limit=20)}"
            )
        return f"Could not verify visible applications: {visible.get('error_message', 'Unknown error')}"
    except Exception as e:
        logger.debug(f"[AgentBay] Could not list visible apps after start_app: {e}")
        return f"Could not verify visible applications: {str(e)[:200]}"


async def _agentbay_computer_start_app(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Start an application on the desktop."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    cmd = arguments.get("cmd", "")
    work_dir = arguments.get("work_dir", "")

    if not cmd.strip():
        return "Missing required argument 'cmd'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_start_app(cmd, work_dir=work_dir)
        if result.get("success"):
            # result.data may contain non-serializable objects (e.g. Process),
            # so convert to string safely instead of json.dumps()
            data = result.get("data")
            if data is not None:
                try:
                    import json

                    data_str = (
                        json.dumps(data, ensure_ascii=False, indent=2)
                        if isinstance(data, (dict, list, str, int, float, bool))
                        else str(data)
                    )
                except (TypeError, ValueError):
                    data_str = str(data)
            else:
                data_str = ""
            return f"Application started: {cmd}" + (f"\n\n{data_str[:1000]}" if data_str else "")

        direct_error = result.get("error_message", "Unknown error")
        installed_note = ""
        try:
            installed_result = await client.computer_get_installed_apps()
            if installed_result.get("success"):
                apps = installed_result.get("apps", [])
                matched_app, score = _agentbay_find_installed_app_match(cmd, apps)
                if matched_app and score >= 0.58:
                    matched_name = _agentbay_app_field(matched_app, "name") or "(unnamed app)"
                    matched_cmd = _agentbay_app_field(matched_app, "start_cmd", "startCmd")
                    matched_work_dir = _agentbay_app_field(matched_app, "work_directory", "workDirectory") or work_dir
                    if matched_cmd and matched_cmd.strip() != cmd.strip():
                        retry = await client.computer_start_app(matched_cmd, work_dir=matched_work_dir)
                        if retry.get("success"):
                            retry_data = retry.get("data")
                            retry_data_str = str(retry_data)[:1000] if retry_data is not None else ""
                            return (
                                f"Direct start command failed: {cmd}\n"
                                f"Matched installed app: {matched_name} (score={score:.2f})\n"
                                f"Retried with start_cmd: {matched_cmd}\n"
                                f"Application started." + (f"\n\n{retry_data_str}" if retry_data_str else "")
                            )

                        retry_error = retry.get("error_message", "Unknown error")
                        if _agentbay_uncertain_start_error(retry_error):
                            visible_note = await _agentbay_visible_apps_note(client)
                            return (
                                f"Direct start command failed: {cmd}\n"
                                f"Matched installed app: {matched_name} (score={score:.2f})\n"
                                f"Retried with start_cmd: {matched_cmd}\n"
                                f"Retry reported an uncertain launch result: {retry_error}\n\n"
                                f"{visible_note}"
                            )
                        return (
                            f"Direct start command failed: {cmd}\n"
                            f"Matched installed app: {matched_name} (score={score:.2f})\n"
                            f"Retried with start_cmd: {matched_cmd}\n"
                            f"Retry failed: {retry_error}"
                        )

                installed_note = (
                    f"\n\nInstalled apps were checked, but no confident match was found for `{cmd}`. "
                    f"Use agentbay_computer_get_installed_apps and then pass the returned start_cmd to this tool."
                )
            else:
                installed_note = (
                    f"\n\nCould not check installed apps: {installed_result.get('error_message', 'Unknown error')}"
                )
        except Exception as e:
            logger.debug(f"[AgentBay] Installed app fallback failed: {e}")
            installed_note = f"\n\nCould not check installed apps: {str(e)[:200]}"

        if _agentbay_uncertain_start_error(direct_error):
            visible_note = await _agentbay_visible_apps_note(client)
            return (
                f"Start command reported an uncertain launch result: {direct_error}\n\n{visible_note}{installed_note}"
            )

        return f"Failed to start application: {direct_error}{installed_note}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer start_app failed")
        return f"Start application failed: {str(e)[:200]}"


async def _agentbay_computer_get_installed_apps(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """List installed desktop applications and launch commands."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    start_menu = arguments.get("start_menu", True)
    desktop = arguments.get("desktop", True)
    ignore_system_apps = arguments.get("ignore_system_apps", True)

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_get_installed_apps(
            start_menu=bool(start_menu),
            desktop=bool(desktop),
            ignore_system_apps=bool(ignore_system_apps),
        )
        if result.get("success"):
            apps = result.get("apps", [])
            if not apps:
                return "No installed applications found."
            return (
                f"Installed applications ({len(apps)}). Use the returned start_cmd exactly with "
                f"agentbay_computer_start_app; do not guess app launch commands.\n\n"
                f"{_agentbay_format_apps(apps, limit=80)}"
            )
        return f"Failed to get installed applications: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer get_installed_apps failed")
        return f"Get installed applications failed: {str(e)[:200]}"


async def _agentbay_computer_list_visible_apps(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """List currently visible/running applications."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_list_visible_apps()
        if result.get("success"):
            import json

            apps = result.get("apps", [])
            if not apps:
                return "No visible applications running."
            apps_str = json.dumps(apps, ensure_ascii=False, indent=2)
            return f"Visible applications ({len(apps)}):\n\n{apps_str[:3000]}"
        return f"Failed to list applications: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer list_visible_apps failed")
        return f"List applications failed: {str(e)[:200]}"


__all__ = [
    "_agentbay_normalize_text",
    "_agentbay_app_field",
    "_agentbay_format_apps",
    "_agentbay_find_installed_app_match",
    "_agentbay_uncertain_start_error",
    "_agentbay_visible_apps_note",
    "_agentbay_computer_start_app",
    "_agentbay_computer_get_installed_apps",
    "_agentbay_computer_list_visible_apps",
]
