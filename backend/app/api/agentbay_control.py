"""AgentBay Take Control API — human-agent collaborative login.

Provides REST endpoints for forwarding mouse/keyboard events to an
AgentBay session and managing the Take Control lock. When locked,
the agent's automatic browser/computer tool execution is paused to
prevent human-agent input collisions.

Cookie export occurs automatically when the Take Control session ends.
"""

import asyncio
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
from app.api.agentbay_control_helpers import (
    _take_control_locks,
    _LOCK_TIMEOUT_SECONDS,
    _browser_initialized,
    _tc_interaction_locks,
    _get_interaction_lock,
    is_session_locked,
    _get_session_env_type,
    ClickRequest,
    TypeRequest,
    PressKeysRequest,
    DragRequest,
    ScreenshotRequest,
    LockRequest,
    UnlockRequest,
    _get_client,
    _is_browser_session,
    _cdp_exec,
    _eval_cdp_script,
    _tc_browser_cleanup,
    _perform_click,
    _perform_type,
    _perform_press_keys,
    _perform_drag,
)

for _agentbay_request_model in (
    ClickRequest,
    TypeRequest,
    PressKeysRequest,
    DragRequest,
    ScreenshotRequest,
    LockRequest,
    UnlockRequest,
):
    _agentbay_request_model.__module__ = __name__

# ── Endpoints ──


class CurrentUrlRequest(BaseModel):
    """Request to get the current page URL from the browser session."""
    session_id: str


@router.post("/current-url")
async def control_current_url(
    agent_id: uuid.UUID,
    data: CurrentUrlRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current page URL from the active browser session via CDP.

    Called by the Take Control panel on mount to auto-populate the cookie
    domain field, so the user doesn't have to type the domain manually.
    """
    _agent, _access = await check_agent_access(db, current_user, agent_id)

    env_type = _get_session_env_type(str(agent_id), data.session_id)
    client = await _get_client(agent_id, data.session_id, env_type=env_type)

    script = """
const { chromium } = require('/usr/local/lib/node_modules/playwright');
let browser;
(async () => {
    let ok = false;
    try {
        browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const page = context.pages()[0];
        const url = page.url();
        console.log('URL_OK:' + url);
        ok = true;
    } catch (e) {
        console.error('URL_FAIL:' + e.message);
    } finally {
        if (browser) await browser.close().catch(() => {});
    }
    process.exit(ok ? 0 : 1);
})();
"""
    try:
        res = await _eval_cdp_script(client, script)
        output = res.get("output", "")
        if "URL_OK:" in output:
            url = output.split("URL_OK:", 1)[1].strip()
            return {"status": "ok", "url": url}
        return {"status": "ok", "url": ""}
    except Exception as e:
        logger.warning(f"[TakeControl] current-url failed: {e}")
        return {"status": "ok", "url": ""}  # Non-fatal — return empty URL



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

    env_type = _get_session_env_type(str(agent_id), data.session_id)
    client = await _get_client(agent_id, data.session_id, env_type=env_type)
    # Serialize interactions per-session: rapid clicks would otherwise overwrite
    # tc_action.js concurrently, causing the second script to read wrong content.
    async with _get_interaction_lock(agent_id, data.session_id):
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

    env_type = _get_session_env_type(str(agent_id), data.session_id)
    client = await _get_client(agent_id, data.session_id, env_type=env_type)
    async with _get_interaction_lock(agent_id, data.session_id):
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

    env_type = _get_session_env_type(str(agent_id), data.session_id)
    client = await _get_client(agent_id, data.session_id, env_type=env_type)
    async with _get_interaction_lock(agent_id, data.session_id):
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


@router.post("/drag")
async def control_drag(
    agent_id: uuid.UUID,
    data: DragRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Simulate a human-like mouse drag in the AgentBay session.

    Used for slider CAPTCHAs and drag-and-drop interactions.
    The drag follows a Bezier curve trajectory with random jitter to
    mimic natural mouse movement, which is required to bypass bot detection.
    """
    _agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_session_locked(str(agent_id), data.session_id):
        raise HTTPException(status_code=400, detail="Session is not in Take Control mode")

    env_type = _get_session_env_type(str(agent_id), data.session_id)
    client = await _get_client(agent_id, data.session_id, env_type=env_type)
    async with _get_interaction_lock(agent_id, data.session_id):
        try:
            result = await _perform_drag(
                client,
                data.from_x, data.from_y,
                data.to_x, data.to_y,
                data.duration_ms,
            )
            if result.get("success"):
                return {"status": "ok", "detail": result.get("output", "Drag complete")}
            else:
                return {"status": "error", "detail": result.get("output", "Drag failed")[:500]}
        except Exception as e:
            logger.error(f"[TakeControl] Drag exception: {e}")
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

    env_type = _get_session_env_type(str(agent_id), data.session_id)
    client = await _get_client(agent_id, data.session_id, env_type=env_type)
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
    # Allow any user with access (manage or use) — Take Control is part of
    # the normal interaction flow, not an admin-only operation.

    key = (str(agent_id), data.session_id)
    existing = _take_control_locks.get(key)
    if existing:
        existing_user_id, locked_at, _existing_env_type = existing
        if existing_user_id != str(current_user.id):
            # Check if the lock has expired
            if time.time() - locked_at > _LOCK_TIMEOUT_SECONDS:
                logger.info(f"[TakeControl] Cleared expired lock held by {existing_user_id}")
            else:
                return {"status": "already_locked", "locked_by": existing_user_id}

    # Sanitize env_type — default to 'browser' if empty or unknown
    env_type = (data.env_type or "browser").lower()
    if env_type not in ("browser", "computer", "code"):
        env_type = "browser"

    # Acquire or refresh lock with current timestamp and env_type
    _take_control_locks[key] = (str(current_user.id), time.time(), env_type)
    is_reentry = existing is not None
    logger.info(
        f"[TakeControl] Lock acquired: agent={agent_id}, session={data.session_id}, "
        f"user={current_user.id}, env_type={env_type}, re_entry={is_reentry}"
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
                locked_env_type = _get_session_env_type(str(agent_id), data.session_id)
                client = await _get_client(agent_id, data.session_id, env_type=locked_env_type)
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
        # Reset browser initialization flag so the next agentbay browser tool
        # call re-initializes the SDK's browser.operator. This clears any stale
        # page references left by TC's CDP interactions that would otherwise
        # cause browser.operator.navigate to hang indefinitely.
        from app.services.agentbay_client import _agentbay_sessions
        for _img_type in ("browser", "browser_latest"):
            _ck = (agent_id, data.session_id, _img_type)
            if _ck in _agentbay_sessions:
                _tc_client, _ts = _agentbay_sessions[_ck]
                _tc_client._browser_initialized = False
                logger.info(
                    f"[TakeControl] Reset _browser_initialized after TC unlock "
                    f"for session={data.session_id[:8]}"
                )
        # Clear from the control-layer initialization tracking set as well
        _browser_initialized.discard((agent_id, data.session_id, "browser"))
        _browser_initialized.discard((agent_id, data.session_id, "browser_latest"))

    # Post-unlock CDP cleanup: cancel any in-progress navigations and release
    # held mouse buttons before the agent resumes its browser tool calls.
    await _tc_browser_cleanup(agent_id, data.session_id)

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
    # Build and execute a Node.js script to export ALL cookies via CDP.
    #
    # Key design decisions:
    # 1. We call context.cookies() WITHOUT a URL filter, which returns every cookie
    #    in the browser profile regardless of which page is currently open.
    # 2. We sanitize each cookie object before exporting:
    #    - Normalize 'sameSite' to the exact casing Playwright addCookies() expects
    #      ('Strict' | 'Lax' | 'None'). CDP returns lowercase; Playwright wants title-case.
    #    - Strip 'expires: -1' (session cookies) — Playwright will reject negative expiry.
    #    - Ensure 'domain' does NOT have a leading dot for addCookies() compatibility.
    #      (Playwright's addCookies prefers 'example.com' not '.example.com'.)
    import base64
    export_script = r"""
const { chromium } = require('/usr/local/lib/node_modules/playwright');
let browser;
(async () => {
    let ok = false;
    try {
        browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        // Fetch ALL cookies from the browser profile (no URL filter = full export)
        const rawCookies = await context.cookies();

        // Sanitize cookies so they can be re-injected by Playwright's addCookies()
        const sameSiteMap = { none: 'None', lax: 'Lax', strict: 'Strict' };
        const cookies = rawCookies.map(c => {
            const out = { ...c };
            // Normalize sameSite casing
            if (out.sameSite != null) {
                out.sameSite = sameSiteMap[String(out.sameSite).toLowerCase()] || 'Lax';
            }
            // Remove negative or zero expires (session cookies) — addCookies rejects them
            if (out.expires != null && out.expires <= 0) {
                delete out.expires;
            }
            // Ensure domain has leading dot so it matches subdomains.
            // Playwright's context.cookies() strips the leading dot from
            // domain cookies, turning them into host-only. Chrome's CDP
            // Network.setCookie needs the dot to match subdomains (e.g.,
            // ".xiaohongshu.com" matches www.xiaohongshu.com).
            if (out.domain && !out.domain.startsWith('.')) {
                out.domain = '.' + out.domain;
            }
            return out;
        });

        console.log('COOKIES_EXPORT:' + JSON.stringify(cookies));
        ok = true;
    } catch (e) {
        console.error('EXPORT_FAIL:' + e.message);
    } finally {
        if (browser) await browser.close().catch(() => {});
    }
    process.exit(ok ? 0 : 1);
})();
"""
    # Use base64 encoding to write script to current directory (not /tmp, which may lack write perms)
    script_b64 = base64.b64encode(export_script.encode('utf-8')).decode('ascii')
    write_result = await client.command_exec(
        f"echo '{script_b64}' | /usr/bin/base64 -d > tc_export_cookies.js"
    )
    logger.info(f"[TakeControl] Cookie export script write: success={write_result.get('success')}, stderr={write_result.get('stderr', '')[:100]}")
    
    result = await client.command_exec("node tc_export_cookies.js", timeout_ms=15000)
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    logger.info(f"[TakeControl] Cookie export script exec: success={result.get('success')}, stdout_len={len(stdout)}, stderr={stderr[:200]}")

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
