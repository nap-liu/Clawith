from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from app.services.agentbay_client import AgentBayClient


IsPlausibleApiKey = Callable[[str | None], bool]
GetAgentbayApiKey = Callable[..., Awaitable[str | None]]
InjectCredentials = Callable[[Any, uuid.UUID], Awaitable[None]]


def _is_plausible_agentbay_api_key(value: str | None) -> bool:
    """AgentBay API keys use an akm-* token format.

    This keeps encrypted blobs that failed to decrypt from being treated as
    plaintext keys and sent to AgentBay, where they surface as
    "invalid apiKey or token".
    """
    return bool(isinstance(value, str) and value.strip().startswith("akm-"))


async def get_agentbay_api_key_for_agent(
    agent_id: uuid.UUID,
    *,
    db=None,
    is_plausible_agentbay_api_key: IsPlausibleApiKey,
) -> str | None:
    """Return the configured AgentBay API key for the given agent.

    Resolution order:
    1. Per-agent ChannelConfig (channel_type='agentbay') — set via Agent detail page
    2. Global Tool.config.api_key (category='agentbay') — set via Company Settings
    """
    from app.config import get_settings
    from app.core.security import decrypt_data
    from app.database import async_session
    from app.models.channel_config import ChannelConfig
    from app.models.tool import Tool
    from sqlalchemy import select

    async def _fetch(session):
        result = await session.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "agentbay",
                ChannelConfig.is_configured == True,
            )
        )
        config = result.scalar_one_or_none()
        if config and config.app_secret:
            try:
                candidate = decrypt_data(config.app_secret, get_settings().SECRET_KEY)
            except Exception:
                candidate = config.app_secret
            if is_plausible_agentbay_api_key(candidate):
                return candidate

        candidate_tools: list[Tool] = []
        tool_result = await session.execute(
            select(Tool).where(
                Tool.name == "agentbay_browser_navigate",
                Tool.enabled == True,
            ).limit(1)
        )
        tool = tool_result.scalar_one_or_none()
        if tool:
            candidate_tools.append(tool)

        all_result = await session.execute(
            select(Tool).where(
                Tool.category == "agentbay",
                Tool.enabled == True,
            ).order_by(Tool.name)
        )
        candidate_tools.extend(
            candidate
            for candidate in all_result.scalars().all()
            if not tool or candidate.id != tool.id
        )

        for candidate_tool in candidate_tools:
            if not (candidate_tool.config and candidate_tool.config.get("api_key")):
                continue
            api_key = candidate_tool.config["api_key"]
            try:
                candidate = decrypt_data(api_key, get_settings().SECRET_KEY)
            except Exception:
                candidate = api_key
            if is_plausible_agentbay_api_key(candidate):
                return candidate

        return None

    if db:
        return await _fetch(db)
    async with async_session() as session:
        return await _fetch(session)


async def test_agentbay_channel(
    agent_id: uuid.UUID,
    current_user,
    db,
    *,
    get_agentbay_api_key_for_agent: GetAgentbayApiKey,
) -> dict[str, Any]:
    """Test AgentBay connectivity."""
    key = await get_agentbay_api_key_for_agent(agent_id, db=db)
    if not key:
        return {"ok": False, "error": "AgentBay not configured"}
    try:
        from agentbay import AgentBay, CreateSessionParams

        sdk = AgentBay(api_key=key)
        result = await asyncio.to_thread(
            sdk.create,
            CreateSessionParams(image_id="linux_latest"),
        )
        if result.success:
            if result.session:
                await asyncio.to_thread(result.session.delete)
            return {"ok": True, "message": "✅ Successfully connected to AgentBay API"}
        return {"ok": False, "error": result.error_message}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def get_agentbay_client_for_agent(
    agent_id: uuid.UUID,
    image_type: str,
    *,
    session_id: str = "",
    agentbay_sessions: MutableMapping[tuple[uuid.UUID, str, str], tuple[Any, datetime]],
    agentbay_session_timeout: timedelta,
    agentbay_client_cls: type[Any],
    get_agentbay_api_key_for_agent: GetAgentbayApiKey,
    is_plausible_agentbay_api_key: IsPlausibleApiKey,
    inject_credentials: InjectCredentials,
) -> AgentBayClient:
    """Get or create AgentBay client for agent."""

    now = datetime.now()
    cache_key = (agent_id, session_id, image_type)

    if cache_key in agentbay_sessions:
        client, last_used = agentbay_sessions[cache_key]
        if now - last_used < agentbay_session_timeout:
            agentbay_sessions[cache_key] = (client, now)
            return client
        logger.info(
            f"[AgentBay] Session expired for {image_type} (session={session_id[:8]}), closing"
        )
        await client.close_session()
        del agentbay_sessions[cache_key]

    from app.services.agent_tools import _get_tool_config

    tool_config = await _get_tool_config(agent_id, "agentbay_browser_navigate")
    api_key = None

    if tool_config and tool_config.get("api_key"):
        api_key = tool_config.get("api_key")
        from app.config import get_settings
        from app.core.security import decrypt_data

        try:
            api_key = decrypt_data(api_key, get_settings().SECRET_KEY)
        except Exception:
            pass
        if not is_plausible_agentbay_api_key(api_key):
            api_key = None

    if not api_key:
        api_key = await get_agentbay_api_key_for_agent(agent_id)

    if not api_key:
        raise RuntimeError("AgentBay not configured for this agent. Please configure in Tools > AgentBay.")

    client = agentbay_client_cls(api_key)

    if image_type == "browser":
        await client.create_session("browser_latest")
        await inject_credentials(client, agent_id)
    elif image_type == "computer":
        os_type = (tool_config or {}).get("os_type", "windows")
        computer_image = "windows_latest" if os_type == "windows" else "linux_latest"
        logger.info(
            f"[AgentBay] Creating computer session with OS: {os_type} "
            f"(image: {computer_image}) for session={session_id[:8]}"
        )
        await client.create_session(computer_image)
    else:
        await client.create_session("code_latest")

    agentbay_sessions[cache_key] = (client, now)
    return client


async def cleanup_agentbay_sessions(
    *,
    agentbay_sessions: MutableMapping[tuple[uuid.UUID, str, str], tuple[Any, datetime]],
    agentbay_session_timeout: timedelta,
) -> None:
    """Clean up expired AgentBay sessions."""
    now = datetime.now()
    expired = [
        cache_key
        for cache_key, (client, last_used) in agentbay_sessions.items()
        if now - last_used > agentbay_session_timeout
    ]
    for cache_key in expired:
        client, _ = agentbay_sessions.pop(cache_key)
        agent_id, session_id, image_type = cache_key
        logger.info(
            f"[AgentBay] Cleaning up expired {image_type} session for agent {agent_id} "
            f"(session={session_id[:8]})"
        )
        await client.close_session()


async def inject_credentials(client: AgentBayClient, agent_id: uuid.UUID) -> None:
    """Inject stored cookies into the browser via CDP after initialization."""
    import base64 as _base64
    import json

    from app.config import get_settings
    from app.core.security import decrypt_data
    from app.database import async_session as async_session_factory
    from app.models.agent_credential import AgentCredential
    from sqlalchemy import select

    settings = get_settings()

    try:
        async with async_session_factory() as db:
            result = await db.execute(
                select(AgentCredential).where(
                    AgentCredential.agent_id == agent_id,
                    AgentCredential.status == "active",
                    AgentCredential.cookies_json.isnot(None),
                )
            )
            credentials = result.scalars().all()
    except Exception as e:
        logger.warning(f"[AgentBay] Failed to query credentials for injection: {e}")
        return

    if not credentials:
        return

    all_cookies = []
    for cred in credentials:
        try:
            raw = decrypt_data(cred.cookies_json, settings.SECRET_KEY)
            cookies = json.loads(raw)
            if isinstance(cookies, list):
                all_cookies.extend(cookies)
        except Exception as e:
            logger.warning(f"[AgentBay] Failed to decrypt cookies for {cred.platform}: {e}")

    if not all_cookies:
        return

    try:
        await client._ensure_browser_initialized()
    except Exception as e:
        logger.warning(f"[AgentBay] Cannot inject cookies — browser not initialized: {e}")
        return

    cookies_json_str = json.dumps(all_cookies)
    inject_script = r"""
const { chromium } = require('/usr/local/lib/node_modules/playwright');
(async () => {
    try {
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const rawCookies = """ + cookies_json_str + r""";

        const sameSiteMap = { none: 'None', lax: 'Lax', strict: 'Strict' };
        const cookies = rawCookies.map(c => {
            const out = { ...c };
            if (out.sameSite != null) {
                out.sameSite = sameSiteMap[String(out.sameSite).toLowerCase()] || 'Lax';
            }
            if (out.expires != null && out.expires <= 0) {
                delete out.expires;
            }
            if (out.domain && !out.domain.startsWith('.')) {
                out.domain = '.' + out.domain;
            }
            return out;
        });

        let injected = 0;
        let failed = 0;
        for (const cookie of cookies) {
            try {
                await context.addCookies([cookie]);
                injected++;
            } catch (e) {
                failed++;
                if (failed <= 3) {
                    console.error('INJECT_SKIP:' + e.message + ' cookie=' + JSON.stringify(cookie).slice(0, 200));
                }
            }
        }
        console.log('INJECT_OK:' + injected + ' injected, ' + failed + ' skipped');
        process.exit(0);
    } catch (e) {
        console.error('INJECT_FAIL:' + e.message);
        process.exit(1);
    }
})();
"""

    try:
        script_b64 = _base64.b64encode(inject_script.encode("utf-8")).decode("ascii")
        write_result = await asyncio.to_thread(
            client._session.command.exec,
            f"echo '{script_b64}' | /usr/bin/base64 -d > tc_inject_cookies.js",
        )
        write_ok = getattr(write_result, "success", False)
        logger.info(f"[AgentBay] Cookie inject script write: success={write_ok}")

        exec_result = await asyncio.to_thread(
            client._session.command.exec,
            "node tc_inject_cookies.js",
            timeout_ms=15000,
        )
        stdout = getattr(exec_result, "stdout", "") or getattr(exec_result, "output", "") or ""
        stderr = getattr(exec_result, "stderr", "") or ""

        if "INJECT_OK" in stdout:
            logger.info(
                f"[AgentBay] Cookie injection successful for agent {agent_id}: "
                f"{stdout.strip()[:100]}"
            )
            try:
                from datetime import timezone as tz

                now = datetime.now(tz.utc)
                async with async_session_factory() as db:
                    for cred in credentials:
                        cred.last_injected_at = now
                        db.add(cred)
                    await db.commit()
            except Exception as e:
                logger.warning(f"[AgentBay] Failed to update last_injected_at: {e}")
        else:
            logger.warning(
                f"[AgentBay] Cookie injection may have failed: "
                f"stdout={stdout[:200]}, stderr={stderr[:200]}"
            )
    except Exception as e:
        logger.warning(f"[AgentBay] Cookie injection error: {e}")
