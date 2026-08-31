from __future__ import annotations

from pathlib import Path
from typing import Optional
import uuid

from loguru import logger

from app.services.agent_tools_agentbay_computer_apps import _agentbay_normalize_text
from app.services.agent_tools_agentbay_computer_screen import _agentbay_get_screen_metadata


async def _agentbay_computer_click(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Click the mouse at specific coordinates on the desktop."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    x = arguments.get("x", 0)
    y = arguments.get("y", 0)
    button = arguments.get("button", "left")

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        try:
            x = int(round(float(x)))
            y = int(round(float(y)))
        except (TypeError, ValueError):
            return f"Click failed: x and y must be numeric desktop pixel coordinates, got x={x!r}, y={y!r}."

        screen_width, screen_height, screen_note = await _agentbay_get_screen_metadata(client)
        if screen_width and screen_height and not (0 <= x < screen_width and 0 <= y < screen_height):
            return (
                f"Click refused: ({x}, {y}) is outside the Cloud Desktop coordinate system "
                f"({screen_note}). Use coordinates from the latest full desktop screenshot."
            )
        result = await client.computer_click(x, y, button=button)
        if result.get("success"):
            note = f" within {screen_note}" if screen_note else ""
            return (
                f"Clicked at ({x}, {y}) with {button} button{note}. "
                f"This only confirms the mouse event was sent; call agentbay_computer_screenshot to verify the UI changed."
            )
        note = f" Coordinate system: {screen_note}." if screen_note else ""
        return f"Click failed at ({x}, {y}).{note}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer click failed")
        return f"Click failed: {str(e)[:200]}"


async def _agentbay_computer_input_text(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Type text at the current cursor position."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    text = arguments.get("text", "")
    if not text:
        return "Missing required argument 'text'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_input_text(text)
        if result.get("success"):
            return f"Typed text: {text[:100]}"
        return f"Text input failed"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer input_text failed")
        return f"Text input failed: {str(e)[:200]}"


async def _agentbay_computer_press_keys(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Press keyboard keys or shortcuts."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    keys = arguments.get("keys", [])
    hold = arguments.get("hold", False)

    if not keys:
        return "Missing required argument 'keys'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_press_keys(keys, hold=hold)
        key_str = "+".join(keys)
        if result.get("success"):
            return f"Pressed keys: {key_str}" + (" (held)" if hold else "")
        return f"Key press failed: {key_str}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer press_keys failed")
        return f"Key press failed: {str(e)[:200]}"


async def _agentbay_computer_scroll(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Scroll the screen at a specific position."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    x = arguments.get("x", 0)
    y = arguments.get("y", 0)
    direction = arguments.get("direction", "down")
    amount = arguments.get("amount", 1)

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_scroll(x, y, direction=direction, amount=amount)
        if result.get("success"):
            return f"Scrolled {direction} by {amount} step(s) at ({x}, {y})"
        return f"Scroll failed"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer scroll failed")
        return f"Scroll failed: {str(e)[:200]}"


async def _agentbay_computer_move_mouse(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Move mouse to coordinates without clicking."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    x = arguments.get("x", 0)
    y = arguments.get("y", 0)

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_move_mouse(x, y)
        if result.get("success"):
            return f"Mouse moved to ({x}, {y})"
        return f"Mouse move failed"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer move_mouse failed")
        return f"Mouse move failed: {str(e)[:200]}"


async def _agentbay_computer_drag_mouse(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Drag mouse from one position to another."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    from_x = arguments.get("from_x", 0)
    from_y = arguments.get("from_y", 0)
    to_x = arguments.get("to_x", 0)
    to_y = arguments.get("to_y", 0)
    button = arguments.get("button", "left")

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_drag_mouse(from_x, from_y, to_x, to_y, button=button)
        if result.get("success"):
            return f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})"
        return f"Drag failed"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer drag_mouse failed")
        return f"Drag failed: {str(e)[:200]}"


async def _agentbay_computer_get_cursor_position(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Get current cursor position."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_get_cursor_position()
        if result.get("success"):
            import json

            data = result.get("data")
            data_str = json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)
            return f"Cursor position: {data_str}"
        return f"Failed to get cursor position: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer get_cursor_position failed")
        return f"Get cursor position failed: {str(e)[:200]}"


async def _agentbay_computer_get_active_window(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Get info about the currently active window."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_get_active_window()
        if result.get("success"):
            import json

            window = result.get("window")
            window_str = json.dumps(window, ensure_ascii=False, indent=2) if isinstance(window, dict) else str(window)
            return f"Active window:\n\n{window_str}"
        return f"Failed to get active window: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer get_active_window failed")
        return f"Get active window failed: {str(e)[:200]}"


async def _agentbay_computer_activate_window(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Activate (bring to front) a window by its ID."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    window_id = arguments.get("window_id")
    if window_id is None:
        return "Missing required argument 'window_id'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_activate_window(int(window_id))
        if result.get("success"):
            return f"Window {window_id} activated (brought to front)"
        return f"Failed to activate window {window_id}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer activate_window failed")
        return f"Activate window failed: {str(e)[:200]}"


async def _agentbay_computer_list_windows(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """List OS-level root windows with IDs and geometry."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    timeout_ms = arguments.get("timeout_ms", 3000)

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_list_windows(timeout_ms=int(timeout_ms))
        if result.get("success"):
            import json

            windows = result.get("windows", [])
            if not windows:
                return "No root windows found."
            windows_str = json.dumps(windows, ensure_ascii=False, indent=2)
            return (
                f"OS-level root desktop windows ({len(windows)}). These window_id values refer to whole "
                f"application windows. Use them for activation, or for closing only when the user explicitly "
                f"asked to close/quit an entire desktop window or app. Do NOT use these IDs for in-app popups, "
                f"modals, embedded marketplace/store panels, browser/app tabs, document tabs, or software-internal "
                f"dialogs; close those with the app UI, Escape, Ctrl+W, or agentbay_computer_dismiss_dialog.\n\n"
                f"{windows_str[:5000]}"
            )
        return f"Failed to list windows: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer list_windows failed")
        return f"List windows failed: {str(e)[:200]}"


async def _agentbay_computer_close_window(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Close an entire OS-level root desktop window/application by explicit ID."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    window_id = arguments.get("window_id")
    title = str(arguments.get("title") or "").strip()

    if window_id is None:
        if not title:
            return (
                "Missing required argument `window_id`. Only use agentbay_computer_close_window when the user "
                "explicitly wants to close or quit an entire OS-level desktop window/application. If the target "
                "is an in-app popup, modal, embedded marketplace/store panel, browser/app tab, document tab, "
                "or software-internal dialog, use app UI controls, Escape, Ctrl+W, or "
                "agentbay_computer_dismiss_dialog instead."
            )

        try:
            _session_id = arguments.pop("_session_id", "")
            client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
            windows_result = await client.computer_list_windows()
            if not windows_result.get("success"):
                return f"Failed to list windows before closing: {windows_result.get('error_message', 'Unknown error')}"

            from difflib import SequenceMatcher
            import json

            title_norm = _agentbay_normalize_text(title)
            candidates: list[dict] = []
            for window in windows_result.get("windows", []):
                if not isinstance(window, dict):
                    continue
                candidate = str(window.get("title") or window.get("window_title") or "")
                candidate_norm = _agentbay_normalize_text(candidate)
                if not candidate_norm:
                    continue
                if title_norm in candidate_norm or candidate_norm in title_norm:
                    score = 0.95
                else:
                    score = SequenceMatcher(None, title_norm, candidate_norm).ratio()
                if score >= 0.35:
                    item = dict(window)
                    item["match_score"] = round(score, 3)
                    candidates.append(item)
            candidates.sort(key=lambda item: item.get("match_score", 0), reverse=True)
            return (
                f"Refusing to close by title-only match for `{title}` because it can close the wrong application. "
                f"The candidates below are whole OS-level root windows. Choose a root window_id only if the user "
                f"explicitly wants to close/quit that entire application window. For in-app popups, modals, "
                f"embedded marketplace/store panels, browser/app tabs, document tabs, or software-internal dialogs, "
                f"do not close a root window; use app UI controls, Escape, Ctrl+W, or "
                f"agentbay_computer_dismiss_dialog instead.\n\n"
                f"{json.dumps(candidates[:8], ensure_ascii=False, indent=2)[:3000]}"
            )
        except RuntimeError as e:
            return f"{str(e)}"
        except Exception as e:
            logger.exception(f"[AgentBay] Computer close_window candidate lookup failed")
            return f"Close window requires window_id. Candidate lookup failed: {str(e)[:200]}"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_close_window(int(window_id))
        if result.get("success"):
            return (
                f"Closed OS-level root desktop window {window_id}; the whole application window may now be gone. "
                f"Call agentbay_computer_screenshot to verify."
            )
        return f"Failed to close window {window_id}: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer close_window failed")
        return f"Close window failed: {str(e)[:200]}"


async def _agentbay_computer_dismiss_dialog(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Safely dismiss the current in-app popup/dialog without closing root windows."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    title = str(arguments.get("title") or "").strip()
    window_id = arguments.get("window_id")

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)

        if window_id is not None:
            return (
                "agentbay_computer_dismiss_dialog does not close root desktop windows. "
                "It only sends Escape to the active in-app popup/dialog. "
                "For in-app tabs, embedded panels, marketplace/store windows, or document tabs, use the app UI "
                "or shortcuts such as Ctrl+W. If the user explicitly wants to close/quit a whole desktop window "
                "or app, call agentbay_computer_close_window with a window_id returned by "
                "agentbay_computer_list_windows."
            )

        esc_result = await client.computer_press_keys(["esc"])
        if esc_result.get("success"):
            title_note = f" Target hint: `{title}`." if title else ""
            return (
                f"Sent Escape to safely dismiss the active in-app popup/dialog.{title_note} "
                f"Call agentbay_computer_screenshot to verify. This tool never closes the root application window; "
                f"if Escape does not affect an in-app tab or embedded panel, use that app's own close control "
                f"or a shortcut such as Ctrl+W instead of root-window close."
            )

        return (
            f"Could not send Escape to dismiss the active popup/dialog: "
            f"{esc_result.get('error_message', 'Unknown error')}. "
            f"Do not use this tool to close root application windows."
        )
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer dismiss_dialog failed")
        return f"Dismiss dialog failed: {str(e)[:200]}"


__all__ = [
    "_agentbay_computer_click",
    "_agentbay_computer_input_text",
    "_agentbay_computer_press_keys",
    "_agentbay_computer_scroll",
    "_agentbay_computer_move_mouse",
    "_agentbay_computer_drag_mouse",
    "_agentbay_computer_get_cursor_position",
    "_agentbay_computer_get_active_window",
    "_agentbay_computer_activate_window",
    "_agentbay_computer_list_windows",
    "_agentbay_computer_close_window",
    "_agentbay_computer_dismiss_dialog",
]
