"""DingTalk Channel API routes.

Provides Config CRUD and message handling for DingTalk bots using Stream mode.

Known limitation — outbound quoted reply (Phase 2 #3, 2026-05-08):
    DingTalk's robot APIs do NOT expose a "reply to a specific message" /
    "quote message" capability. We confirmed this directly from the
    official docs:

      https://open.dingtalk.com/document/dingstart/robot-reply-and-send-messages
        > "机器人回复消息本质上就是机器人发送消息的过程。因此本文中,
        >  回复消息和发送消息具有相同的含义。"
        (Robot "reply" is literally a synonym for "send"; there is no
         thread/quote semantics.)

      https://open.dingtalk.com/document/development/the-robot-sends-a-group-message
        Body schema: {msgParam, msgKey, openConversationId, robotCode,
                      coolAppCode}. No quoteMessageId / parentMessageId /
        replyTo field of any kind.

    All msgKey templates (sampleText / sampleMarkdown / sampleActionCard /
    etc., enumerated at /document/dingstart/types-of-messages-sent-by-robots)
    likewise carry no quote-related field.

    Workaround possibilities considered and rejected:
      - markdown `> blockquote` to *visually* echo the user's text:
        rejected because it looks like a real quoted reply but does not
        link back to the source message in the DingTalk UI, which is
        actively misleading.

    This limitation applies only to outbound provider-native quoting. Inbound
    user replies can carry ``text.repliedMsg`` and are normalized by the Stream
    adapter so the quoted content remains available to the shared turn loop.

    Feishu's outbound quoted reply ships in feishu_service.send_message via the
    POST /open-apis/im/v1/messages/{message_id}/reply endpoint — see that
    function's docstring. Until DingTalk OpenAPI gains an equivalent,
    DingTalk replies stay plain.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.channel_config import ChannelConfigPublic as ChannelConfigOut
from app.services.chat_attachments import attachment_from_workspace_path
from app.services.dingtalk_quoted_message import has_trusted_dingtalk_sender_alias
from app.services.quoted_message import resolve_quoted_message_sender

router = APIRouter(tags=["dingtalk"])


async def _deliver_dingtalk_command_reply(
    *,
    message_id,
    agent_id: uuid.UUID,
    conversation_id: str,
    external_conv_id: str,
    is_group: bool,
    message: str,
):
    """Deliver and finalize one already-persisted command reply via OpenAPI."""
    from app.services.im_delivery import (
        IMDeliveryResult,
        deliver_persisted_message,
        register_delivery,
    )
    from app.services.turn_runtime import TurnRuntime

    try:
        result = await deliver_persisted_message(
            message_id=message_id,
            agent_id=agent_id,
            runtime=TurnRuntime(
                session_found=True,
                source_channel="dingtalk",
                conversation_id=conversation_id,
                external_conv_id=external_conv_id,
                is_group=is_group,
            ),
            message=message,
        )
    except Exception as exc:
        result = IMDeliveryResult.from_exception("dingtalk", exc)
        await register_delivery(message_id, result)
    return result


async def _get_tenant_dingtalk_provider(db: AsyncSession, tenant_id: uuid.UUID | None):
    """Resolve the active DingTalk identity provider for exactly one tenant."""
    if tenant_id is None:
        return None

    from app.services.identity_provider_lookup import (
        build_identity_provider_query,
        choose_preferred_identity_provider,
    )

    result = await db.execute(
        build_identity_provider_query("dingtalk", tenant_id, is_active=True)
    )
    return choose_preferred_identity_provider(
        result.scalars().all(),
        provider_type="dingtalk",
        tenant_id=str(tenant_id),
    )


def _resolve_dingtalk_directory_credentials(
    provider,
    channel_config: ChannelConfig | None,
) -> list[tuple[str, str, str]]:
    """Return enterprise credentials followed by the current agent fallback."""
    credentials: list[tuple[str, str, str]] = []
    if provider is not None:
        config = provider.config or {}
        app_key = config.get("app_key") or config.get("appkey") or config.get("app_id")
        app_secret = (
            config.get("app_secret")
            or config.get("appsecret")
            or config.get("app_secret_key")
        )
        if app_key and app_secret:
            credentials.append((str(app_key), str(app_secret), "enterprise"))

    if channel_config and channel_config.app_id and channel_config.app_secret:
        robot_pair = (channel_config.app_id, channel_config.app_secret)
        if not credentials or credentials[0][:2] != robot_pair:
            credentials.append((*robot_pair, "robot_fallback"))
    return credentials


async def _get_corp_access_token(app_key: str, app_secret: str) -> str | None:
    """Get corp access_token via global DingTalkTokenManager (shared with stream/reaction)."""
    from app.services.dingtalk_token import dingtalk_token_manager
    return await dingtalk_token_manager.get_token(app_key, app_secret)


async def _get_dingtalk_user_detail(
    app_key: str,
    app_secret: str,
    staff_id: str,
) -> dict | None:
    """Query DingTalk user detail via corp API for directory identity/profile data.

    Uses /topapi/v2/user/get, requires contact.user.read permission.
    Returns None on failure (graceful degradation).
    """
    import httpx

    try:
        access_token = await _get_corp_access_token(app_key, app_secret)
        if not access_token:
            return None

        async with httpx.AsyncClient(timeout=10) as client:
            user_resp = await client.post(
                "https://oapi.dingtalk.com/topapi/v2/user/get",
                params={"access_token": access_token},
                json={"userid": staff_id, "language": "zh_CN"},
            )
            user_data = user_resp.json()

            if user_data.get("errcode") != 0:
                logger.warning(
                    "[DingTalk] /topapi/v2/user/get failed: errcode={} errmsg={}",
                    user_data.get("errcode"),
                    user_data.get("errmsg"),
                )
                return None

            result = user_data.get("result", {})
            return {
                "name": result.get("name", ""),
                "unionid": result.get("unionid", ""),
                # Keep the raw fields separate through reconciliation.  A
                # collapsed fallback would hide email/org_email disagreement.
                "mobile": result.get("mobile"),
                "email": result.get("email"),
                "org_email": result.get("org_email"),
            }

    except Exception as exc:
        logger.warning(
            "[DingTalk] _get_dingtalk_user_detail error_type={}",
            type(exc).__name__,
        )
        return None


async def _get_dingtalk_user_detail_with_fallback(
    credentials: list[tuple[str, str, str]],
    staff_id: str,
    provider_id: uuid.UUID | None = None,
) -> dict | None:
    """Use enterprise credentials first and merge an agent fallback response."""
    merged: dict[str, str] = {}
    for app_key, app_secret, source in credentials:
        detail = await _get_dingtalk_user_detail(app_key, app_secret, staff_id)
        if not detail:
            logger.warning(
                "[DingTalk] Directory enrichment failed via source={}; trying fallback if available",
                source,
            )
            continue

        for field in ("name", "unionid", "mobile", "email", "org_email"):
            if not merged.get(field) and detail.get(field):
                merged[field] = detail[field]

        logger.info(
            "[DingTalk] Directory enrichment source={} provider_id={} "
            "has_unionid={} has_mobile={} has_email={}",
            source,
            provider_id,
            bool(merged.get("unionid")),
            bool(merged.get("mobile")),
            bool(merged.get("email") or merged.get("org_email")),
        )
        if merged.get("mobile"):
            return merged

        logger.warning(
            "[DingTalk] Directory enrichment via source={} returned no mobile; "
            "trying fallback if available",
            source,
        )

    return merged or None


# ─── Config CRUD ────────────────────────────────────────

@router.post("/agents/{agent_id}/dingtalk-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_dingtalk_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure DingTalk bot for an agent. Fields: app_key, app_secret, agent_id (optional)."""
    _, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Manage access is required to configure channel")

    app_key = data.get("app_key", "").strip()
    app_secret = data.get("app_secret", "").strip()
    if not app_key or not app_secret:
        raise HTTPException(status_code=422, detail="app_key and app_secret are required")

    # Handle connection mode (Stream/WebSocket vs Webhook) and agent_id
    extra_config = data.get("extra_config", {})
    conn_mode = extra_config.get("connection_mode", "websocket")
    dingtalk_agent_id = extra_config.get("agent_id", "")  # DingTalk AgentId for API messaging

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "dingtalk",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.app_id = app_key
        existing.app_secret = app_secret
        existing.is_configured = True
        existing.extra_config = {**existing.extra_config, "connection_mode": conn_mode, "agent_id": dingtalk_agent_id}
        await db.flush()

        # Restart Stream client if in websocket mode
        if conn_mode == "websocket":
            from app.services.dingtalk_stream import dingtalk_stream_manager
            import asyncio
            asyncio.create_task(dingtalk_stream_manager.start_client(agent_id, app_key, app_secret))
        else:
            # Stop existing Stream client if switched to webhook
            from app.services.dingtalk_stream import dingtalk_stream_manager
            import asyncio
            asyncio.create_task(dingtalk_stream_manager.stop_client(agent_id))

        return ChannelConfigOut.model_validate(existing)

    config = ChannelConfig(
        agent_id=agent_id,
        channel_type="dingtalk",
        app_id=app_key,
        app_secret=app_secret,
        is_configured=True,
        extra_config={"connection_mode": conn_mode, "agent_id": dingtalk_agent_id},
    )
    db.add(config)
    await db.commit()

    # Start Stream client if in websocket mode
    if conn_mode == "websocket":
        from app.services.dingtalk_stream import dingtalk_stream_manager
        import asyncio
        asyncio.create_task(dingtalk_stream_manager.start_client(agent_id, app_key, app_secret))

    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/dingtalk-channel", response_model=ChannelConfigOut)
async def get_dingtalk_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "dingtalk",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="DingTalk not configured")
    return ChannelConfigOut.model_validate(config)


@router.delete("/agents/{agent_id}/dingtalk-channel", status_code=204)
async def delete_dingtalk_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Manage access is required to remove channel")
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "dingtalk",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="DingTalk not configured")
    await db.delete(config)

    # Stop Stream client
    from app.services.dingtalk_stream import dingtalk_stream_manager
    import asyncio
    asyncio.create_task(dingtalk_stream_manager.stop_client(agent_id))


# ─── Message Dedup (防止钉钉重传导致重复处理) ─────────────

_processed_messages: dict[str, float] = {}  # {message_id: timestamp}
_dedup_check_counter: int = 0


async def _check_message_dedup(message_id: str) -> bool:
    """Check if a message_id has already been processed. Returns True if duplicate.

    Uses Redis SETNX as primary (atomic, cross-process), falls back to in-memory dict.
    """
    global _dedup_check_counter

    if not message_id:
        return False

    # Try Redis first
    try:
        from app.core.events import get_redis
        redis_client = await get_redis()
        dedup_key = f"dingtalk:dedup:{message_id}"
        # SETNX + EX: set only if not exists, expire in 300s
        was_set = await redis_client.set(dedup_key, "1", ex=300, nx=True)
        if not was_set:
            logger.info(f"[DingTalk Dedup] Duplicate message_id={message_id} (Redis)")
            return True
        return False
    except Exception:
        pass  # Redis unavailable, fall back to in-memory

    # In-memory fallback
    import time as _time_dedup
    now = _time_dedup.time()

    if message_id in _processed_messages:
        if now - _processed_messages[message_id] < 300:  # 5 minutes
            logger.info(f"[DingTalk Dedup] Duplicate message_id={message_id} (memory)")
            return True

    _processed_messages[message_id] = now

    # Periodic cleanup (every 100 checks, remove entries older than 10 minutes)
    _dedup_check_counter += 1
    if _dedup_check_counter % 100 == 0:
        cutoff = now - 600
        expired = [k for k, v in _processed_messages.items() if v < cutoff]
        for k in expired:
            del _processed_messages[k]
        if expired:
            logger.debug(f"[DingTalk Dedup] Cleaned {len(expired)} expired entries")

    return False


# ─── Message Processing (called by Stream callback) ────

async def process_dingtalk_message(
    agent_id: uuid.UUID,
    sender_staff_id: str,
    user_text: str,
    conversation_id: str,
    conversation_type: str,
    saved_file_paths: list[str] | None = None,
    sender_nick: str = "",
    message_id: str = "",
    sender_id: str = "",
    chatbot_user_id: str = "",
    conversation_title: str = "",
    channel_reactions=None,
    quoted_message: dict | None = None,
):
    from app.api import dingtalk_message_processing as _message_processing

    for _name in (
        "_get_tenant_dingtalk_provider",
        "_resolve_dingtalk_directory_credentials",
        "_get_dingtalk_user_detail_with_fallback",
        "_deliver_dingtalk_command_reply",
    ):
        setattr(_message_processing, _name, globals()[_name])
    return await _message_processing.process_dingtalk_message(
        agent_id=agent_id,
        sender_staff_id=sender_staff_id,
        user_text=user_text,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        saved_file_paths=saved_file_paths,
        sender_nick=sender_nick,
        message_id=message_id,
        sender_id=sender_id,
        chatbot_user_id=chatbot_user_id,
        conversation_title=conversation_title,
        channel_reactions=channel_reactions,
        quoted_message=quoted_message,
    )


# ─── OAuth Callback (SSO) ──────────────────────────────

@router.get("/auth/dingtalk/callback")
async def dingtalk_callback(
    authCode: str, # DingTalk uses authCode parameter
    request: Request,
    state: str = None,
    db: AsyncSession = Depends(get_db),
):
    """Callback for DingTalk OAuth2 login."""
    from app.models.identity import SSOScanSession
    from app.core.security import create_access_token
    from fastapi.responses import RedirectResponse
    from app.services.auth_provider import DingTalkAuthProvider
    from app.services.sso_login_state import (
        get_enabled_sso_provider,
        parse_sso_or_legacy_state,
        sso_browser_cookie_name,
        sso_completion_url,
        sso_error_url,
        verify_sso_browser_binding,
    )

    # 1. Resolve session to get tenant context
    sid, provider_id, login_query = parse_sso_or_legacy_state(state)
    if sid is None or provider_id is None:
        return RedirectResponse(sso_error_url("invalid_state", login_query), status_code=302)
    if not verify_sso_browser_binding(sid, request.cookies.get(sso_browser_cookie_name(sid))):
        return RedirectResponse(sso_error_url("browser_mismatch", login_query), status_code=302)
    tenant_id = None
    if sid:
        s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
        session = s_res.scalar_one_or_none()
        if session and session.expires_at >= datetime.now(timezone.utc) and session.status in {"pending", "scanned"}:
            tenant_id = session.tenant_id
        else:
            return RedirectResponse(sso_error_url("invalid_session", login_query), status_code=302)

    # 2. Get DingTalk provider config
    provider = await get_enabled_sso_provider(db, provider_id, "dingtalk", tenant_id)
    if not provider:
        return RedirectResponse(sso_error_url("provider_unavailable", login_query), status_code=302)
    auth_provider = DingTalkAuthProvider(provider=provider, config=provider.config or {})

    # 3. Exchange code for token and get user info
    try:
        # Step 1: Exchange authCode for userAccessToken
        token_data = await auth_provider.exchange_code_for_token(authCode)
        access_token = token_data.get("access_token")
        if not access_token:
            logger.error(f"DingTalk token exchange failed: {token_data}")
            return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)

        # Step 2: Get user info using modern v1.0 API
        user_info = await auth_provider.get_user_info(access_token)
        if not user_info.provider_union_id:
            logger.error(f"DingTalk user info missing unionId: {user_info.raw_data}")
            return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)

        # Step 3: Find or create user (handles OrgMember linking)
        user, is_new = await auth_provider.find_or_create_user(
            db, user_info, tenant_id=str(tenant_id) if tenant_id else None
        )
        if not user:
            return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)

    except Exception as e:
        logger.error(f"DingTalk login error: {e}")
        return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)

    # 4. Standard login
    token = create_access_token(str(user.id), user.role)

    if sid:
        try:
            s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
            session = s_res.scalar_one_or_none()
            if session:
                session.status = "authorized"
                session.provider_type = "dingtalk"
                session.user_id = user.id
                session.access_token = token
                session.error_msg = None
                await db.commit()
                return RedirectResponse(sso_completion_url(sid, login_query), status_code=302)
        except Exception as e:
            logger.exception("Failed to update SSO session (dingtalk) %s", e)

    return RedirectResponse(sso_error_url("session_update_failed", login_query), status_code=302)
