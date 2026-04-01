"""AgentBay Take Control API — human-agent collaborative login.

Provides REST endpoints for forwarding mouse/keyboard events to an
AgentBay session and managing the Take Control lock. When locked,
the agent's automatic browser/computer tool execution is paused to
prevent human-agent input collisions.

Cookie export occurs automatically when the Take Control session ends.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.security import encrypt_data, get_current_user
from app.database import get_db
from app.models.agent_credential import AgentCredential
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/{agent_id}/control", tags=["agentbay-control"])


# ── In-memory Take Control lock registry ──
# Key: (agent_id_str, session_id_str) → (user_id, lock_timestamp)
_take_control_locks: dict[tuple[str, str], tuple[str, float]] = {}
_LOCK_TIMEOUT_SECONDS = 600  # Auto-expire stale locks after 10 minutes

# Cache of sessions that have already had browser initialization called.
# Avoids redundant _ensure_browser_initialized() on every screenshot poll.
_browser_initialized: set[tuple] = set()


def is_session_locked(agent_id: str, session_id: str) -> bool:
    """Check if a session is currently under human Take Control.

    Called by execute_tool to block automatic agentbay_* tool calls.
    Automatically clears expired locks.
    """
    key = (agent_id, session_id)
    if key not in _take_control_locks:
        return False
    _user_id, locked_at = _take_control_locks[key]
    if time.time() - locked_at > _LOCK_TIMEOUT_SECONDS:
        logger.info(f"[TakeControl] Auto-expired stale lock for session={session_id[:8]}")
        del _take_control_locks[key]
        return False
    return True


# ── Request schemas ──


class ClickRequest(BaseModel):
    """Mouse click event forwarding."""
    session_id: str
    x: int
    y: int
    button: str = "left"  # left | right | middle


class TypeRequest(BaseModel):
    """Text input event forwarding."""
    session_id: str
    text: str


class PressKeysRequest(BaseModel):
    """Keyboard key press event forwarding."""
    session_id: str
    keys: list[str]  # e.g. ["ctrl", "v"] or ["Tab"]


class ScreenshotRequest(BaseModel):
    """Request an immediate screenshot."""
    session_id: str


class LockRequest(BaseModel):
    """Enter Take Control mode."""
    session_id: str
    platform_hint: Optional[str] = None  # current page domain (for cookie export)


class UnlockRequest(BaseModel):
    """Exit Take Control mode."""
    session_id: str
    export_cookies: bool = True  # whether to export cookies on exit
    platform_hint: Optional[str] = None  # domain to associate cookies with


# ── Helpers ──


async def _get_client(agent_id: uuid.UUID, session_id: str):
    """Retrieve the AgentBay client for the given agent + session.

    Checks the session cache for any active image type (browser, computer, code)
    rather than hardcoding 'browser'. This ensures Take Control works with
    whatever session type the agent is actively using.

    IMPORTANT: For browser sessions, this also calls _ensure_browser_initialized()
    because the browser SDK requires explicit initialization before screenshot/
    interaction APIs will work. Without this, get_browser_snapshot_base64() returns
    None ("Browser not initialized") and all CDP-based interactions fail silently.
    """
    from app.services.agentbay_client import _agentbay_sessions, _AGENTBAY_SESSION_TIMEOUT
    from datetime import datetime

    now = datetime.now()

    # First, try to find an existing cached session for this agent+session
    # across all image types (browser, computer, code)
    for image_type in ("browser", "computer", "code"):
        cache_key = (agent_id, session_id, image_type)
        if cache_key in _agentbay_sessions:
            client, last_used = _agentbay_sessions[cache_key]
            if now - last_used < _AGENTBAY_SESSION_TIMEOUT:
                # Refresh timestamp and reuse
                _agentbay_sessions[cache_key] = (client, now)
                logger.info(
                    f"[TakeControl] Found existing {image_type} session for "
                    f"agent={agent_id}, session={session_id[:8]}"
                )
                # Ensure browser is initialized for browser-type sessions
                # (only on first access — cached to avoid delay on subsequent polls)
                if image_type in ("browser", "browser_latest") and cache_key not in _browser_initialized:
                    try:
                        await client._ensure_browser_initialized()
                        _browser_initialized.add(cache_key)
                    except Exception as e:
                        logger.warning(f"[TakeControl] Browser init on cached session failed: {e}")
                return client

    # No cached session found — create a new browser session
    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        client = await get_agentbay_client_for_agent(
            agent_id, image_type="browser", session_id=session_id
        )
        # Ensure browser is initialized for the newly created session
        try:
            await client._ensure_browser_initialized()
            _browser_initialized.add((agent_id, session_id, "browser"))
            logger.info(f"[TakeControl] Browser initialized for new session, agent={agent_id}")
        except Exception as e:
            logger.warning(f"[TakeControl] Browser init on new session failed: {e}")
        return client
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active browser session found: {e}",
        )


# ── Session-aware input helpers ──
# Browser sessions use CDP (Chrome DevTools Protocol) via Playwright to
# interact directly with Chrome. Desktop sessions use the SDK's computer API.


import asyncio


def _is_browser_session(client) -> bool:
    """Check if the client's active session is a browser image."""
    return getattr(client, "_image_type", "") in ("browser", "browser_latest")


async def _cdp_exec(client, script: str, timeout_ms: int = 15000) -> dict:
    """Execute a Playwright CDP script inside the AgentBay container.

    Uses the AgentBayClient.command_exec wrapper which properly handles
    the SDK call and returns a dict with {success, stdout, stderr, ...}.
    """
    # Write script to temp file inside the container
    write_result = await client.command_exec(
        f"cat > /tmp/_tc_action.js << 'TCEOF'\n{script}\nTCEOF",
        timeout_ms=5000,
    )
    if not write_result.get("success"):
        logger.error(f"[TakeControl] Failed to write CDP script: {write_result}")
        return {"success": False, "output": "Failed to write script", "stderr": str(write_result)[:200]}

    result = await client.command_exec(
        "node /tmp/_tc_action.js",
        timeout_ms=timeout_ms,
    )
    stdout = result.get("stdout", "") or result.get("output", "") or ""
    stderr = result.get("stderr", "") or result.get("error_message", "") or ""
    cmd_success = result.get("success", False)
    tc_success = "TC_OK" in stdout

    logger.info(
        f"[TakeControl] CDP exec: cmd_success={cmd_success}, tc_ok={tc_success}, "
        f"stdout={stdout[:200]}, stderr={stderr[:200]}, exit_code={result.get('exit_code', 'N/A')}"
    )
    return {"success": tc_success, "output": stdout[:500], "stderr": stderr[:200]}


async def _perform_click(client, x: int, y: int, button: str = "left"):
    """Click at (x, y) on the remote session.

    Uses the SDK's Computer API (click_mouse) for both browser and desktop
    sessions. This is a direct, pixel-precise, sub-second operation — much
    faster than the LLM-powered browser.operator.act() which takes 2-5s.

    The browser runs inside a virtual desktop where screen coordinates
    align with the browser viewport, so computer.click_mouse() works
    correctly for browser sessions too.
    """
    image_type = getattr(client, '_image_type', 'unknown')
    logger.info(f"[TakeControl] Click at ({x}, {y}), button={button}, image_type={image_type}")

    try:
        result = await asyncio.to_thread(
            client._session.computer.click_mouse, x, y, button
        )
        success = getattr(result, 'success', False)
        logger.info(f"[TakeControl] Computer click at ({x}, {y}): success={success}")
        return {"success": success, "method": "computer_click", "output": f"Clicked at ({x}, {y})"}
    except Exception as e:
        logger.warning(f"[TakeControl] Computer click failed: {e}")
        return {"success": False, "output": f"Click failed: {str(e)[:200]}"}


async def _perform_type(client, text: str):
    """Type text into the remote session.

    Uses the SDK's Computer API (input_text) which types directly at the
    current cursor/focus position. Much faster than the LLM-powered
    browser.operator.act() approach.
    """
    image_type = getattr(client, '_image_type', 'unknown')
    logger.info(f"[TakeControl] Type text: '{text[:30]}', image_type={image_type}")

    try:
        result = await asyncio.to_thread(
            client._session.computer.input_text, text
        )
        success = getattr(result, 'success', False)
        logger.info(f"[TakeControl] Computer input_text: success={success}")
        return {"success": success, "method": "computer_input", "output": "Text typed"}
    except Exception as e:
        logger.warning(f"[TakeControl] Computer input_text failed: {e}")
        return {"success": False, "output": f"Type failed: {str(e)[:200]}"}


async def _perform_press_keys(client, keys: list[str]):
    """Press key combination on the remote session.

    Uses the SDK's Computer API (press_keys) for direct, instant key
    press — no LLM interpretation overhead.
    """
    key_desc = "+".join(keys)
    logger.info(f"[TakeControl] Press keys: {key_desc}")

    try:
        result = await asyncio.to_thread(
            client._session.computer.press_keys, keys
        )
        success = getattr(result, 'success', False)
        logger.info(f"[TakeControl] Computer press_keys: success={success}")
        return {"success": success, "method": "computer_keys", "output": f"Pressed {key_desc}"}
    except Exception as e:
        logger.warning(f"[TakeControl] Computer press_keys failed: {e}")
        return {"success": False, "output": f"Key press failed: {str(e)[:200]}"}


# ── Endpoints ──


@router.post("/click")
async def control_click(
    agent_id: uuid.UUID,
    data: ClickRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Forward a mouse click to the AgentBay session.

    Requires the session to be in Take Control mode (locked).
    Returns {status: 'ok'|'error', detail: str} so the frontend knows if it worked.
    """
    _agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_session_locked(str(agent_id), data.session_id):
        raise HTTPException(status_code=400, detail="Session is not in Take Control mode")

    client = await _get_client(agent_id, data.session_id)
    try:
        result = await _perform_click(client, data.x, data.y, data.button)
        if result.get("success"):
            return {"status": "ok", "detail": f"Clicked at ({data.x}, {data.y})"}
        else:
            detail = result.get("stderr") or result.get("output") or "Click operation failed"
            return {"status": "error", "detail": detail[:500]}
    except Exception as e:
        logger.error(f"[TakeControl] Click exception: {e}")
        return {"status": "error", "detail": str(e)[:500]}


@router.post("/type")
async def control_type(
    agent_id: uuid.UUID,
    data: TypeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Forward text input to the AgentBay session."""
    _agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_session_locked(str(agent_id), data.session_id):
        raise HTTPException(status_code=400, detail="Session is not in Take Control mode")

    client = await _get_client(agent_id, data.session_id)
    try:
        result = await _perform_type(client, data.text)
        if result.get("success"):
            return {"status": "ok", "detail": "Text sent"}
        else:
            detail = result.get("stderr") or result.get("output") or "Type operation failed"
            return {"status": "error", "detail": detail[:500]}
    except Exception as e:
        logger.error(f"[TakeControl] Type exception: {e}")
        return {"status": "error", "detail": str(e)[:500]}


@router.post("/press_keys")
async def control_press_keys(
    agent_id: uuid.UUID,
    data: PressKeysRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Forward keyboard key presses to the AgentBay session."""
    _agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_session_locked(str(agent_id), data.session_id):
        raise HTTPException(status_code=400, detail="Session is not in Take Control mode")

    client = await _get_client(agent_id, data.session_id)
    try:
        result = await _perform_press_keys(client, data.keys)
        if result.get("success"):
            return {"status": "ok", "detail": f"Pressed: {'+'.join(data.keys)}"}
        else:
            detail = result.get("stderr") or result.get("output") or "Key press failed"
            return {"status": "error", "detail": detail[:500]}
    except Exception as e:
        logger.error(f"[TakeControl] Press keys exception: {e}")
        return {"status": "error", "detail": str(e)[:500]}


@router.post("/screenshot")
async def control_screenshot(
    agent_id: uuid.UUID,
    data: ScreenshotRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get an immediate screenshot from the AgentBay session.

    Automatically detects the session type (browser/desktop) and uses
    the appropriate snapshot method. Returns a base64 data URI and
    the screen size for coordinate mapping.
    """
    _agent, _access = await check_agent_access(db, current_user, agent_id)

    client = await _get_client(agent_id, data.session_id)
    try:
        # Try browser snapshot first, then desktop
        screenshot_b64 = await client.get_browser_snapshot_base64()
        if not screenshot_b64:
            screenshot_b64 = await client.get_desktop_snapshot_base64()
        if not screenshot_b64:
            logger.warning(f"[TakeControl] Screenshot returned None for agent={agent_id}")

        # Also fetch screen size for coordinate mapping between
        # screenshot dimensions and computer.click_mouse() coordinates
        screen_size = None
        try:
            size_result = await asyncio.to_thread(
                client._session.computer.get_screen_size
            )
            if size_result.success and getattr(size_result, 'data', None):
                screen_size = size_result.data
        except Exception:
            pass  # Non-critical — TC still works without it

        return {
            "status": "ok",
            "screenshot": screenshot_b64,
            "screen_size": screen_size,
        }
    except Exception as e:
        logger.warning(f"[TakeControl] Screenshot failed: {e}")
        return {"status": "error", "detail": str(e)[:500]}


@router.post("/lock")
async def control_lock(
    agent_id: uuid.UUID,
    data: LockRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Enter Take Control mode — locks the session against automatic tool execution.

    While locked, the agent's execute_tool will return a "waiting for human"
    message instead of executing browser/computer tools.
    """
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level not in ("manage",) and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Manage access required")

    key = (str(agent_id), data.session_id)
    existing = _take_control_locks.get(key)
    if existing:
        existing_user_id, locked_at = existing
        if existing_user_id != str(current_user.id):
            # Check if the lock has expired
            if time.time() - locked_at > _LOCK_TIMEOUT_SECONDS:
                logger.info(f"[TakeControl] Cleared expired lock held by {existing_user_id}")
            else:
                return {"status": "already_locked", "locked_by": existing_user_id}

    # Acquire or refresh lock with current timestamp
    _take_control_locks[key] = (str(current_user.id), time.time())
    is_reentry = existing is not None
    logger.info(
        f"[TakeControl] Lock acquired: agent={agent_id}, session={data.session_id}, "
        f"user={current_user.id}, re_entry={is_reentry}"
    )
    return {"status": "locked", "locked_by": str(current_user.id)}


@router.post("/unlock")
async def control_unlock(
    agent_id: uuid.UUID,
    data: UnlockRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Exit Take Control mode — unlock session and optionally export cookies.

    If export_cookies is True and platform_hint is provided, the current
    browser cookies will be exported and stored (encrypted) in the
    agent_credentials table.
    """
    _agent, _access = await check_agent_access(db, current_user, agent_id)

    key = (str(agent_id), data.session_id)
    if key not in _take_control_locks:
        logger.info(f"[TakeControl] Unlock called but no lock found: agent={agent_id}, session={data.session_id}")
        return {"status": "not_locked"}

    exported = False
    export_count = 0

    try:
        # Export cookies if requested (non-critical — lock is released regardless)
        if data.export_cookies and data.platform_hint:
            try:
                client = await _get_client(agent_id, data.session_id)
                export_count = await _export_cookies_from_session(
                    client, agent_id, data.platform_hint, db
                )
                exported = True
                logger.info(
                    f"[TakeControl] Cookies exported: agent={agent_id}, "
                    f"platform={data.platform_hint}, count={export_count}"
                )
            except Exception as e:
                logger.warning(f"[TakeControl] Cookie export failed (non-fatal): {e}")
    finally:
        # ALWAYS release the lock, even if cookie export fails
        _take_control_locks.pop(key, None)
        logger.info(
            f"[TakeControl] Lock released: agent={agent_id}, session={data.session_id}"
        )

    return {
        "status": "unlocked",
        "cookies_exported": exported,
        "cookie_count": export_count,
    }


async def _export_cookies_from_session(
    client, agent_id: uuid.UUID, platform_hint: str, db: AsyncSession
) -> int:
    """Export cookies from the current browser session via CDP and store encrypted.

    Uses Playwright's connectOverCDP to read all browser cookies, then upserts
    into the agent_credentials table for the matching platform.

    Returns the number of cookies exported.
    """
    # Build and execute a Node.js script to export cookies via CDP
    export_script = """
const { chromium } = require('playwright');
(async () => {
    try {
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const cookies = await context.cookies();
        console.log('COOKIES_EXPORT:' + JSON.stringify(cookies));
    } catch (e) {
        console.error('EXPORT_FAIL:' + e.message);
    }
})();
"""
    # Write script to temp file to avoid shell quoting issues
    await client.command_exec("cat > /tmp/_export_cookies.js << 'SCRIPT_EOF'\n" + export_script + "\nSCRIPT_EOF")
    result = await client.command_exec("node /tmp/_export_cookies.js", timeout_ms=15000)
    stdout = result.get("stdout", "")

    if "COOKIES_EXPORT:" not in stdout:
        logger.warning(f"[TakeControl] Cookie export script failed: {stdout}")
        return 0

    # Parse the exported cookies JSON
    cookies_line = [line for line in stdout.split("\n") if "COOKIES_EXPORT:" in line]
    if not cookies_line:
        return 0

    cookies_json_str = cookies_line[0].split("COOKIES_EXPORT:", 1)[1].strip()
    try:
        cookies = json.loads(cookies_json_str)
    except json.JSONDecodeError:
        logger.warning("[TakeControl] Failed to parse exported cookies JSON")
        return 0

    if not cookies:
        return 0

    # Encrypt and store
    settings = get_settings()
    encrypted_cookies = encrypt_data(cookies_json_str, settings.SECRET_KEY)

    # Try to find existing credential for this platform
    result = await db.execute(
        select(AgentCredential).where(
            AgentCredential.agent_id == agent_id,
            AgentCredential.platform == platform_hint,
        )
    )
    existing = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)

    if existing:
        # Update existing credential
        existing.cookies_json = encrypted_cookies
        existing.cookies_updated_at = now
        existing.last_login_at = now
        existing.status = "active"
    else:
        # Create new credential
        new_cred = AgentCredential(
            agent_id=agent_id,
            credential_type="website",
            platform=platform_hint,
            display_name=platform_hint,
            cookies_json=encrypted_cookies,
            cookies_updated_at=now,
            last_login_at=now,
            status="active",
        )
        db.add(new_cred)

    await db.commit()
    return len(cookies)
