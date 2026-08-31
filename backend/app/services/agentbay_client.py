"""AgentBay API client using official SDK.

This module provides a client wrapper around the official AgentBay SDK
for browser and code execution operations.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from loguru import logger
from pydantic import RootModel

class GenericExtractSchema(RootModel[Any]):
    pass

from agentbay import AgentBay, CreateSessionParams
from app.core.logging_config import _disable_agentbay_logger_override, configure_logging
from app.services import agentbay_client_runtime as _agentbay_client_runtime

_disable_agentbay_logger_override()
configure_logging()


@dataclass
class AgentBaySession:
    """AgentBay session info."""
    session_id: str
    image: str
    created_at: datetime
    expires_at: Optional[datetime] = None


class AgentBayClient:
    """Client for AgentBay SDK interactions."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._sdk = AgentBay(api_key=api_key)
        self._session = None
        self._image_type = None

    async def create_session(self, image: str = "linux_latest") -> AgentBaySession:
        """Create a new session using SDK.

        Closes any existing session first to prevent leaked sessions
        on the AgentBay API side.
        """
        # Close existing session to prevent leaking concurrent sessions
        if self._session:
            logger.info("[AgentBay] Closing existing session before creating new one")
            await self.close_session()

        image_id_map = {
            "browser_latest": "browser_latest",
            "code_latest": "linux_latest",
            "linux_latest": "linux_latest",
            "windows_latest": "windows_latest",
        }
        image_id = image_id_map.get(image, image)
        self._image_type = image

        result = await asyncio.to_thread(self._sdk.create, CreateSessionParams(image_id=image_id))
        if not result.success:
            raise RuntimeError(f"Failed to create session: {result.error_message}")

        self._session = result.session
        self._browser_initialized = False
        logger.info(f"[AgentBay] Created session with image {image_id}")
        return AgentBaySession(
            session_id=self._session.session_id,
            image=image,
            created_at=datetime.now(),
            expires_at=datetime.now() + timedelta(hours=1),
        )

    async def close_session(self):
        """Release the current session."""
        if not self._session:
            return
        try:
            await asyncio.to_thread(self._session.delete)
            logger.info("[AgentBay] Closed session")
        except Exception as e:
            logger.warning(f"[AgentBay] Failed to close session: {e}")
        finally:
            self._session = None
            self._browser_initialized = False

    # ─── Browser Operations ──────────────────────────

    async def _ensure_browser_initialized(self):
        """Ensure the browser is initialized for the current session."""
        if not self._session:
            raise RuntimeError("No active browser session")
        if not getattr(self, "_browser_initialized", False):
            from agentbay import BrowserOption
            from agentbay._common.models.browser import BrowserViewport, BrowserScreen
            
            # Use high-res viewport for clearer screenshots and better layout
            options = BrowserOption(
                viewport=BrowserViewport(width=1920, height=1080),
                screen=BrowserScreen(width=1920, height=1080)
            )
            success = await asyncio.to_thread(self._session.browser.initialize, options)
            if success is False:
                raise RuntimeError("SDK failed to initialize browser (returned False).")
            self._browser_initialized = True

    async def browser_navigate(self, url: str, wait_for: str = "", screenshot: bool = False) -> dict:
        """Navigate browser to URL using SDK.

        The AgentBay SDK default navigation timeout is ~60 s. We wrap the call
        with a 40-second asyncio soft-timeout so callers receive an actionable
        error quickly rather than hanging the whole agent loop. The underlying
        SDK thread may continue briefly in the background but its result is
        discarded — the browser will eventually settle on its own.
        """
        if not self._session or self._image_type not in ("browser", "browser_latest"):
            await self.create_session("browser_latest")

        await self._ensure_browser_initialized()

        # Navigate to URL with a 40-second soft timeout.
        # asyncio.wait_for cancels the coroutine wrapper; the blocking thread
        # inside asyncio.to_thread keeps running until SDK returns, but we
        # no longer block the agent loop waiting for it.
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._session.browser.operator.navigate, url),
                timeout=40.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[AgentBay] navigate to {url!r} timed out after 40 s")
            raise RuntimeError(
                f"Navigation to '{url}' timed out (>40 s). "
                "The browser may be busy or the page is unreachable. "
                "Try calling agentbay_browser_screenshot to check the current "
                "state, or retry the navigation."
            )

        result = {"url": url, "success": True, "title": url}

        if screenshot:
            # Wait for dynamic content and SPA rendering (React/Vue) before screenshotting
            await asyncio.sleep(3)
            screenshot_data = await asyncio.to_thread(
                self._session.browser.operator.screenshot, full_page=False
            )
            result["screenshot"] = screenshot_data

        return result

    async def browser_screenshot(self) -> dict:
        """Take a screenshot of the current browser page without navigating.

        Use this after actions (click, type, form submit) to verify results
        without refreshing the page. Never call browser_navigate just to screenshot.
        """
        await self._ensure_browser_initialized()
        
        # Wait for dynamic content and SPA rendering before screenshotting
        await asyncio.sleep(3)
        
        screenshot_data = await asyncio.to_thread(
            self._session.browser.operator.screenshot, full_page=False
        )
        return {"success": True, "screenshot": screenshot_data}


    async def browser_click(self, selector: str) -> dict:
        """Click element by CSS selector using SDK."""
        await self._ensure_browser_initialized()

        from agentbay import ActOptions
        await asyncio.to_thread(self._session.browser.operator.act, ActOptions(action=f"click on {selector}"))
        return {"success": True, "selector": selector}

    async def browser_type(self, selector: str, text: str) -> dict:
        """Type text into element using SDK."""
        await self._ensure_browser_initialized()

        from agentbay import ActOptions

        # Detect OTP/PIN-style inputs: short digit-only strings (4-8 chars)
        # These use segmented input boxes that auto-advance focus per digit,
        # so character-by-character typing often fails. Use paste strategy instead.
        is_otp = text.isdigit() and 4 <= len(text) <= 8

        if is_otp:
            action_msg = (
                f"The text '{text}' appears to be a verification/OTP code. "
                f"Find the verification code input area near '{selector}'. "
                f"Click on the first input box, then paste or type the full code '{text}'. "
                f"If the input is split into individual digit boxes, click the first box "
                f"and type each digit one at a time: {', '.join(text)}. "
                f"Each box should auto-advance to the next after entering a digit."
            )
        else:
            # Standard input: click to focus, then type character by character
            # to correctly trigger React/Vue input events.
            action_msg = (
                f"Click on the element matching '{selector}' to focus it, "
                f"then use the keyboard to type the text '{text}' character by character. "
                f"This ensures modern web frameworks like React register the input."
            )

        await asyncio.to_thread(self._session.browser.operator.act, ActOptions(action=action_msg))
        return {"success": True, "selector": selector, "text": text}

    async def browser_login(self, url: str, login_config: str) -> dict:
        """Perform an automated login using AgentBay's built-in login skill.

        This leverages AgentBay's AI-driven login capability to handle complex
        login flows including CAPTCHAs, OTP inputs, and multi-step authentication.

        Args:
            url: The login page URL to navigate to first.
            login_config: JSON string with login configuration, e.g.
                          '{"api_key": "xxx", "skill_id": "yyy"}'
        """
        if not self._session or self._image_type != "browser":
            await self.create_session("browser_latest")
        await self._ensure_browser_initialized()

        # Navigate to the login page first
        await asyncio.to_thread(self._session.browser.operator.navigate, url)

        # Execute the login skill
        result = await asyncio.to_thread(
            self._session.browser.operator.login,
            login_config,
            use_vision=True,
        )
        return {
            "success": result.success,
            "message": result.message or "",
        }

    # ─── Code Operations ──────────────────────────

    async def code_execute(self, language: str, code: str, timeout: int = 30) -> dict:
        """Execute code in code space using SDK."""
        lang_map = {
            "python": "python",
            "bash": "bash",
            "shell": "bash",
            "node": "node",
            "javascript": "node",
        }
        sdk_lang = lang_map.get(language.lower(), "python")

        if not self._session or self._image_type not in ("code", "code_latest"):
            await self.create_session("code_latest")

        result = await asyncio.to_thread(self._session.code.run_code, code, sdk_lang)

        return {
            "stdout": result.result if result.success else "",
            "stderr": result.error_message if not result.success else "",
            "exit_code": 0 if result.success else 1,
            "success": result.success,
        }

    # ─── Browser: Extract & Observe ───────────────────

    async def browser_extract(self, instruction: str, selector: str = "") -> dict:
        """Extract structured data from current page using natural language instruction."""
        await self._ensure_browser_initialized()
        
        # Wait for dynamic content and SPA rendering before extracting
        await asyncio.sleep(3)

        from agentbay._common.models.browser_operator import ExtractOptions
        # Use a generic RootModel schema since we cannot define a custom Pydantic model at runtime
        options = ExtractOptions(
            instruction=instruction,
            schema=GenericExtractSchema,
            selector=selector or None,
        )
        success, data = await asyncio.to_thread(
            self._session.browser.operator.extract, options
        )
        if success and data:
            if hasattr(data, "model_dump"):
                data = data.model_dump()
        return {"success": success, "data": data}

    async def browser_observe(self, instruction: str, selector: str = "") -> dict:
        """Observe the current page state and return interactive elements."""
        await self._ensure_browser_initialized()
        
        # Wait for dynamic content and SPA rendering before observing
        await asyncio.sleep(3)

        from agentbay._common.models.browser_operator import ObserveOptions
        options = ObserveOptions(
            instruction=instruction,
            selector=selector or None,
        )
        success, results = await asyncio.to_thread(
            self._session.browser.operator.observe, options
        )
        # Convert ObserveResult objects to dicts for serialization
        result_dicts = []
        for r in (results or []):
            result_dicts.append(vars(r) if hasattr(r, "__dict__") else str(r))
        return {"success": success, "elements": result_dicts}

    # ─── Command (Shell) Operations ──────────────────

    async def command_exec(self, command: str, timeout_ms: int = 50000, cwd: str = "") -> dict:
        """Execute a shell command in the AgentBay environment."""
        if not self._session:
            await self.create_session("linux_latest")

        result = await asyncio.to_thread(
            self._session.command.exec,
            command,
            timeout_ms=timeout_ms,
            cwd=cwd or None,
        )
        return {
            "success": result.success,
            "stdout": getattr(result, "stdout", "") or getattr(result, "output", "") or "",
            "stderr": getattr(result, "stderr", "") or "",
            "exit_code": getattr(result, "exit_code", -1),
            "error_message": result.error_message or "",
        }

    # ─── Computer Operations ──────────────────────────

    async def _ensure_computer_session(self):
        """Ensure a computer (linux or windows desktop) session is active."""
        if not self._session or self._image_type not in ("computer", "linux_latest", "windows_latest"):
            await self.create_session("linux_latest")

    async def computer_screenshot(self) -> dict:
        """Take a screenshot of the desktop.

        Tries the standard screenshot() API first, then falls back to
        beta_take_screenshot() for cloud environments that don't support
        the standard API yet.
        """
        await self._ensure_computer_session()
        
        # Wait briefly for UI animations/rendering to settle
        await asyncio.sleep(2)

        try:
            result = await asyncio.to_thread(self._session.computer.screenshot)
            # Some cloud environments return success=False with a message
            # telling us to use beta_take_screenshot() instead of throwing.
            if not result.success and "beta_take_screenshot" in (result.error_message or ""):
                logger.info("[AgentBay] screenshot() unsupported, falling back to beta_take_screenshot()")
                result = await asyncio.to_thread(self._session.computer.beta_take_screenshot)
        except Exception as e:
            # Also handle the case where it raises an exception
            if "beta_take_screenshot" in str(e):
                logger.info("[AgentBay] Falling back to beta_take_screenshot() after exception")
                result = await asyncio.to_thread(self._session.computer.beta_take_screenshot)
            else:
                raise
        return {
            "success": result.success,
            "data": getattr(result, "data", None),
            "error_message": result.error_message or "",
        }

    async def computer_click(self, x: int, y: int, button: str = "left") -> dict:
        """Click the mouse at coordinates (x, y)."""
        await self._ensure_computer_session()
        move_result = await asyncio.to_thread(self._session.computer.move_mouse, x, y)
        result = await asyncio.to_thread(self._session.computer.click_mouse, x, y, button)
        return {
            "success": result.success,
            "moved": getattr(move_result, "success", False),
            "x": x,
            "y": y,
            "button": button,
        }

    async def computer_input_text(self, text: str) -> dict:
        """Input text at the current cursor position."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.input_text, text)
        return {"success": result.success, "text": text}

    async def computer_press_keys(self, keys: list, hold: bool = False) -> dict:
        """Press keyboard keys (e.g. ['ctrl', 'c'] for Ctrl+C)."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.press_keys, keys, hold=hold)
        return {"success": result.success, "keys": keys, "hold": hold}

    async def computer_scroll(self, x: int, y: int, direction: str = "down", amount: int = 1) -> dict:
        """Scroll the screen at position (x, y)."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(
            self._session.computer.scroll, x, y, direction=direction, amount=amount
        )
        return {"success": result.success, "direction": direction, "amount": amount}

    async def computer_move_mouse(self, x: int, y: int) -> dict:
        """Move mouse to coordinates (x, y) without clicking."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.move_mouse, x, y)
        return {"success": result.success, "x": x, "y": y}

    async def computer_drag_mouse(
        self, from_x: int, from_y: int, to_x: int, to_y: int, button: str = "left"
    ) -> dict:
        """Drag mouse from (from_x, from_y) to (to_x, to_y)."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(
            self._session.computer.drag_mouse, from_x, from_y, to_x, to_y, button=button
        )
        return {"success": result.success, "from": [from_x, from_y], "to": [to_x, to_y]}

    async def computer_get_screen_size(self) -> dict:
        """Get the screen resolution."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.get_screen_size)
        return {
            "success": result.success,
            "data": getattr(result, "data", None),
            "error_message": result.error_message or "",
        }

    async def computer_start_app(self, cmd: str, work_dir: str = "") -> dict:
        """Start an application by its command."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(
            self._session.computer.start_app, cmd, work_directory=work_dir
        )
        return {
            "success": result.success,
            "data": getattr(result, "data", None),
            "error_message": result.error_message or "",
        }

    async def computer_get_installed_apps(
        self,
        start_menu: bool = True,
        desktop: bool = True,
        ignore_system_apps: bool = True,
    ) -> dict:
        """List installed applications and their launch commands."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(
            self._session.computer.get_installed_apps,
            start_menu,
            desktop,
            ignore_system_apps,
        )
        apps = []
        for app in (getattr(result, "data", None) or []):
            apps.append(vars(app) if hasattr(app, "__dict__") else str(app))
        return {
            "success": result.success,
            "apps": apps,
            "error_message": result.error_message or "",
        }

    async def computer_get_cursor_position(self) -> dict:
        """Get current cursor position."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.get_cursor_position)
        return {
            "success": result.success,
            "data": getattr(result, "data", None),
            "error_message": result.error_message or "",
        }

    async def computer_get_active_window(self) -> dict:
        """Get info about the currently active window."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.get_active_window)
        window = getattr(result, "window", None)
        return {
            "success": result.success,
            "window": vars(window) if window and hasattr(window, "__dict__") else str(window),
            "error_message": result.error_message or "",
        }

    async def computer_list_windows(self, timeout_ms: int = 3000) -> dict:
        """List root desktop windows with IDs and geometry."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.list_root_windows, timeout_ms)
        windows = []
        for window in (getattr(result, "windows", None) or []):
            windows.append(vars(window) if hasattr(window, "__dict__") else str(window))
        return {
            "success": result.success,
            "windows": windows,
            "error_message": result.error_message or "",
        }

    async def computer_activate_window(self, window_id: int) -> dict:
        """Activate (bring to front) a window by its ID."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.activate_window, window_id)
        return {"success": result.success, "window_id": window_id}

    async def computer_close_window(self, window_id: int) -> dict:
        """Close a desktop window by its ID."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.close_window, window_id)
        return {
            "success": result.success,
            "window_id": window_id,
            "error_message": result.error_message or "",
        }

    async def computer_list_visible_apps(self) -> dict:
        """List currently visible/running applications."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.list_visible_apps)
        data = getattr(result, "data", [])
        # Convert process objects to dicts
        apps = []
        for p in (data or []):
            apps.append(vars(p) if hasattr(p, "__dict__") else str(p))
        return {
            "success": result.success,
            "apps": apps,
            "error_message": result.error_message or "",
        }

    # ─── Live Preview Support ──────────────────────────

    async def get_live_url(self) -> str | None:
        """Get the VNC/viewer URL for the current computer session.

        Calls session.get_link() which returns a shareable viewer URL
        for the cloud desktop. Returns None if no session is active
        or the API call fails.
        """
        if not self._session:
            return None
        try:
            result = await asyncio.to_thread(self._session.get_link)
            if result.success and result.data:
                logger.info(f"[AgentBay] Got live URL: {str(result.data)[:80]}...")
                return result.data
            logger.warning(f"[AgentBay] get_link() failed: {result.error_message}")
            return None
        except Exception as e:
            logger.warning(f"[AgentBay] Failed to get live URL: {e}")
            return None

    async def get_desktop_snapshot_base64(self) -> str | None:
        """Take a quick desktop screenshot and return compressed base64 JPEG.

        Used for live preview panel. Calls the same screenshot API as
        computer_screenshot() but without the sleep delay, and compresses
        the result for efficient WebSocket transfer.
        Returns data:image/jpeg;base64,... or None on failure.
        """
        if not self._session:
            return None
        try:
            # Use the same screenshot logic as computer_screenshot()
            try:
                result = await asyncio.to_thread(self._session.computer.screenshot)
                if not result.success and "beta_take_screenshot" in (result.error_message or ""):
                    result = await asyncio.to_thread(self._session.computer.beta_take_screenshot)
            except Exception as e:
                if "beta_take_screenshot" in str(e):
                    result = await asyncio.to_thread(self._session.computer.beta_take_screenshot)
                else:
                    raise

            screenshot_data = getattr(result, "data", None)
            if not screenshot_data:
                return None

            # Compress to JPEG base64 for live preview
            import base64
            from io import BytesIO
            from PIL import Image

            img = Image.open(BytesIO(screenshot_data))
            # Resize to max 1920px wide for live preview (up from 1280px to preserve details)
            if img.width > 1920:
                ratio = 1920 / img.width
                img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=80, optimize=True)
            b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            logger.warning(f"[AgentBay] Desktop snapshot failed: {e}")
            return None

    async def get_browser_snapshot_base64(self) -> str | None:
        """Take a quick browser screenshot and return compressed base64 JPEG.

        Used for live preview panel — no wait/sleep since we want
        the snapshot to reflect the current state immediately.
        Returns data:image/jpeg;base64,... or None on failure.
        """
        if not self._session:
            logger.info("[AgentBay] Browser snapshot skipped: No active session")
            return None
        if not getattr(self, "_browser_initialized", False):
            logger.info("[AgentBay] Browser snapshot skipped: Browser not initialized")
            return None
        
        try:
            screenshot_data = await asyncio.to_thread(
                self._session.browser.operator.screenshot, full_page=False
            )
            if not screenshot_data:
                logger.info("[AgentBay] Browser snapshot returned empty data")
                return None

            # Compress screenshot to JPEG base64 for efficient transfer
            import base64
            from io import BytesIO
            from PIL import Image

            if isinstance(screenshot_data, str):
                # The AgentBay SDK may return a raw base64 string without proper
                # padding. Normalize by stripping whitespace and adding padding chars.
                screenshot_data = screenshot_data.strip()
                # Remove data URI prefix if present (e.g., "data:image/png;base64,")
                if "," in screenshot_data:
                    screenshot_data = screenshot_data.split(",", 1)[1]
                # Add base64 padding if missing
                missing_padding = len(screenshot_data) % 4
                if missing_padding:
                    screenshot_data += "=" * (4 - missing_padding)
                screenshot_data = base64.b64decode(screenshot_data)


            img = Image.open(BytesIO(screenshot_data))
            # Resize to max 1920px wide for live preview (up from 1280px to preserve details)
            if img.width > 1920:
                ratio = 1920 / img.width
                img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=80, optimize=True)
            b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            logger.warning(f"[AgentBay] Browser snapshot failed: {e}")
            return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close_session()


# ─── Session Cache for Tool Executions ──────────────────────────
# Key: (agent_id, session_id, image_type) so each ChatSession gets
# its own independent AgentBay instance for browser/computer/code.
# Previously keyed by (agent_id, image_type) which meant all users
# of the same Agent shared one browser/desktop — causing conflicts.

_agentbay_sessions: dict[tuple[uuid.UUID, str, str], tuple[AgentBayClient, datetime]] = {}
_AGENTBAY_SESSION_TIMEOUT = timedelta(minutes=5)


AGENTBAY_API_URL = "https://api.agentbay.ai/v1"


def _is_plausible_agentbay_api_key(value: str | None) -> bool:
    return _agentbay_client_runtime._is_plausible_agentbay_api_key(value)


async def get_agentbay_api_key_for_agent(agent_id: uuid.UUID, db=None) -> Optional[str]:
    return await _agentbay_client_runtime.get_agentbay_api_key_for_agent(
        agent_id,
        db=db,
        is_plausible_agentbay_api_key=_is_plausible_agentbay_api_key,
    )


async def test_agentbay_channel(agent_id: uuid.UUID, current_user, db) -> dict:
    return await _agentbay_client_runtime.test_agentbay_channel(
        agent_id,
        current_user,
        db,
        get_agentbay_api_key_for_agent=get_agentbay_api_key_for_agent,
    )


async def get_agentbay_client_for_agent(agent_id: uuid.UUID, image_type: str, session_id: str = "") -> AgentBayClient:
    return await _agentbay_client_runtime.get_agentbay_client_for_agent(
        agent_id,
        image_type,
        session_id=session_id,
        agentbay_sessions=_agentbay_sessions,
        agentbay_session_timeout=_AGENTBAY_SESSION_TIMEOUT,
        agentbay_client_cls=AgentBayClient,
        get_agentbay_api_key_for_agent=get_agentbay_api_key_for_agent,
        is_plausible_agentbay_api_key=_is_plausible_agentbay_api_key,
        inject_credentials=_inject_credentials,
    )


async def cleanup_agentbay_sessions():
    await _agentbay_client_runtime.cleanup_agentbay_sessions(
        agentbay_sessions=_agentbay_sessions,
        agentbay_session_timeout=_AGENTBAY_SESSION_TIMEOUT,
    )


async def _inject_credentials(client: AgentBayClient, agent_id: uuid.UUID):
    await _agentbay_client_runtime.inject_credentials(client, agent_id)
