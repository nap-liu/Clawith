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
    """Process an incoming DingTalk bot message through durable OpenAPI routes.

    Args:
        saved_file_paths: List of local file paths where media files were saved.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select as _select
    from app.database import async_session
    from app.models.agent import Agent as AgentModel
    from app.models.user import User as UserModel
    from sqlalchemy.orm import selectinload as _selectinload
    from app.services.channel_session import find_or_create_channel_session
    from app.services.channel_llm import _call_agent_llm
    from app.services.im_thinking_output import BufferedIMThinkingSender, resolve_im_thinking_enabled

    async with async_session() as db:
        sender_staff_id = (sender_staff_id or "").strip()
        sender_id = (sender_id or "").strip()
        chatbot_user_id = (chatbot_user_id or "").strip()

        # Load agent
        agent_r = await db.execute(_select(AgentModel).where(AgentModel.id == agent_id))
        agent_obj = agent_r.scalar_one_or_none()
        if not agent_obj:
            logger.warning(f"[DingTalk] Agent {agent_id} not found")
            return
        if not sender_staff_id:
            logger.warning("[DingTalk] Skip message attribution because sender_staff_id is empty")
            return
        from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE
        ctx_size = (agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE) if agent_obj else DEFAULT_CONTEXT_WINDOW_SIZE

        # Determine conv_id for session isolation
        if conversation_type == "2":
            # Group chat
            conv_id = f"dingtalk_group_{conversation_id}"
        else:
            # P2P / single chat
            conv_id = f"dingtalk_p2p_{sender_staff_id}"

        # Load robot credentials for message delivery and directory fallback.
        _early_cfg_r = await db.execute(
            _select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "dingtalk",
            )
        )
        _early_cfg = _early_cfg_r.scalar_one_or_none()

        # -- Multi-dimension user matching (optimized: local-first, API-last) --
        from app.models.org import OrgMember
        from sqlalchemy import or_ as _or
        from app.models.user import Identity as _IdentityModel

        dt_username = f"dingtalk_{sender_staff_id}"
        platform_user = None
        dt_unionid = ""
        dt_mobile = ""
        dt_email = ""
        dt_raw_email = None
        dt_raw_org_email = None
        dt_real_name = ""

        # Company-level credentials are primary; the agent robot is the fallback.
        _dingtalk_provider = await _get_tenant_dingtalk_provider(
            db, agent_obj.tenant_id
        )
        _directory_credentials = _resolve_dingtalk_directory_credentials(
            _dingtalk_provider, _early_cfg
        )
        if _directory_credentials:
            logger.debug(
                "[DingTalk] Directory credential chain selected: sources={} provider_id={}",
                [item[2] for item in _directory_credentials],
                getattr(_dingtalk_provider, "id", None),
            )
        else:
            logger.warning(
                "[DingTalk] No enterprise or agent robot credentials are available for directory enrichment"
            )

        # Canonical attribution is resolved once through the shared scoped
        # channel binding. Corporate directory credentials are authoritative;
        # the robot credentials are only the documented fallback source.
        if _directory_credentials:
            dt_user_detail = await _get_dingtalk_user_detail_with_fallback(
                _directory_credentials,
                sender_staff_id,
                getattr(_dingtalk_provider, "id", None),
            )
            if dt_user_detail:
                dt_real_name = dt_user_detail.get("name", "")
                dt_unionid = dt_user_detail.get("unionid", "")
                dt_mobile = dt_user_detail.get("mobile") or ""
                dt_raw_email = dt_user_detail.get("email")
                dt_raw_org_email = dt_user_detail.get("org_email")
                dt_email = dt_raw_org_email or dt_raw_email or ""

        from app.services.channel_user_service import channel_user_service

        platform_user = await channel_user_service.resolve_channel_user(
            db,
            agent_obj,
            "dingtalk",
            sender_staff_id,
            {
                "external_id": sender_staff_id,
                "unionid": dt_unionid,
                "mobile": dt_mobile,
                "email": dt_email,
                "raw_email": dt_raw_email,
                "raw_org_email": dt_raw_org_email,
                "raw_mobile": dt_mobile,
                "name": dt_real_name,
                "nickname": sender_nick,
                "directory_name_verified": bool(dt_real_name),
                "identity_verified": bool(dt_mobile or dt_email),
            },
        )

        _sender_org_member = None

        # Step 1: Match via sender_staff_id in org_members.external_id (企业 userId，最稳定)
        if sender_staff_id and _dingtalk_provider and not platform_user:
            _om_r = await db.execute(
                _select(OrgMember).where(
                    OrgMember.provider_id == _dingtalk_provider.id,
                    OrgMember.external_id == sender_staff_id,
                    OrgMember.status == "active",
                ).order_by(OrgMember.synced_at.desc(), OrgMember.id.desc()).limit(1)
            )
            _sender_org_member = _om_r.scalar_one_or_none()
            if _sender_org_member:
                dt_unionid = _sender_org_member.unionid or ""
                dt_mobile = _sender_org_member.phone or ""
                dt_email = _sender_org_member.email or ""
            if _sender_org_member and _sender_org_member.user_id:
                _u_r = await db.execute(_select(UserModel).where(UserModel.id == _sender_org_member.user_id).options(_selectinload(UserModel.identity)))
                platform_user = _u_r.scalar_one_or_none()
                if platform_user:
                    logger.info(f"[DingTalk] Step1: Matched user via staff_id {sender_staff_id}: {platform_user.username}")
                    if platform_user.identity:
                        dt_mobile = dt_mobile or platform_user.identity.phone or ""
                        identity_email = platform_user.identity.email or ""
                        if identity_email and not identity_email.endswith(".local"):
                            dt_email = dt_email or identity_email

        # Step 2: Match via username = dingtalk_{staffId} (兼容旧用户)
        if sender_staff_id and not platform_user:
            _u_r = await db.execute(
                _select(UserModel).join(UserModel.identity).where(
                    _IdentityModel.username == dt_username,
                    UserModel.tenant_id == agent_obj.tenant_id,
                ).options(_selectinload(UserModel.identity))
            )
            platform_user = _u_r.scalar_one_or_none()
            if platform_user:
                logger.info(f"[DingTalk] Step2: Matched user via username {dt_username}")
                if platform_user.identity:
                    dt_mobile = dt_mobile or platform_user.identity.phone or ""
                    identity_email = platform_user.identity.email or ""
                    if identity_email and not identity_email.endswith(".local"):
                        dt_email = dt_email or identity_email

        # Step 3: Enrich missing identity data, including legacy dingtalk_* users.
        if not dt_mobile and _directory_credentials and sender_staff_id:
            dt_user_detail = await _get_dingtalk_user_detail_with_fallback(
                _directory_credentials,
                sender_staff_id,
                getattr(_dingtalk_provider, "id", None),
            )
            if dt_user_detail:
                dt_real_name = dt_real_name or dt_user_detail.get("name", "")
                dt_unionid = dt_unionid or dt_user_detail.get("unionid", "")
                dt_mobile = dt_mobile or dt_user_detail.get("mobile") or ""
                dt_raw_email = dt_user_detail.get("email")
                dt_raw_org_email = dt_user_detail.get("org_email")
                dt_email = dt_email or dt_raw_org_email or dt_raw_email or ""

        # 3a: unionId 查 org_members（跨通道匹配 SSO 用户）
        if dt_unionid and _dingtalk_provider and not platform_user:
            _om_r = await db.execute(
                _select(OrgMember).where(
                    OrgMember.provider_id == _dingtalk_provider.id,
                    OrgMember.status == "active",
                    _or(
                        OrgMember.unionid == dt_unionid,
                        OrgMember.external_id == dt_unionid,
                    ),
                ).order_by(OrgMember.synced_at.desc(), OrgMember.id.desc()).limit(1)
            )
            _om = _om_r.scalar_one_or_none()
            if _om and _om.user_id:
                _u_r = await db.execute(_select(UserModel).where(UserModel.id == _om.user_id).options(_selectinload(UserModel.identity)))
                platform_user = _u_r.scalar_one_or_none()
                if platform_user:
                    logger.info("[DingTalk] Step3a: Matched user via enterprise unionid")

        # 3b: mobile 匹配
        if dt_mobile and not platform_user:
            _u_r = await db.execute(
                _select(UserModel).join(UserModel.identity).where(
                    _IdentityModel.phone == dt_mobile,
                    UserModel.tenant_id == agent_obj.tenant_id,
                ).options(_selectinload(UserModel.identity))
            )
            mobile_user = _u_r.scalar_one_or_none()
            if mobile_user:
                platform_user = mobile_user
                logger.info(f"[DingTalk] Step3b: Matched user via mobile: {platform_user.username}")

        # 3c: email 匹配
        if dt_email and not platform_user:
            _u_r = await db.execute(
                _select(UserModel).join(UserModel.identity).where(
                    _IdentityModel.email == dt_email,
                    UserModel.tenant_id == agent_obj.tenant_id,
                ).options(_selectinload(UserModel.identity))
            )
            platform_user = _u_r.scalar_one_or_none()
            if platform_user:
                logger.info(f"[DingTalk] Step3c: Matched user via email: {platform_user.username}")


        # Step 4: No match found — create new user
        if not platform_user:
            # Defensive fallback only: inbound channel identities are not login
            # principals and must never mint synthetic credentials.
            platform_user = UserModel(
                identity_id=None,
                display_name=sender_nick or f"DingTalk {sender_staff_id[:8]}",
                role="member",
                tenant_id=agent_obj.tenant_id if agent_obj else None,
                source="dingtalk",
                is_active=True,
            )
            db.add(platform_user)
            await db.flush()
            logger.info(f"[DingTalk] Step4: Created new user: {dt_username}")
        else:
            # Update source and verified contact data for existing users. The
            # canonical display name is owned by the directory profile; an
            # inbound senderNick must never overwrite it.
            updated = False
            if not platform_user.source or platform_user.source == "web":
                platform_user.source = "dingtalk"
                updated = True
            # 补充 mobile/email（通讯录获取的信息写入已有用户的 Identity）
            if platform_user.identity:
                if dt_mobile and not platform_user.identity.phone:
                    _claimed_phone_r = await db.execute(
                        _select(_IdentityModel.id).where(
                            _IdentityModel.phone == dt_mobile,
                            _IdentityModel.id != platform_user.identity.id,
                        ).limit(1)
                    )
                    if _claimed_phone_r.scalar_one_or_none() is None:
                        platform_user.identity.phone = dt_mobile
                        updated = True
                    else:
                        logger.warning(
                            "[DingTalk] Skipped identity phone backfill because the number "
                            "is already claimed by another identity"
                        )
                if dt_email and (not platform_user.identity.email or platform_user.identity.email.endswith((".local",))):
                    from sqlalchemy import func as _func

                    _claimed_email_r = await db.execute(
                        _select(_IdentityModel.id).where(
                            _func.lower(_IdentityModel.email) == dt_email.lower(),
                            _IdentityModel.id != platform_user.identity.id,
                        ).limit(1)
                    )
                    if _claimed_email_r.scalar_one_or_none() is None:
                        platform_user.identity.email = dt_email
                        updated = True
                    else:
                        logger.warning(
                            "[DingTalk] Skipped identity email backfill because the address "
                            "is already claimed by another identity"
                        )
            if updated:
                await db.flush()

        # -- Ensure org_member record exists (for future Step 1 fast-path) --
        if _dingtalk_provider and sender_staff_id:
            if _sender_org_member:
                _existing_om = _sender_org_member
                if _existing_om.user_id != platform_user.id:
                    _existing_om.user_id = platform_user.id
            else:
                _om_check_r = await db.execute(
                    _select(OrgMember).where(
                        OrgMember.user_id == platform_user.id,
                        OrgMember.provider_id == _dingtalk_provider.id,
                    ).order_by(OrgMember.synced_at.desc(), OrgMember.id.desc()).limit(1)
                )
                _existing_om = _om_check_r.scalar_one_or_none()
            if not _existing_om:
                # Create org_member so next message hits Step 1 directly
                _new_om = OrgMember(
                    user_id=platform_user.id,
                    provider_id=_dingtalk_provider.id,
                    external_id=sender_staff_id,
                    unionid=dt_unionid or None,
                    phone=dt_mobile or None,
                    email=dt_email or None,
                    name=dt_real_name or platform_user.display_name or sender_nick or dt_username,
                    nickname=sender_nick or None,
                    status="active",
                    tenant_id=agent_obj.tenant_id,
                )
                db.add(_new_om)
                await db.flush()
                logger.info(f"[DingTalk] Created org_member for user {platform_user.username}, external_id={sender_staff_id}")
            else:
                updated_member = False
                if _existing_om.external_id != sender_staff_id:
                    _existing_om.external_id = sender_staff_id
                    updated_member = True
                if dt_unionid and not _existing_om.unionid:
                    _existing_om.unionid = dt_unionid
                    updated_member = True
                if dt_mobile and not _existing_om.phone:
                    _existing_om.phone = dt_mobile
                    updated_member = True
                if dt_email and not _existing_om.email:
                    _existing_om.email = dt_email
                    updated_member = True
                if dt_real_name and _existing_om.name != dt_real_name:
                    _existing_om.name = dt_real_name
                    updated_member = True
                if sender_nick and _existing_om.nickname != sender_nick:
                    _existing_om.nickname = sender_nick
                    updated_member = True
                if updated_member:
                    await db.flush()
                    logger.info("[DingTalk] Backfilled enterprise org member identity fields")

        platform_user_id = platform_user.id

        # Learn the opaque DingTalk senderId only from a callback that also
        # carried a real corporate staff id.  The Stream adapter falls back to
        # senderId when senderStaffId is absent, so equal values are not trusted
        # identity evidence.
        trusted_sender_alias = has_trusted_dingtalk_sender_alias(
            sender_staff_id,
            sender_id,
        )
        if _dingtalk_provider is None and (trusted_sender_alias or quoted_message):
            _dingtalk_provider = await _get_tenant_dingtalk_provider(
                db,
                agent_obj.tenant_id,
            )

        if trusted_sender_alias and _dingtalk_provider is not None:
            await channel_user_service.remember_provider_user_alias(
                db,
                provider=_dingtalk_provider,
                channel_type="dingtalk",
                id_type="sender_id",
                subject=sender_id,
                user=platform_user,
            )

        quoted_message = await resolve_quoted_message_sender(
            db,
            quoted_message,
            provider=_dingtalk_provider,
            channel_type="dingtalk",
            provider_sender_id_type="sender_id",
            agent=agent_obj,
            agent_provider_ref=chatbot_user_id,
        )

        # Check for channel commands (/new, /reset)
        from app.services.channel_commands import (
            is_channel_command,
            prepare_channel_command_reply,
        )
        if is_channel_command(user_text):
            cmd_result = await prepare_channel_command_reply(
                db=db, command=user_text, agent_id=agent_id,
                user_id=platform_user_id, external_conv_id=conv_id,
                external_user_id=sender_staff_id,
                source_channel="dingtalk",
                provider_event_id=message_id,
                is_group=conversation_type == "2",
                group_name=conversation_title or None,
            )
            if not cmd_result["should_deliver"]:
                await db.commit()
                return
            await db.commit()
            _cmd_delivery = await _deliver_dingtalk_command_reply(
                message_id=cmd_result["message_id"],
                agent_id=agent_id,
                conversation_id=cmd_result["conversation_id"],
                external_conv_id=conv_id,
                is_group=conversation_type == "2",
                message=cmd_result["message"],
            )
            if not _cmd_delivery.ok:
                logger.error(
                    f"[DingTalk] Command reply failed: {_cmd_delivery.error}"
                )
            return

        # Use the real DingTalk group title when the stream event provides one;
        # fall back to a conversation_id-based placeholder otherwise.
        _dt_group_name = None
        if conversation_type == "2":
            _dt_group_name = (
                conversation_title.strip()
                if conversation_title and conversation_title.strip()
                else f"DingTalk Group {conversation_id[:12]}"
            )

        # Find or create session
        sess = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            user_id=platform_user_id,
            external_conv_id=conv_id,
            source_channel="dingtalk",
            first_message_title=user_text,
            is_group=(conversation_type == "2"),
            group_name=_dt_group_name,
        )
        session_conv_id = str(sess.id)

        # Load provider-neutral history; the shared caller materializes images
        # only after the concrete model attempt is resolved.
        from app.services.chat_history import load_history_for_llm
        history = await load_history_for_llm(
            db,
            agent_id=agent_id,
            conversation_id=session_conv_id,
            ctx_size=ctx_size,
            is_group=(conversation_type == "2"),
        )

        # Save user message — use display-friendly format for DB (no base64)
        # Build saved_content: [file:name] prefix for each saved file + clean text
        import re as _re_dt
        _clean_text = _re_dt.sub(
            r'\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]',
            "", user_text,
        ).strip()
        if saved_file_paths:
            from pathlib import Path as _PathDT
            _file_prefixes = "\n".join(
                f"[file:{_PathDT(p).name}]" for p in saved_file_paths
            )
            saved_content = f"{_file_prefixes}\n{_clean_text}".strip() if _clean_text else _file_prefixes
        else:
            saved_content = _clean_text or user_text
        from app.services.chat_history import (
            finish_blocked_confirmation_ingest,
            finish_ignored_confirmation_ingest,
            ingest_incoming_chat_message,
            load_history_prefix_before_anchor,
        )
        from app.services.quoted_message import normalize_quoted_message

        normalized_quote = normalize_quoted_message(quoted_message)
        own_attachments = [
            attachment_from_workspace_path(path)
            for path in (saved_file_paths or [])
        ]
        quoted_attachments = (
            list(normalized_quote.get("attachments") or [])
            if normalized_quote is not None
            else []
        )
        inbound_meta = {
            "sender_display_name": platform_user.display_name,
            "sender_nickname": sender_nick or None,
            "attachments": [*own_attachments, *quoted_attachments],
        }
        if normalized_quote is not None:
            inbound_meta["quoted_message"] = normalized_quote

        ingested = await ingest_incoming_chat_message(
            db,
            session=sess,
            agent_id=agent_id,
            user_id=platform_user_id,
            content=saved_content,
            source_channel="dingtalk",
            provider_event_id=message_id or None,
            channel_config_id=_early_cfg.id if _early_cfg else None,
            actor_ref=sender_staff_id,
            message_meta=inbound_meta,
        )

        async def _delete_unconsumed_uploads() -> None:
            stored_paths = {
                item["path"]
                for item in [*own_attachments, *quoted_attachments]
                if item.get("path")
            }
            if stored_paths:
                from app.services.storage import (
                    agent_storage_key,
                    get_storage_backend,
                )

                storage = get_storage_backend()
                for workspace_path in stored_paths:
                    await storage.delete(
                        agent_storage_key(agent_id, workspace_path)
                    )

        if await finish_blocked_confirmation_ingest(db, ingested):
            await _delete_unconsumed_uploads()
            return
        if not ingested.created:
            await _delete_unconsumed_uploads()
        turn_anchor_id = ingested.message.id
        sess.last_message_at = datetime.now(timezone.utc)
        await db.commit()
        if ingested.ignored_confirmation is not None:
            await finish_ignored_confirmation_ingest(ingested)
            refreshed_prefix = await load_history_prefix_before_anchor(
                db,
                agent_id=agent_id,
                conversation_id=session_conv_id,
                turn_anchor_id=ingested.message.id,
                ctx_size=ctx_size,
                is_group=(conversation_type == "2"),
            )
            if refreshed_prefix is None:
                raise RuntimeError(
                    "confirmation ignore committed but DingTalk history prefix could not be rebuilt"
                )
            history = refreshed_prefix

        # Mirror this inbound message to anyone viewing the session on web in real
        # time — the agent reply already streams there; this makes the user's own
        # message show up live too, not only on reload.
        from app.services.channel_llm import broadcast_channel_user_message
        await broadcast_channel_user_message(
            agent_id, session_conv_id, message=ingested.message,
            sender_name=platform_user.display_name or sender_nick or None,
            user_id=platform_user_id,
        )

        if ingested.consumed_by_onmessage:
            logger.info(
                "[DingTalk] Inbound message %s routed to %d on_message execution(s)",
                message_id,
                len(ingested.execution_ids),
            )
            return

        from app.services.quoted_message import render_quoted_message_for_llm
        from app.services.sender_attribution import wrap_with_sender

        # Group chats get a platform-injected <sender> prefix (spec §4.0/§4.1).
        # DingTalk P2P had no prefix before this iteration and we keep it that
        # way — agent_context's "## Current Conversation" handles the single-
        # speaker session-level identity.
        llm_user_text = render_quoted_message_for_llm(user_text, normalized_quote)
        if conversation_type == "2":
            llm_user_text = wrap_with_sender(
                llm_user_text,
                platform_user_id,
                platform_user.display_name or sender_nick,
            )

        # Call LLM
        _thinking_chunks: list[str] = []

        _thinking_sender = BufferedIMThinkingSender.for_runtime(
            enabled=resolve_im_thinking_enabled(agent_obj, sess),
            agent_id=agent_id,
            user_id=platform_user_id,
            conversation_id=session_conv_id,
            turn_anchor_id=turn_anchor_id,
        )

        async def _collect_thinking(text: str):
            _thinking_chunks.append(text)
            if channel_reactions and channel_reactions.on_thinking:
                try:
                    await channel_reactions.on_thinking(text)
                except Exception as exc:  # noqa: BLE001 - reaction feedback is best-effort
                    logger.warning(f"[DingTalk] Thinking reaction update failed: {exc}")
            await _thinking_sender.push(text)

        async def _notify_tool_call(evt: dict):
            if channel_reactions and channel_reactions.on_tool_call:
                try:
                    await channel_reactions.on_tool_call(evt)
                except Exception as exc:  # noqa: BLE001 - reaction feedback is best-effort
                    logger.warning(f"[DingTalk] Tool reaction update failed: {exc}")

        try:
            reply_text = await _call_agent_llm(
                db, agent_id, llm_user_text,
                history=history, user_id=platform_user_id,
                session_id=session_conv_id,
                is_group=(conversation_type == "2"),
                on_thinking=_collect_thinking,
                on_tool_call=_notify_tool_call,
                turn_anchor_id=turn_anchor_id,
            )
        finally:
            await _thinking_sender.flush()

        has_media = bool(own_attachments or quoted_attachments)
        logger.info(
            f"[DingTalk] LLM reply ({('media' if has_media else 'text')} input): "
            f"{reply_text[:100]}"
        )

        if reply_text:
            # Persist the final assistant reply before the external side effect.
            # The assistant row is the durable completion marker for this
            # append-only message turn.
            from app.database import async_session as _reply_session_factory
            from app.services.chat_history import persist_assistant_reply_row
            from app.services.im_delivery import (
                IMDeliveryResult,
                attach_delivery_to_meta,
                register_delivery,
            )

            async with _reply_session_factory() as reply_db:
                assistant_message_id = await persist_assistant_reply_row(
                    reply_db,
                    agent_id=agent_id,
                    user_id=platform_user_id,
                    conversation_id=session_conv_id,
                    content=reply_text,
                    thinking="".join(_thinking_chunks) or None,
                    message_meta=attach_delivery_to_meta(
                        {},
                        IMDeliveryResult.pending("dingtalk"),
                    ),
                    turn_anchor_id=turn_anchor_id,
                )
                await reply_db.commit()
            from app.services.conversation_turn_lifecycle import publish_committed_turn_terminal

            await publish_committed_turn_terminal(
                agent_id=agent_id,
                conversation_id=session_conv_id,
                turn_anchor_id=turn_anchor_id,
                message_id=assistant_message_id,
                content=reply_text,
            )
            sess.last_message_at = datetime.now(timezone.utc)
            await db.commit()

            # All final replies use the durable OpenAPI route so delivery is
            # independent of the inbound callback's temporary webhook TTL.
            from app.services.im_delivery import deliver_persisted_message
            from app.services.turn_runtime import TurnRuntime

            try:
                delivery_result = await deliver_persisted_message(
                    message_id=assistant_message_id,
                    agent_id=agent_id,
                    runtime=TurnRuntime(
                        session_found=True,
                        source_channel="dingtalk",
                        conversation_id=session_conv_id,
                        external_conv_id=sess.external_conv_id,
                        is_group=bool(sess.is_group),
                    ),
                    message=reply_text,
                )
            except Exception as e:
                logger.error(f"[DingTalk] OpenAPI reply failed: {type(e).__name__}")
                await register_delivery(
                    assistant_message_id,
                    IMDeliveryResult.from_exception("dingtalk", e),
                )

        # Log activity
        from app.services.activity_logger import log_activity
        await log_activity(
            agent_id, "chat_reply",
            f"Replied to DingTalk message: {reply_text[:80]}",
            detail={"channel": "dingtalk", "user_text": user_text[:200], "reply": reply_text[:500]},
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
