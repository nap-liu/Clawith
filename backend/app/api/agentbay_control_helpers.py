"""Session registries and interaction helpers for AgentBay Take Control."""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel

from app.config import get_settings

logger = logging.getLogger("app.api.agentbay_control")

# ── In-memory Take Control lock registry ──
# Key: (agent_id_str, session_id_str) → (user_id, lock_timestamp, env_type)
# env_type: 'browser' | 'computer' | 'code' — which AgentBay environment the
# user is controlling. Stored at lock time so all subsequent TC endpoints can
# look up the correct session type without re-deriving it from the frontend.
_take_control_locks: dict[tuple[str, str], tuple[str, float, str]] = {}
_LOCK_TIMEOUT_SECONDS = 600  # Auto-expire stale locks after 10 minutes

# Cache of sessions that have already had browser initialization called.
# Avoids redundant _ensure_browser_initialized() on every screenshot poll.
_browser_initialized: set[tuple] = set()

# Per-session interaction locks to serialize concurrent TC interactions.
# Without this, two rapid clicks both write tc_action.js simultaneously,
# corrupting one script's execution. Each TC session gets its own Lock.
_tc_interaction_locks: dict[str, asyncio.Lock] = {}


def _get_interaction_lock(agent_id: uuid.UUID, session_id: str) -> asyncio.Lock:
    """Get or create the per-session asyncio.Lock for TC interactions."""
    key = f"{agent_id}:{session_id}"
    if key not in _tc_interaction_locks:
        _tc_interaction_locks[key] = asyncio.Lock()
    return _tc_interaction_locks[key]


def is_session_locked(agent_id: str, session_id: str) -> bool:
    """Check if a session is currently under human Take Control.

    Called by execute_tool to block automatic agentbay_* tool calls.
    Automatically clears expired locks.
    """
    key = (agent_id, session_id)
    if key not in _take_control_locks:
        return False
    _user_id, locked_at, _env_type = _take_control_locks[key]
    if time.time() - locked_at > _LOCK_TIMEOUT_SECONDS:
        logger.info(f"[TakeControl] Auto-expired stale lock for session={session_id[:8]}")
        del _take_control_locks[key]
        return False
    return True


def _get_session_env_type(agent_id: str, session_id: str) -> str:
    """Return the env_type stored in the lock registry for this session.

    Falls back to 'browser' if no lock entry is found (backward compat).
    """
    key = (agent_id, session_id)
    entry = _take_control_locks.get(key)
    if entry:
        _user_id, _locked_at, env_type = entry
        return env_type
    return "browser"


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


class DragRequest(BaseModel):
    """Mouse drag event forwarding — used for slider CAPTCHAs and drag-and-drop."""
    session_id: str
    from_x: int
    from_y: int
    to_x: int
    to_y: int
    duration_ms: int = 600  # Total drag duration in milliseconds


class ScreenshotRequest(BaseModel):
    """Request an immediate screenshot."""
    session_id: str


class LockRequest(BaseModel):
    """Enter Take Control mode."""
    session_id: str
    platform_hint: Optional[str] = None  # current page domain (for cookie export)
    env_type: Optional[str] = "browser"  # which env the user is controlling: browser | computer | code


class UnlockRequest(BaseModel):
    """Exit Take Control mode."""
    session_id: str
    export_cookies: bool = True  # whether to export cookies on exit
    platform_hint: Optional[str] = None  # domain to associate cookies with


# ── Helpers ──


async def _get_client(agent_id: uuid.UUID, session_id: str, env_type: str = "browser"):
    """Retrieve the AgentBay client for the given agent + session.

    Search order (most to least specific):
    1. Exact match: (agent_id, session_id, env_type) — fastest, correct in normal flow.
    2. Env-type preference, any session: search all cached sessions for this agent
       by env_type preference. This handles the common case where the TC frontend's
       session_id doesn't exactly match the session_id the agent used when it created
       the AgentBay session (e.g., new chat thread opened mid-task).
    3. Create new session: last resort — will show a blank desktop/browser.

    IMPORTANT: For browser sessions, this also calls _ensure_browser_initialized()
    because the browser SDK requires explicit initialization before screenshot/
    interaction APIs will work. Without this, get_browser_snapshot_base64() returns
    None ("Browser not initialized") and all CDP-based interactions fail silently.
    """
    from app.services.agentbay_client import _agentbay_sessions, _AGENTBAY_SESSION_TIMEOUT
    from datetime import datetime

    now = datetime.now()

    # Build search order: requested env_type first, then the rest as fallback
    all_types = ["browser", "computer", "code"]
    search_order = [env_type] + [t for t in all_types if t != env_type]

    # ── Phase 1: Exact (agent_id, session_id, env_type) match ──
    for image_type in search_order:
        cache_key = (agent_id, session_id, image_type)
        if cache_key in _agentbay_sessions:
            client, last_used = _agentbay_sessions[cache_key]
            if now - last_used < _AGENTBAY_SESSION_TIMEOUT:
                _agentbay_sessions[cache_key] = (client, now)
                logger.info(
                    f"[TakeControl] Found existing {image_type} session (exact match) for "
                    f"agent={agent_id}, session={session_id[:8]} "
                    f"(requested env_type={env_type})"
                )
                if image_type in ("browser", "browser_latest") and cache_key not in _browser_initialized:
                    try:
                        await client._ensure_browser_initialized()
                        _browser_initialized.add(cache_key)
                    except Exception as e:
                        logger.warning(f"[TakeControl] Browser init on cached session failed: {e}")
                return client

    # ── Phase 2: Fallback — search all sessions for this agent by env_type preference ──
    # The TC frontend session_id may not match the session_id that the agent used
    # when it created the AgentBay session (e.g., agent started in conversation A,
    # user opens TC from conversation B). We still want to connect to the agent's
    # ACTIVE session rather than spin up a blank new one.
    best_client = None
    best_image_type = None
    best_cache_key = None
    best_ts = None

    # Scan all cached sessions; prefer env_type match and most-recently-used
    for img_type in search_order:
        for (ag_id, sess_id, it), (client, last_used) in list(_agentbay_sessions.items()):
            if ag_id != agent_id or it != img_type:
                continue
            if now - last_used >= _AGENTBAY_SESSION_TIMEOUT:
                continue
            # Pick the most recently used session among candidates
            if best_ts is None or last_used > best_ts:
                best_client = client
                best_image_type = it
                best_cache_key = (ag_id, sess_id, it)
                best_ts = last_used
        if best_client:
            break  # Found a match for preferred env_type — stop

    if best_client:
        # Refresh the timestamp so this session stays warm
        _agentbay_sessions[best_cache_key] = (best_client, now)
        logger.info(
            f"[TakeControl] Found existing {best_image_type} session (agent-id fallback) for "
            f"agent={agent_id} (requested session={session_id[:8]}, "
            f"actual session={best_cache_key[1][:8]}, env_type={env_type})"
        )
        if best_image_type in ("browser", "browser_latest") and best_cache_key not in _browser_initialized:
            try:
                await best_client._ensure_browser_initialized()
                _browser_initialized.add(best_cache_key)
            except Exception as e:
                logger.warning(f"[TakeControl] Browser init on fallback session failed: {e}")
        return best_client

    # ── Phase 3: No cached session found — create a new session ──
    # This is a last resort: the agent has no active AgentBay session at all.
    # The resulting session will show a blank browser/desktop until the agent
    # starts using it via its tools.
    from app.services.agentbay_client import get_agentbay_client_for_agent

    logger.warning(
        f"[TakeControl] No cached AgentBay session found for agent={agent_id} "
        f"(env_type={env_type}). Creating new session — will show blank screen."
    )
    try:
        client = await get_agentbay_client_for_agent(
            agent_id, image_type=env_type, session_id=session_id
        )
        if env_type == "browser":
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
            detail=f"No active {env_type} session found: {e}",
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


async def _eval_cdp_script(client, script_body: str) -> dict:
    """Evaluate a Node.js Playwright CDP script in the browser container."""
    import base64
    try:
        # Base64 encode the script to avoid shell escaping issues inside the container
        script_b64 = base64.b64encode(script_body.encode('utf-8')).decode('ascii')

        # Write base64 to file and decode it to tc_action.js (in current working dir, since /tmp might be restricted)
        cmd_write = f"echo '{script_b64}' | /usr/bin/base64 -d > tc_action.js"
        await asyncio.to_thread(client._session.command.exec, cmd_write)

        # Execute the script
        result = await asyncio.to_thread(client._session.command.exec, "node tc_action.js")

        success = getattr(result, 'success', False)
        output = getattr(result, 'output', '') or getattr(result, 'stdout', '') or ''
        stderr = getattr(result, 'stderr', '') or ''

        if not success:
            logger.error(f"[TakeControl] CDP execution failed. Output: {output}, Stderr: {stderr}")
            return {"success": False, "output": f"Node error: {stderr[:200]}"}

        return {"success": True, "output": output}
    except Exception as e:
        logger.error(f"[TakeControl] CDP exception: {e}")
        return {"success": False, "output": str(e)}


async def _tc_browser_cleanup(agent_id: uuid.UUID, session_id: str) -> None:
    """Best-effort cleanup immediately after Take Control exits.

    Uses the AgentBay SDK's own browser.operator.navigate() to navigate to
    about:blank. This goes through the SERVICE'S Playwright instance (not a
    new connectOverCDP connection), so there's no competing CDP session,
    no Target.attachToTarget/detachFromTarget events, and no risk of confusing
    the service's internal page state.

    IMPORTANT: Previous approaches that used connectOverCDP + browser.close()
    for cleanup were sending Target.detachFromTarget events to Chrome while
    navigation was in progress. The AgentBay service's Playwright received
    these detach events mid-navigation, which put its internal state machine
    into a 60-second recovery loop before it could accept the next page.goto().
    """
    from app.services.agentbay_client import _agentbay_sessions

    cleanup_client = None
    for img_type in ("browser", "browser_latest"):
        ck = (agent_id, session_id, img_type)
        if ck in _agentbay_sessions:
            cleanup_client = _agentbay_sessions[ck][0]
            break
    if not cleanup_client:
        return

    try:
        # Cleanup strategy: stop all in-flight page navigations, then navigate
        # the active content page to about:blank.
        #
        # WHY multi-step:
        # 1. stopLoading on all pages: a TC click may have opened a NEW TAB
        #    (target=_blank link on baidu) that is still loading a heavy article.
        #    Page.stopLoading kills that load immediately so Chrome's DevTools
        #    is no longer blocked draining a multi-MB response.
        # 2. Page.navigate to about:blank on the active page: gives the AgentBay
        #    service's page.goto() a clean starting point. about:blank commits in
        #    <10ms; the service no longer has to wait for tieba/zhihu/baidu to drain.
        # 3. Wait for Page.loadEventFired before process.exit(): ensures Chrome has
        #    fully settled at about:blank before we disconnect. This means Chrome
        #    emits Target.detachedFromTarget (from our WebSocket close) while the
        #    page is in a stable, loaded state — not mid-navigation — so the
        #    service's Playwright state machine doesn't enter a 60-second recovery.
        # 4. No browser.close(): we let Node.js exit naturally. Chrome handles
        #    the WebSocket close without an explicit Target.detachFromTarget CDP
        #    command that races with other async CDP events.
        cleanup_script = """
const { chromium } = require('/usr/local/lib/node_modules/playwright');
(async () => {
    try {
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const allPages = context.pages();

        // Stop all loading pages so Chrome is not draining heavy responses.
        // tc clicks frequently open new tabs (target=_blank) that stay loading
        // for 20-40s; stopping them is critical for fast post-TC recovery.
        for (const p of allPages) {
            try {
                const cdp = await context.newCDPSession(p);
                await cdp.send('Page.stopLoading');
                await cdp.detach();
            } catch(_) {}
        }

        // Navigate the active content page (last non-blank) to about:blank.
        // Use raw CDP Page.navigate — the AgentBay SDK rejects about:blank
        // ("must start with http or https") but Chrome's CDP has no such rule.
        const contentPage = allPages.slice().reverse().find(p => p.url() !== 'about:blank')
                            || allPages[allPages.length - 1];
        const cdp = await context.newCDPSession(contentPage);

        // Navigate and wait for loadEventFired so about:blank is fully settled.
        await new Promise((resolve) => {
            cdp.on('Page.loadEventFired', () => resolve());
            cdp.send('Page.navigate', { url: 'about:blank' }).catch(() => resolve());
            setTimeout(resolve, 800);  // Fallback: about:blank always loads in <100ms
        });

        console.log('CLEANUP_OK');
    } catch(e) {
        console.error('CLEANUP_FAIL: ' + e.message);
    }
    // No browser.close() — let Chrome handle WebSocket close gracefully after
    // the page is in a stable loaded state (about:blank).
    process.exit(0);
})();
"""
        res = await _eval_cdp_script(cleanup_client, cleanup_script)
        logger.info(
            f"[TakeControl] Cleanup: {res.get('output', 'no output')[:100]} "
            f"for session={session_id[:8]}"
        )
    except Exception as e:
        logger.warning(f"[TakeControl] Cleanup failed (non-fatal): {e}")


async def _perform_click(client, x: int, y: int, button: str = "left"):
    """Click at (x, y) on the remote session.

    Browser sessions use connectOverCDP because the Computer API's click_mouse
    tool is only available in the computer image type, not browser_latest.
    Each CDP script uses try/catch/finally with browser.close() to ensure a
    graceful disconnect so Chrome's DevTools session does not leak.
    """
    image_type = getattr(client, '_image_type', 'unknown')
    logger.info(f"[TakeControl] Click at ({x}, {y}), button={button}, image_type={image_type}")

    if _is_browser_session(client):
        script = f"""
const {{ chromium }} = require('/usr/local/lib/node_modules/playwright');
(async () => {{
    let ok = false;
    try {{
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const pages = context.pages();

        // Page selection: prefer the last page with a committed non-blank URL.
        // When a tc click opens a new tab (target=_blank), the new tab briefly
        // has url() === 'about:blank' before its navigation commits. During that
        // window, we correctly target the ORIGINAL content page (the one the user
        // sees in the TC screenshot). The NEXT click, after the new tab has settled,
        // will naturally pick the new tab because its URL will be non-blank by then.
        const page = pages.slice().reverse().find(p => p.url() !== 'about:blank')
                     || pages[pages.length - 1];
        const initialUrl = page.url();
        const initialPageCount = pages.length;
        console.log('TARGET_PAGE:' + initialUrl);

        await page.mouse.click({x}, {y}, {{ button: '{button}' }});
        console.log('CLICK_OK');
        ok = true;

        // Wait 2 seconds for any triggered navigation to commit before releasing
        // the interaction lock. This covers both cases:
        //   A) Same-tab navigation: URL commits in ~0.5-1s
        //   B) New-tab navigation (target=_blank): new tab URL transitions from
        //      about:blank to the target URL in ~1-2s
        //
        // WHY a fixed sleep instead of polling context.pages() every 200ms:
        // Polling makes ~20 CDP calls while Chrome is loading a heavy new tab.
        // Under that combined load, Chrome's DevTools HTTP server stops responding,
        // causing the NEXT connectOverCDP to time out with a 30-second error.
        // A passive sleep has zero CDP overhead and achieves the same goal.
        await new Promise(r => setTimeout(r, 2000));
    }} catch (e) {{
        console.error('CLICK_FAIL:' + e.message);
    }}
    // No browser.close() — avoid explicit Target.detachFromTarget.
    // Chrome handles the WebSocket close gracefully.
    process.exit(ok ? 0 : 1);
}})();
"""
        res = await _eval_cdp_script(client, script)
        return {"success": res.get("success", False) and "CLICK_OK" in res.get("output", ""), "method": "cdp_click", "output": "Clicked" if "CLICK_OK" in res.get("output", "") else res.get("output", "Unknown error")}

    # Desktop session — use Computer API
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

    Browser sessions use CDP keyboard API; desktop sessions use computer.input_text.
    """
    image_type = getattr(client, '_image_type', 'unknown')
    logger.info(f"[TakeControl] Type text: '{text[:30]}', image_type={image_type}")

    if _is_browser_session(client):
        import urllib.parse
        encoded_text = urllib.parse.quote(text)
        script = f"""
const {{ chromium }} = require('/usr/local/lib/node_modules/playwright');
(async () => {{
    let ok = false;
    try {{
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const pages = context.pages();
        const page = pages.slice().reverse().find(p => p.url() !== 'about:blank') || pages[pages.length - 1];
        const textToType = decodeURIComponent('{encoded_text}');
        await page.keyboard.type(textToType);
        console.log('TYPE_OK');
        ok = true;
    }} catch (e) {{
        console.error('TYPE_FAIL:' + e.message);
    }}
    // No browser.close() — avoid Target.detachFromTarget mid-navigation.
    process.exit(ok ? 0 : 1);
}})();
"""
        res = await _eval_cdp_script(client, script)
        return {"success": res.get("success", False) and "TYPE_OK" in res.get("output", ""), "method": "cdp_type", "output": "Text typed" if "TYPE_OK" in res.get("output", "") else res.get("output", "Unknown error")}

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

    Browser sessions use CDP keyboard API; desktop sessions use computer.press_keys.
    """
    key_desc = "+".join(keys)
    logger.info(f"[TakeControl] Press keys: {key_desc}")

    if _is_browser_session(client):
        # Convert key names to the Playwright format (e.g. 'ctrl' → 'Control')
        key_map = {
            'ctrl': 'Control', 'alt': 'Alt', 'shift': 'Shift', 'meta': 'Meta',
            'enter': 'Enter', 'backspace': 'Backspace', 'esc': 'Escape', 'tab': 'Tab',
        }
        playwright_keys = [key_map.get(k.lower(), k.upper() if len(k) == 1 else k) for k in keys]
        combined = "+".join(playwright_keys)
        script = f"""
const {{ chromium }} = require('/usr/local/lib/node_modules/playwright');
(async () => {{
    let ok = false;
    try {{
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const pages = context.pages();
        const page = pages.slice().reverse().find(p => p.url() !== 'about:blank') || pages[pages.length - 1];
        await page.keyboard.press('{combined}');
        console.log('PRESS_OK');
        ok = true;
    }} catch (e) {{
        console.error('PRESS_FAIL:' + e.message);
    }}
    // No browser.close() — avoid Target.detachFromTarget mid-navigation.
    process.exit(ok ? 0 : 1);
}})();
"""
        res = await _eval_cdp_script(client, script)
        return {"success": res.get("success", False) and "PRESS_OK" in res.get("output", ""), "method": "cdp_press", "output": f"Pressed {key_desc}" if "PRESS_OK" in res.get("output", "") else res.get("output", "Unknown error")}

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




async def _perform_drag(
    client, from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 600
) -> dict:
    """Simulate a human-like mouse drag using a Bezier curve trajectory.

    Browser sessions use CDP to send precise mouse events with a Bezier
    curve trajectory and sub-pixel jitter for CAPTCHA bypass.
    Desktop sessions use the Computer API move_mouse sequence.
    All CDP scripts use browser.close() for graceful disconnect.
    """
    logger.info(
        f"[TakeControl] Drag: ({from_x},{from_y}) -> ({to_x},{to_y}), "
        f"duration={duration_ms}ms"
    )

    if _is_browser_session(client):
        script = f"""
const {{ chromium }} = require('/usr/local/lib/node_modules/playwright');
let browser;
(async () => {{
    let ok = false;
    try {{
        browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const pages = context.pages();
        const page = pages.slice().reverse().find(p => p.url() !== 'about:blank') || pages[pages.length - 1];

        const steps = 30;
        const duration = {duration_ms};
        const x0 = {from_x}, y0 = {from_y};
        const x3 = {to_x},  y3 = {to_y};
        const dx = x3 - x0, dy = y3 - y0;
        const perpX = -dy * 0.15, perpY = dx * 0.15;
        const x1 = x0 + dx * 0.3 + perpX, y1 = y0 + dy * 0.3 + perpY;
        const x2 = x0 + dx * 0.7 - perpX, y2 = y0 + dy * 0.7 - perpY;
        const bezier = (t) => {{
            const u = 1 - t;
            return {{ x: u*u*u*x0+3*u*u*t*x1+3*u*t*t*x2+t*t*t*x3, y: u*u*u*y0+3*u*u*t*y1+3*u*t*t*y2+t*t*t*y3 }};
        }};
        await page.mouse.move(x0, y0);
        await page.mouse.down();
        for (let i = 1; i <= steps; i++) {{
            const pt = bezier(i / steps);
            const jx = (Math.random() - 0.5) * 2;
            const jy = (Math.random() - 0.5) * 2;
            await page.mouse.move(Math.round(pt.x + jx), Math.round(pt.y + jy));
            await new Promise(r => setTimeout(r, duration / steps));
        }}
        await page.mouse.move(x3, y3);
        await page.mouse.up();
        console.log('TC_OK: drag complete');
        ok = true;
    }} catch (e) {{
        console.error('TC_FAIL: ' + e.message);
    }}
    // No browser.close() — avoid Target.detachFromTarget mid-navigation.
    process.exit(ok ? 0 : 1);
}})();
"""
        res = await _eval_cdp_script(client, script)
        return {
            "success": res.get("success", False) and "TC_OK" in res.get("output", ""),
            "method": "cdp_drag",
            "output": f"Dragged ({from_x},{from_y}) -> ({to_x},{to_y})" if "TC_OK" in res.get("output", "") else res.get("output", "Unknown error"),
        }
