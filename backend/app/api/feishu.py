"""Feishu OAuth and Channel API routes."""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from loguru import logger
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import get_db
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.models.identity import IdentityProvider
from app.schemas.schemas import ChannelConfigCreate, ChannelConfigOut, TokenResponse, UserOut
# Shared, channel-agnostic LLM entry point now lives in services/channel_llm.
# Re-exported here for backwards compatibility (older code does
# `from app.api.feishu import _call_agent_llm`); new code imports it directly.
from app.services.channel_llm import _call_agent_llm  # noqa: F401
from app.services.channel_dispatch import ChannelReactions, run_channel_message
from app.services.channel_commands import is_channel_command, handle_channel_command
from app.services.feishu_service import feishu_service
from app.services.im_thinking_output import resolve_im_thinking_enabled
from app.services.storage import agent_upload_key, get_storage_backend, store_agent_upload

router = APIRouter(tags=["feishu"])

# Number of tool status lines to keep visible in the Feishu card.
# Shows the last N non-running lines plus any active "running" entry.
_TOOL_STATUS_KEEP_LINES = 20

_USER_RESOLUTION_ERROR_TIP = (
    "抱歉，我暂时无法稳定识别你的飞书账号，已停止本次处理以避免重复创建账号。"
    "请稍后重试，或联系管理员检查飞书 Contact API 权限。"
)


def _storage_mtime(entry) -> float:
    raw = str(getattr(entry, "modified_at", "") or "")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0


def _build_card(
    answer_text: str,
    thinking_text: str = "",
    streaming: bool = False,
    tool_status_lines: list[str] | None = None,
    agent_name: str = "AI 回复",
) -> dict:
    """Build a Feishu interactive card for streaming replies."""
    elements = []

    if tool_status_lines:
        elements.append({
            "tag": "markdown",
            "content": "\n".join(tool_status_lines[-_TOOL_STATUS_KEEP_LINES:]),
        })
        elements.append({"tag": "hr"})

    if thinking_text:
        think_preview = thinking_text[:200].replace("\n", " ")
        elements.append({
            "tag": "markdown",
            "content": f"<font color='grey'>💭 **Thinking**\n{think_preview}{'...' if len(thinking_text) > 200 else ''}</font>",
        })
        elements.append({"tag": "hr"})

    body = answer_text + ("▌" if streaming and answer_text else ("..." if streaming else ""))
    elements.append({"tag": "markdown", "content": body or "..."})
    return {
        "config": {"update_multi": True},
        "header": {
            "template": "blue",
            "title": {"content": agent_name, "tag": "plain_text"},
        },
        "elements": elements,
    }


def _looks_like_error_text(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    markers = (
        "⚠️",
        "[error]",
        "error:",
        "error ",
        "failed",
        "failure",
        "timeout",
        "timed out",
        "调用模型出错",
        "未配置 llm 模型",
        "数字员工未找到",
    )
    return any(marker in normalized for marker in markers)


def _normalize_tool_error(tool_name: str, result: object) -> str | None:
    text = "" if result is None else str(result).strip()
    if not text or not _looks_like_error_text(text):
        return None
    compact = " ".join(text.split())
    if len(compact) > 240:
        compact = compact[:240].rstrip() + "..."
    return f"`{tool_name}`: {compact}"


def _append_error_details(reply_text: str, tool_errors: list[str]) -> str:
    base = (reply_text or "").strip()
    unique_errors: list[str] = []
    seen: set[str] = set()
    for item in tool_errors:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_errors.append(normalized)
    if not unique_errors:
        return base
    details = "\n".join(f"- {item}" for item in unique_errors)
    if not base:
        return f"执行失败，错误信息如下：\n{details}"
    if _looks_like_error_text(base):
        return f"{base}\n\n执行过程中出现以下错误：\n{details}"
    return base


class _SerialPatchQueue:
    """Serialize patch requests for one Feishu message to prevent out-of-order overwrite."""

    def __init__(self):
        self._tail: asyncio.Task | None = None

    def enqueue(self, job_factory: Callable[[], Awaitable[None]]) -> None:
        prev = self._tail

        async def _runner():
            if prev:
                try:
                    await prev
                except Exception as e:
                    logger.warning(f"[Feishu] Previous patch job failed before next job: {e}")
            await job_factory()

        self._tail = asyncio.create_task(_runner())

    async def drain(self) -> None:
        if self._tail:
            await self._tail


# ─── OAuth ──────────────────────────────────────────────

from fastapi.responses import HTMLResponse, Response

@router.get("/auth/feishu/callback")
@router.post("/auth/feishu/callback", response_model=TokenResponse)
async def feishu_oauth_callback(
    code: str, 
    state: str = None, 
    db: AsyncSession = Depends(get_db)
):
    """Handle Feishu OAuth callback — exchange code for user session."""
    # Parse state if it's a UUID (session ID) or other context
    from app.models.identity import SSOScanSession
    tenant_id = None
    if state:
        try:
            sid = uuid.UUID(state)
            s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
            session = s_res.scalar_one_or_none()
            if session:
                tenant_id = session.tenant_id
        except (ValueError, AttributeError):
            pass

    try:
        # Use FeishuAuthProvider instead of legacy feishu_service
        from app.services.auth_provider import FeishuAuthProvider
        from app.models.identity import IdentityProvider
        from app.config import get_settings

        # Get Feishu credentials from settings
        settings = get_settings()
        feishu_config = {
            "app_id": settings.FEISHU_APP_ID,
            "app_secret": settings.FEISHU_APP_SECRET,
        }

        # Get or create provider via auth provider
        provider = None
        if tenant_id:
            result = await db.execute(
                select(IdentityProvider).where(
                    IdentityProvider.provider_type == "feishu",
                    IdentityProvider.tenant_id == tenant_id
                )
            )
            provider = result.scalar_one_or_none()

        auth_provider = FeishuAuthProvider(provider=provider, config=feishu_config)

        # Ensure provider exists (will create if not)
        await auth_provider._ensure_provider(db, tenant_id)
        provider = auth_provider.provider

        # Exchange code for user info
        token_data = await auth_provider.exchange_code_for_token(code)
        access_token = token_data.get("access_token", "")
        user_info = await auth_provider.get_user_info(access_token)

        # Find or create user
        user, is_new = await auth_provider.find_or_create_user(db, user_info, tenant_id=tenant_id)

        # Generate JWT token
        from app.core.security import create_access_token
        token = create_access_token(str(user.id), user.role)

    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Feishu auth failed: {e}")

    # If this is an SSO session, store result and redirect to frontend completion
    if state:
        try:
            sid = uuid.UUID(state)
            s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
            session = s_res.scalar_one_or_none()
            if session:
                session.status = "authorized"
                session.provider_type = "feishu"
                session.user_id = user.id
                session.access_token = token
                session.error_msg = None
                await db.commit()
                return HTMLResponse(
                    f"""<html><head><meta charset="utf-8" /></head>
                    <body style="font-family: sans-serif; padding: 24px;">
                        <div>SSO login successful. Redirecting...</div>
                        <script>window.location.href = "/sso/entry?sid={sid}&complete=1";</script>
                    </body></html>"""
                )
        except Exception as e:
            logger.exception("Failed to update SSO session (feishu) %s", e)

    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


# ─── Channel Config (per-agent Feishu bot) ──────────────

@router.post("/agents/{agent_id}/channel", response_model=ChannelConfigOut, status_code=status.HTTP_201_CREATED)
async def configure_channel(
    agent_id: uuid.UUID,
    data: ChannelConfigCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure Feishu bot credentials for a digital employee (wizard step 5)."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    # Check existing
    result = await db.execute(select(ChannelConfig).where(
        ChannelConfig.agent_id == agent_id,
        ChannelConfig.channel_type == "feishu",
    ))
    existing = result.scalar_one_or_none()
    if existing:
        existing.app_id = data.app_id
        existing.app_secret = data.app_secret
        existing.encrypt_key = data.encrypt_key
        existing.verification_token = data.verification_token
        existing.extra_config = data.extra_config or {}
        existing.is_configured = True
        await db.flush()
        
        # Start/Stop WS client in background
        from app.services.feishu_ws import feishu_ws_manager
        import asyncio
        mode = existing.extra_config.get("connection_mode", "webhook")
        if mode == "websocket":
            asyncio.create_task(feishu_ws_manager.start_client(agent_id, existing.app_id, existing.app_secret))
        else:
            asyncio.create_task(feishu_ws_manager.stop_client(agent_id))
        
        return ChannelConfigOut.model_validate(existing)

    config = ChannelConfig(
        agent_id=agent_id,
        channel_type=data.channel_type,
        app_id=data.app_id,
        app_secret=data.app_secret,
        encrypt_key=data.encrypt_key,
        verification_token=data.verification_token,
        extra_config=data.extra_config or {},
        is_configured=True,
    )
    db.add(config)
    await db.flush()

    # Start WS client in background
    from app.services.feishu_ws import feishu_ws_manager
    import asyncio
    mode = config.extra_config.get("connection_mode", "webhook")
    if mode == "websocket":
        asyncio.create_task(feishu_ws_manager.start_client(agent_id, config.app_id, config.app_secret))

    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/channel", response_model=ChannelConfigOut)
async def get_channel_config(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get Feishu channel configuration for an agent."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(select(ChannelConfig).where(
        ChannelConfig.agent_id == agent_id,
        ChannelConfig.channel_type == "feishu",
    ))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Channel not configured")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/channel/webhook-url")
async def get_webhook_url(agent_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Get the webhook URL for this agent's Feishu bot."""
    from app.services.platform_service import platform_service
    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/feishu/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/channel", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel_config(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove Feishu bot configuration for an agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    result = await db.execute(select(ChannelConfig).where(
        ChannelConfig.agent_id == agent_id,
        ChannelConfig.channel_type == "feishu",
    ))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Channel not configured")
    await db.delete(config)



# ─── Feishu Event Webhook ───────────────────────────────

@router.post("/channel/feishu/{agent_id}/webhook")
async def feishu_event_webhook(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Feishu event callback for a specific agent's bot."""
    body = await request.json()
    
    # Handle verification challenge
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    return await process_feishu_event(agent_id, body, db)


async def process_feishu_event(agent_id: uuid.UUID, body: dict, db: AsyncSession):
    """Core logic to process feishu events from both webhook and WS client."""
    import json as _json
    logger.info(f"[Feishu] Event processing for {agent_id}: event_type={body.get('header', {}).get('event_type', 'N/A')}")

    event_id = body.get("header", {}).get("event_id", "")

    # Get channel config — filter by feishu since an agent can have multiple channels
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "feishu",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return {"code": 1, "msg": "Channel not found"}

    # Handle events
    event = body.get("event", {})
    event_type = body.get("header", {}).get("event_type", "")

    if event_type == "im.message.receive_v1":
        message = event.get("message", {})
        sender = event.get("sender", {}).get("sender_id", {})
        sender_open_id = sender.get("open_id", "")
        sender_user_id_from_event = sender.get("user_id", "")  # tenant-stable ID, available directly in event body
        msg_type = message.get("message_type", "text")
        chat_type = message.get("chat_type", "p2p")  # p2p or group
        chat_id = message.get("chat_id", "")

        logger.info(f"[Feishu] Received {msg_type} message, chat_type={chat_type}, open_id={sender_open_id!r}, user_id_from_event={sender_user_id_from_event!r}")

        # ── Normalize post (rich text) → extract text + schedule image downloads ──
        if msg_type == "post":
            import json as _json_post
            _post_body = _json_post.loads(message.get("content", "{}"))
            # Feishu post content: {"title": "...", "content": [[{"tag":"text","text":"..."},...],...]}
            # The content may be nested under a locale key like "zh_cn"
            _paragraphs = _post_body.get("content", [])
            if not _paragraphs:
                # Try locale keys (zh_cn, en_us, etc.)
                for _locale_key, _locale_val in _post_body.items():
                    if isinstance(_locale_val, dict) and "content" in _locale_val:
                        _paragraphs = _locale_val["content"]
                        break
            _text_parts = []
            _post_image_keys = []
            for _para in _paragraphs:
                _line_parts = []
                for _elem in _para:
                    _tag = _elem.get("tag")
                    if _tag == "text":
                        _line_parts.append(_elem.get("text", ""))
                    elif _tag == "a":
                        _href = _elem.get("href", "")
                        _link_text = _elem.get("text", "")
                        _line_parts.append(f"{_link_text} ({_href})" if _href else _link_text)
                    elif _tag == "img":
                        _ik = _elem.get("image_key", "")
                        if _ik:
                            _post_image_keys.append(_ik)
                if _line_parts:
                    _text_parts.append("".join(_line_parts))
            _extracted_text = "\n".join(_text_parts).strip()
            # Download images and embed as base64 for vision-capable models
            _image_markers = []
            if _post_image_keys:
                import base64 as _b64
                _msg_id = message.get("message_id", "")
                for _ik in _post_image_keys:
                    try:
                        _img_bytes = await feishu_service.download_message_resource(
                            config.app_id, config.app_secret, _msg_id, _ik, "image"
                        )
                        _, _workspace_path, _save_path = await store_agent_upload(
                            agent_id,
                            f"image_{_ik[-8:]}.jpg",
                            _img_bytes,
                            content_type="image/jpeg",
                        )
                        logger.info(f"[Feishu] Saved post image to {_workspace_path} ({len(_img_bytes)} bytes)")
                        # Embed as base64 marker for vision models
                        _b64_data = _b64.b64encode(_img_bytes).decode("ascii")
                        _image_markers.append(f"[image_data:data:image/jpeg;base64,{_b64_data}]")
                    except Exception as _dl_err:
                        logger.error(f"[Feishu] Failed to download post image {_ik}: {_dl_err}")
            # Build final text with embedded images
            if not _extracted_text and _image_markers:
                _extracted_text = "[用户发送了图片，请看图片内容]"
            _final_content = _extracted_text
            if _image_markers:
                _final_content += "\n" + "\n".join(_image_markers)
            # Rewrite as text message so existing handler processes it
            message["content"] = _json_post.dumps({"text": _final_content})
            msg_type = "text"
            logger.info(f"[Feishu] Normalized post → text='{_extracted_text[:100]}', images={len(_image_markers)}")

        if msg_type in ("file", "image"):
            # Do not acknowledge the provider before the durable ingest/match
            # transaction has completed. Provider retries are deduplicated by
            # ChatMessage.external_event_key, not by process-local memory.
            await _handle_feishu_file(
                db,
                agent_id,
                config,
                message,
                sender_open_id,
                sender_user_id_from_event,
                chat_type,
                chat_id,
            )
            return {"code": 0, "msg": "ok"}

        if msg_type == "text":
            import json
            import re
            content = json.loads(message.get("content", "{}"))
            user_text = content.get("text", "")

            # Strip @mention tags (e.g. @_user_1) from group messages
            user_text = re.sub(r'@_user_\d+', '', user_text).strip()

            if not user_text:
                return {"code": 0, "msg": "empty message after stripping mentions"}

            # Detect task creation intent
            task_match = re.search(
                r'(?:创建|新建|添加|建一个|帮我建)(?:一个)?(?:任务|待办|todo)[，,：:\s]*(.+)',
                user_text, re.IGNORECASE
            )

            # Determine conversation_id for history isolation
            # Group chats: use chat_id; P2P chats: prefer user_id (tenant-stable)
            if chat_type == "group" and chat_id:
                conv_id = f"feishu_group_{chat_id}"
            else:
                conv_id = f"feishu_p2p_{sender_user_id_from_event or sender_open_id}"

            # Early-return for channel commands (/new, /reset):
            # Must run BEFORE find_or_create_channel_session to avoid creating a
            # ghost session that is immediately archived.
            if is_channel_command(user_text):
                from app.database import async_session as _async_session
                async with _async_session() as _cmd_db:
                    cmd_result = await handle_channel_command(
                        db=_cmd_db, command=user_text, agent_id=agent_id,
                        user_id=None, external_conv_id=conv_id,
                        source_channel="feishu",
                    )
                    await _cmd_db.commit()
                _cmd_reply_to = chat_id if chat_type == "group" and chat_id else sender_open_id
                _cmd_rid_type = "chat_id" if chat_type == "group" and chat_id else "open_id"
                try:
                    await feishu_service.send_message(
                        config.app_id, config.app_secret,
                        _cmd_reply_to, "text",
                        json.dumps({"text": cmd_result["message"]}),
                        receive_id_type=_cmd_rid_type,
                    )
                except Exception as _cmd_e:
                    logger.error(f"[Feishu] Failed to send command reply: {_cmd_e}")
                return {"code": 0, "msg": "ok"}

            # Load recent conversation history via session (session UUID may already exist)
            from app.models.agent import Agent as AgentModel
            from app.services.channel_session import find_or_create_channel_session
            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent_obj = agent_r.scalar_one_or_none()
            creator_id = agent_obj.creator_id if agent_obj else agent_id
            from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE
            ctx_size = (agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE) if agent_obj else DEFAULT_CONTEXT_WINDOW_SIZE

            # --- Resolve Feishu sender identity & find/create platform user ---
            import uuid as _uuid
            import httpx as _httpx

            sender_name = ""
            sender_user_id_feishu = sender_user_id_from_event  # tenant-level user_id, pre-filled from event body
            extra_info: dict | None = {
                "open_id": sender_open_id,
                "external_id": sender_user_id_feishu or None,
            }

            try:
                async with _httpx.AsyncClient() as _client:
                    _tok_resp = await _client.post(
                        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
                        json={"app_id": config.app_id, "app_secret": config.app_secret},
                    )
                    _app_token = _tok_resp.json().get("app_access_token", "")
                    if _app_token:
                        _user_resp = await _client.get(
                            f"https://open.feishu.cn/open-apis/contact/v3/users/{sender_open_id}",
                            params={"user_id_type": "open_id"},
                            headers={"Authorization": f"Bearer {_app_token}"},
                        )
                        _user_data = _user_resp.json()
                        logger.info(f"[Feishu] Sender resolve: code={_user_data.get('code')}, msg={_user_data.get('msg', '')}")
                        if _user_data.get("code") == 0:
                            _user_info = _user_data.get("data", {}).get("user", {})
                            sender_name = _user_info.get("name", "")
                            sender_user_id_feishu = _user_info.get("user_id", "")
                            sender_email = _user_info.get("email", "") or _user_info.get("enterprise_email", "")
                            # Feishu contact API returns 'avatar' as a dict
                            # (keys: avatar_240, avatar_640, avatar_origin), NOT a plain URL.
                            # We must extract a string to avoid a DataError when writing to the DB.
                            _raw_avatar = _user_info.get("avatar")
                            if isinstance(_raw_avatar, dict):
                                _avatar_url = (
                                    _raw_avatar.get("avatar_240")
                                    or _raw_avatar.get("avatar_640")
                                    or _raw_avatar.get("avatar_origin")
                                    or ""
                                )
                            else:
                                _avatar_url = _raw_avatar or ""
                            extra_info = {
                                "name": sender_name,
                                "email": sender_email,
                                "mobile": _user_info.get("mobile"),
                                "avatar_url": _avatar_url,
                                "external_id": _user_info.get("user_id"),
                                "unionid": _user_info.get("union_id"),
                                "open_id": sender_open_id,
                            }
                            logger.info(f"[Feishu] Resolved sender: {sender_name} (user_id={sender_user_id_feishu})")
                            # Cache sender info so feishu_user_search can find them by name
                            if sender_name and sender_open_id:
                                try:
                                    import pathlib as _pl, json as _cj, time as _ct
                                    _safe_id = str(agent_id).replace("..", "").replace("/", "")
                                    _cache = _pl.Path(f"/data/workspaces/{_safe_id}/feishu_contacts_cache.json")
                                    _cache.parent.mkdir(parents=True, exist_ok=True)
                                    _existing = {}
                                    if _cache.exists():
                                        try:
                                            _existing = _cj.loads(_cache.read_text())
                                        except Exception:
                                            pass
                                    # Key by user_id when available (tenant-stable), fallback to open_id
                                    _users = {}
                                    for _u in _existing.get("users", []):
                                        _key = _u.get("user_id") or _u.get("open_id", "")
                                        _users[_key] = _u
                                    _cache_key = sender_user_id_feishu or sender_open_id
                                    _users[_cache_key] = {
                                        "open_id": sender_open_id,
                                        "name": sender_name,
                                        "email": sender_email,
                                        "user_id": sender_user_id_feishu,
                                    }
                                    _cache.write_text(_cj.dumps(
                                        {"ts": _ct.time(), "users": list(_users.values())},
                                        ensure_ascii=False,
                                    ), encoding="utf-8")
                                    import os as _os
                                    _os.chmod(str(_cache), 0o600)
                                except Exception as _ce:
                                    logger.error(f"[Feishu] Cache write failed: {_ce}")
            except Exception as e:
                logger.error(f"[Feishu] Failed to resolve sender: {e}")

            # Resolve channel user via unified service (uses OrgMember + SSO patterns)
            from app.services.channel_user_service import channel_user_service
            try:
                platform_user = await channel_user_service.resolve_channel_user(
                    db=db,
                    agent=agent_obj,
                    channel_type="feishu",
                    # For Feishu, external_user_id is strictly user_id (tenant-stable).
                    external_user_id=sender_user_id_feishu or None,
                    extra_info=extra_info,
                )
            except Exception as e:
                from app.services.channel_user_service import ChannelUserResolutionError

                if isinstance(e, ChannelUserResolutionError):
                    logger.warning(f"[Feishu] Sender resolution refused: {e}")
                    _reply_to = chat_id if chat_type == "group" else sender_open_id
                    _rid_type = "chat_id" if chat_type == "group" else "open_id"
                    await feishu_service.send_message(
                        config.app_id,
                        config.app_secret,
                        _reply_to,
                        "text",
                        json.dumps({"text": _USER_RESOLUTION_ERROR_TIP}),
                        receive_id_type=_rid_type,
                    )
                    return {"code": 0, "msg": "user_resolution_skipped"}
                raise
            platform_user_id = platform_user.id

            # ── Find-or-create a ChatSession via external_conv_id (DB-based, no cache needed) ──
            from datetime import datetime as _dt, timezone as _tz
            _is_group = (chat_type == "group")

            # For group chats, fetch the real chat title from Feishu so the
            # session shows e.g. "产品讨论组" instead of "Feishu Group ou_xxx".
            # API failure falls back to the conversation_id-based placeholder
            # so a chat-info hiccup never blocks message processing.
            _fs_group_name = None
            if _is_group:
                try:
                    _chat_info = await feishu_service.get_chat_info(
                        config.app_id, config.app_secret, chat_id,
                    )
                    _real_name = (_chat_info or {}).get("name") if _chat_info else None
                    _fs_group_name = (
                        _real_name.strip() if _real_name and _real_name.strip()
                        else f"Feishu Group {chat_id[:12]}"
                    )
                except Exception as _gci_err:
                    logger.warning(f"[Feishu] chat-info lookup failed: {_gci_err}")
                    _fs_group_name = f"Feishu Group {chat_id[:12]}"

            _sess = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=platform_user_id if not _is_group else creator_id,
                external_conv_id=conv_id,
                source_channel="feishu",
                first_message_title=user_text,
                is_group=_is_group,
                group_name=_fs_group_name,
            )
            session_conv_id = str(_sess.id)

            # Per-session lock key is 1-to-1 with the DB session:
            #   group → "feishu:feishu_group_{chat_id}"
            #   P2P   → "feishu:feishu_p2p_{user_id_or_open_id}"
            lock_key = f"feishu:{conv_id}"

            # Feishu has no emoji "thinking" reaction (unlike DingTalk), so the
            # boundary hooks (on_consume / on_complete / on_error) stay None.
            # The loop-internal hooks are threaded directly through _work below.
            reactions = ChannelReactions()

            async def _work() -> str:
                # ── User-row write (full turn starts here, inside the session lock) ──
                from app.services.chat_history import ingest_incoming_chat_message

                ingested = await ingest_incoming_chat_message(
                    db,
                    session=_sess,
                    agent_id=agent_id,
                    user_id=platform_user_id,
                    content=user_text,
                    source_channel="feishu",
                    provider_event_id=event_id or message.get("message_id") or None,
                    channel_config_id=config.id,
                    actor_ref=sender_user_id_feishu or sender_open_id,
                    message_meta={
                        "actor_ref_type": "user_id" if sender_user_id_feishu else "open_id"
                    },
                )
                _sess.last_message_at = _dt.now(_tz.utc)
                await db.commit()

                # Mirror this inbound message to anyone viewing the session on web
                # in real time — the agent reply already streams there; this makes
                # the user's own message show up live too, not only on reload.
                from app.services.channel_llm import broadcast_channel_user_message
                await broadcast_channel_user_message(
                    agent_id, session_conv_id, content=user_text,
                    sender_name=sender_name or None, user_id=platform_user_id,
                )

                if ingested.consumed_by_onmessage:
                    logger.info(
                        "[Feishu] Inbound event %s routed to %d on_message execution(s)",
                        event_id,
                        len(ingested.execution_ids),
                    )
                    return ""

                # Load history inside the lock so concurrent turns cannot observe
                # each other's not-yet-committed rows (race condition fix).
                from app.services.chat_history import load_history_for_llm
                history = await load_history_for_llm(
                    db,
                    agent_id=agent_id,
                    conversation_id=session_conv_id,
                    ctx_size=ctx_size,
                    is_group=(chat_type == "group"),
                )

                # Build the message we'll send to the LLM. Group chats get a
                # platform-injected <sender> prefix (see spec §4.0/§4.1); P2P keeps
                # the legacy `[发送者: ...]` plain prefix (its history is single-
                # speaker so message-level tagging would be redundant).
                from app.services.sender_attribution import wrap_with_sender

                llm_user_text = user_text
                if chat_type == "group":
                    llm_user_text = wrap_with_sender(
                        user_text,
                        platform_user_id,
                        sender_name or platform_user.display_name,
                    )
                elif sender_name:
                    llm_user_text = f"[发送者: {sender_name}] {user_text}"

                # ── Inject recent uploaded file context ──────────────────────────
                # Check the uploads directory for recently modified files (within 30 min).
                # This is more reliable than scanning DB history, because the file save
                # to disk always succeeds even if the DB transaction fails. Uses the
                # storage backend abstraction (local FS or remote object store).
                try:
                    import time as _time
                    _storage = get_storage_backend()
                    _upload_key = agent_upload_key(agent_id, "placeholder").rsplit("/", 1)[0]
                    _recent_file_path = None
                    if "uploads/" not in user_text and "workspace/" not in user_text:
                        _now = _time.time()
                        if await _storage.exists(_upload_key) and await _storage.is_dir(_upload_key):
                            _candidates = sorted(
                                [e for e in await _storage.list_dir(_upload_key) if not e.is_dir],
                                key=_storage_mtime,
                                reverse=True,
                            )
                            for _entry in _candidates:
                                _mtime = _storage_mtime(_entry)
                                if _mtime and (_now - _mtime) < 1800:
                                    _recent_file_path = f"uploads/{_entry.name}"
                                    break
                    if _recent_file_path:
                        # _recent_file_path is relative to uploads dir; agent workspace root is
                        # AGENT_DATA_DIR/{agent_id}/, so the correct relative path is workspace/uploads/
                        _ws_rel_path = f"workspace/{_recent_file_path}"
                        llm_user_text = (
                            llm_user_text
                            + f"\n\n[系统提示：用户刚上传了文件，路径为工作区 `{_ws_rel_path}`。"
                            f"如果用户的指令涉及这篇文章、这个文件、这份文档等，"
                            f"请立即调用 read_document(path=\"{_ws_rel_path}\") 读取内容，不要先用 list_files 验证，直接读取即可。]"
                        )
                        logger.info(f"[Feishu] Injected recent file hint: {_ws_rel_path}")
                except Exception as _fe:
                    logger.error(f"[Feishu] File injection error: {_fe}")

                # Set sender open_id contextvar so calendar tool can auto-invite the requester
                from app.services.agent_tools import channel_feishu_sender_open_id as _cfso
                _cfso_token = _cfso.set(sender_open_id)

                # Set channel_file_sender contextvar so the agent can send files back via Feishu
                from app.services.agent_tools import channel_file_sender as _cfs
                _reply_to_id = chat_id if chat_type == "group" else sender_open_id
                _rid_type = "chat_id" if chat_type == "group" else "open_id"

                async def _feishu_file_sender(file_path, msg: str = ""):
                    try:
                        await feishu_service.upload_and_send_file(
                            config.app_id, config.app_secret,
                            _reply_to_id, file_path,
                            receive_id_type=_rid_type,
                            accompany_msg=msg,
                        )
                    except Exception as _upload_err:
                        # Fallback: send a download link when upload permission is not granted
                        from pathlib import Path as _P
                        from app.config import get_settings as _gs_fallback
                        _fs = _gs_fallback()
                        _base_url = getattr(_fs, 'BASE_URL', '').rstrip('/') or ''
                        _fp = _P(file_path)
                        # Resolve a workspace-relative path for the download link.
                        # Prefer the "workspace/" anchor (robust across storage
                        # backends); fall back to STORAGE_LOCAL_ROOT / AGENT_DATA_DIR.
                        _parts = list(_fp.parts)
                        try:
                            _workspace_idx = _parts.index("workspace")
                            _rel = "/".join(_parts[_workspace_idx:])
                        except ValueError:
                            _ws_root = _P(getattr(_fs, "STORAGE_LOCAL_ROOT", "") or _fs.AGENT_DATA_DIR)
                            try:
                                _rel = str(_fp.relative_to(_ws_root / str(agent_id)))
                            except ValueError:
                                _rel = _fp.name
                        _fallback_parts = []
                        if msg:
                            _fallback_parts.append(msg)
                        if _base_url:
                            _dl_url = f"{_base_url}/api/agents/{agent_id}/files/download?path={_rel}"
                            _fallback_parts.append(f"📎 {_fp.name}\n🔗 {_dl_url}")
                        _fallback_parts.append(
                            f"⚠️ 文件直接发送失败（{_upload_err}）\n"
                            "如需 Agent 直接发飞书文件，请在飞书开放平台为应用开启 "
                            "`im:resource`（即 `im:resource:upload`）权限并发布版本。"
                        )
                        await feishu_service.send_message(
                            config.app_id, config.app_secret,
                            _reply_to_id, "text",
                            _json.dumps({"text": "\n\n".join(_fallback_parts)}),
                            receive_id_type=_rid_type,
                        )

                _cfs_token = _cfs.set(_feishu_file_sender)

                _reply_target = chat_id if chat_type == "group" and chat_id else sender_open_id
                _reply_rid_type = "chat_id" if chat_type == "group" and chat_id else "open_id"
                # Quote the user's original message in groups so the agent's reply
                # threads under it in the Feishu client (Phase 2 #3). Outside group
                # chats we don't need quoting — P2P already has a single thread.
                _reply_to_user_msg_id = (
                    message.get("message_id") or "" if chat_type == "group" else ""
                )

                # ── Streaming card state (intra-turn, orthogonal to the per-session lock) ──
                _stream_buffer: list[str] = []
                _thinking_buffer: list[str] = []
                _thinking_output_enabled = resolve_im_thinking_enabled(agent_obj, _sess)
                _agent_name = agent_obj.name if agent_obj else "AI 回复"
                _tool_errors: list[str] = []
                _tool_status_running: dict[str, str] = {}
                _tool_status_done: list[str] = []
                _patch_queue = _SerialPatchQueue()
                _heartbeat_task: asyncio.Task | None = None
                _llm_done = False
                _last_flushed_hash: int = 0
                _last_flush_time = 0.0
                _flush_interval = 1.0
                _patch_msg_id: str | None = None
                _flush_lock = asyncio.Lock()

                def _visible_tool_status_lines() -> list[str]:
                    done_visible = _tool_status_done[-_TOOL_STATUS_KEEP_LINES:]
                    running_visible = list(_tool_status_running.values())
                    return done_visible + running_visible

                async def _queue_patch_card(card: dict, stage: str) -> None:
                    if not _patch_msg_id:
                        return
                    payload = _json.dumps(card)

                    async def _job():
                        try:
                            await feishu_service.patch_message(
                                config.app_id,
                                config.app_secret,
                                _patch_msg_id,
                                payload,
                                stage=stage,
                            )
                        except Exception as e:
                            logger.warning(f"[Feishu] Patch failed (stage={stage}, message_id={_patch_msg_id}): {e}")

                    _patch_queue.enqueue(_job)

                _init_card = _build_card(
                    answer_text="",
                    streaming=True,
                    agent_name=_agent_name,
                )
                try:
                    _init_resp = await feishu_service.send_message(
                        config.app_id,
                        config.app_secret,
                        _reply_target,
                        "interactive",
                        _json.dumps(_init_card),
                        receive_id_type=_reply_rid_type,
                        stage="stream_init_card",
                        reply_to_message_id=_reply_to_user_msg_id or None,
                    )
                    _patch_msg_id = _init_resp.get("data", {}).get("message_id")
                except Exception as e:
                    logger.error(f"[Feishu] Failed to send init streaming card: {e}")

                async def _flush_stream(reason: str, force: bool = False):
                    nonlocal _last_flushed_hash, _last_flush_time
                    if not _patch_msg_id:
                        return
                    async with _flush_lock:
                        now = time.time()
                        if not force and now - _last_flush_time < _flush_interval:
                            return
                        accumulated = "".join(_stream_buffer)
                        thinking_text = "".join(_thinking_buffer) if _thinking_output_enabled else ""
                        tool_status_lines = _visible_tool_status_lines()
                        current_hash = hash(accumulated + thinking_text + "\n".join(tool_status_lines))
                        if reason == "heartbeat" and current_hash == _last_flushed_hash:
                            return
                        _last_flushed_hash = current_hash
                        card = _build_card(
                            answer_text=accumulated,
                            thinking_text=thinking_text,
                            streaming=True,
                            tool_status_lines=tool_status_lines,
                            agent_name=_agent_name,
                        )
                        await _queue_patch_card(card, stage=f"stream_{reason}")
                        _last_flush_time = now

                # ── Loop-internal streaming callbacks ──────────────────────────────
                # These are the ChannelReactions loop hooks threaded into _call_agent_llm.
                # Defined here (inside _work) so they close over the per-turn state above.
                async def _ws_on_chunk(text: str):
                    _stream_buffer.append(text)
                    if _patch_msg_id:
                        try:
                            await _flush_stream("chunk")
                        except Exception as _e:
                            logger.warning(f"[Feishu] chunk flush failed (ignored): {_e}")

                async def _ws_on_thinking(text: str):
                    _thinking_buffer.append(text)
                    if _patch_msg_id:
                        try:
                            await _flush_stream("thinking")
                        except Exception as _e:
                            logger.warning(f"[Feishu] thinking flush failed (ignored): {_e}")

                async def _ws_on_tool_call(evt: dict):
                    tool_name = evt.get("name") or "unknown_tool"
                    call_id = evt.get("call_id") or tool_name
                    status = (evt.get("status") or "").lower()
                    result = evt.get("result")
                    if status == "running":
                        _tool_status_running[call_id] = f"⏳ Tool running: `{tool_name}`"
                    elif status == "done":
                        _tool_status_running.pop(call_id, None)
                        normalized_error = _normalize_tool_error(tool_name, result)
                        if normalized_error:
                            _tool_errors.append(normalized_error)
                            _tool_status_done.append(f"❌ Tool failed: `{tool_name}`")
                        else:
                            _tool_status_done.append(f"✅ Tool done: `{tool_name}`")
                    elif status and status not in {"running", "done"}:
                        _tool_status_running.pop(call_id, None)
                        _tool_errors.append(f"`{tool_name}`: tool status `{status}`")
                        _tool_status_done.append(f"ℹ️ Tool update: `{tool_name}` ({status})")

                    # Persistence is centralized in _call_agent_llm via the shared
                    # persist_tool_call (one canonical schema for every channel).
                    # This callback only drives live IM progress hints — no DB write.
                    if _patch_msg_id:
                        try:
                            await _flush_stream("tool", force=True)
                        except Exception as _flush_err:
                            logger.warning(f"[Feishu] tool-status flush failed (ignored): {_flush_err}")

                # Register callbacks into the ChannelReactions bundle (single source of truth)
                reactions.on_chunk = _ws_on_chunk
                reactions.on_thinking = _ws_on_thinking
                reactions.on_tool_call = _ws_on_tool_call

                async def _heartbeat():
                    while not _llm_done:
                        await asyncio.sleep(_flush_interval)
                        if _patch_msg_id:
                            await _flush_stream("heartbeat")

                if _patch_msg_id:
                    _heartbeat_task = asyncio.create_task(_heartbeat())

                # Call LLM — pass loop hooks via the reactions bundle (single source of truth)
                try:
                    reply_text = await _call_agent_llm(
                        db,
                        agent_id,
                        llm_user_text,
                        history=history,
                        user_id=platform_user_id,
                        session_id=session_conv_id,
                        on_chunk=reactions.on_chunk,
                        on_thinking=reactions.on_thinking,
                        on_tool_call=reactions.on_tool_call,
                        is_group=(chat_type == "group"),
                        turn_anchor_id=ingested.message.id,
                    )
                finally:
                    _llm_done = True
                    if _heartbeat_task:
                        _heartbeat_task.cancel()
                        try:
                            await _heartbeat_task
                        except (Exception, asyncio.CancelledError):
                            pass
                    _cfs.reset(_cfs_token)
                    _cfso.reset(_cfso_token)
                logger.info(f"[Feishu] LLM reply: {reply_text[:100]}")

                # If task creation detected, create a real Task record
                if task_match:
                    task_title = task_match.group(1).strip()
                    if task_title:
                        try:
                            from app.models.task import Task as TaskModel
                            from app.models.agent import Agent as AgentModel
                            from app.services.task_executor import execute_task
                            import asyncio as _asyncio

                            # Find the agent's creator to use as task creator
                            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
                            agent_obj_task = agent_r.scalar_one_or_none()
                            creator_id_task = agent_obj_task.creator_id if agent_obj_task else agent_id

                            task_obj = TaskModel(
                                agent_id=agent_id,
                                title=task_title,
                                created_by=creator_id_task,
                                status="pending",
                                priority="medium",
                            )
                            db.add(task_obj)
                            await db.commit()
                            await db.refresh(task_obj)
                            _asyncio.create_task(execute_task(task_obj.id, agent_id))
                            reply_text += f"\n\n📋 已同步创建任务到任务面板：【{task_title}】"
                            logger.info(f"[Feishu] Created task: {task_title}")
                        except Exception as e:
                            logger.error(f"[Feishu] Failed to create task: {e}")
                            reply_text += f"\n\n⚠️ 任务已识别，但写入任务面板失败：{str(e)[:150]}"

                final_reply_text = _append_error_details(reply_text, _tool_errors)
                final_card = _build_card(
                    answer_text=final_reply_text or "...",
                    thinking_text="",
                    streaming=False,
                    tool_status_lines=_visible_tool_status_lines(),
                    agent_name=_agent_name,
                )

                if _patch_msg_id:
                    try:
                        await _patch_queue.drain()
                    except Exception as e:
                        logger.warning(f"[Feishu] Drain patch queue failed before final patch: {e}")
                    try:
                        await feishu_service.patch_message(
                            config.app_id,
                            config.app_secret,
                            _patch_msg_id,
                            _json.dumps(final_card),
                            stage="stream_final",
                        )
                    except Exception as e:
                        logger.error(f"[Feishu] Failed to patch final interactive reply: {e}")
                        try:
                            await feishu_service.send_message(
                                config.app_id,
                                config.app_secret,
                                _reply_target,
                                "text",
                                _json.dumps({"text": final_reply_text}),
                                receive_id_type=_reply_rid_type,
                                stage="final_after_task_fallback_text",
                            )
                        except Exception as e2:
                            logger.error(f"[Feishu] Failed to send fallback text reply: {e2}")
                else:
                    try:
                        await feishu_service.send_message(
                            config.app_id,
                            config.app_secret,
                            _reply_target,
                            "interactive",
                            _json.dumps(final_card),
                            receive_id_type=_reply_rid_type,
                            stage="final_after_task",
                        )
                    except Exception as e:
                        logger.error(f"[Feishu] Failed to send final interactive reply: {e}")
                        try:
                            await feishu_service.send_message(
                                config.app_id,
                                config.app_secret,
                                _reply_target,
                                "text",
                                _json.dumps({"text": final_reply_text}),
                                receive_id_type=_reply_rid_type,
                                stage="final_after_task_fallback_text",
                            )
                        except Exception as e2:
                            logger.error(f"[Feishu] Failed to send fallback text reply: {e2}")

                # Log activity
                from app.services.activity_logger import log_activity
                await log_activity(agent_id, "chat_reply", f"回复了飞书消息: {final_reply_text[:80]}", detail={"channel": "feishu", "user_text": user_text[:200], "reply": final_reply_text[:500]})

                # Save assistant reply via the shared writer. Its own session stamps
                # created_at at save time (after the tool loop), so the reply orders
                # AFTER the turn's tool calls instead of being folded into the web
                # UI's analysis card.
                from app.services.chat_history import persist_assistant_reply
                from app.database import async_session as _areply_session
                await persist_assistant_reply(
                    _areply_session, agent_id=agent_id, user_id=platform_user_id,
                    conversation_id=session_conv_id, content=final_reply_text,
                    thinking="".join(_thinking_buffer) or None,
                    turn_anchor_id=ingested.message.id,
                )
                _sess.last_message_at = _dt.now(_tz.utc)
                await db.commit()

                return final_reply_text

            await run_channel_message(lock_key, is_command=False, reactions=reactions, work=_work)

    return {"code": 0, "msg": "ok"}


IMPORT_RE = None  # lazy sentinel
_FILE_ACK_MESSAGES = [
    "收到你的文件，请问有什么需要帮忙的？",
    "文件收到了！你想让我怎么处理它？",
    "好的，我已经收到这份文件，请告诉我你的需求~",
    "已收到文件，随时准备好为你处理！",
    "收到！请问希望我对这份文件做什么？",
]


async def _handle_feishu_file(
    db,
    agent_id,
    config,
    message,
    sender_open_id,
    sender_user_id_from_event,
    chat_type,
    chat_id,
):
    """Handle an incoming Feishu file/image before acknowledging the event."""
    import asyncio, random, json
    from app.models.agent import Agent as AgentModel
    from app.models.user import User as UserModel
    from app.services.channel_session import find_or_create_channel_session
    from app.core.security import hash_password
    from app.database import async_session as _async_session
    from datetime import datetime as _dt, timezone as _tz
    import uuid as _uuid
    from sqlalchemy import select as _select

    msg_type = message.get("message_type", "file")
    message_id = message.get("message_id", "")
    content = json.loads(message.get("content", "{}"))

    # Extract file key and name
    if msg_type == "image":
        file_key = content.get("image_key", "")
        filename = f"image_{file_key[-8:]}.jpg" if file_key else "image.jpg"
        res_type = "image"
    else:
        file_key = content.get("file_key", "")
        filename = content.get("file_name") or f"file_{file_key[-8:]}.bin"
        res_type = "file"

    if not file_key:
        logger.warning(f"[Feishu] No file_key in {msg_type} message")
        return

    # Resolve workspace upload dir
    # Download the file
    try:
        file_bytes = await feishu_service.download_message_resource(
            config.app_id, config.app_secret, message_id, file_key, res_type
        )
        _, workspace_path, save_path = await store_agent_upload(
            agent_id,
            filename,
            file_bytes,
            content_type="image/jpeg" if msg_type == "image" else None,
        )
        logger.info(f"[Feishu] Saved {msg_type} to {workspace_path} ({len(file_bytes)} bytes)")
    except Exception as e:
        logger.error(f"[Feishu] Failed to download {msg_type}: {e}")
        err_tip = "抱歉，文件下载失败。可能原因：机器人缺少 `im:resource` 权限（文件读取）。\n请在飞书开放平台 → 权限管理 → 批量导入权限 JSON → 重新发布机器人版本后重试。"
        try:
            import json as _j
            if chat_type == "group" and chat_id:
                await feishu_service.send_message(config.app_id, config.app_secret, chat_id, "text", _j.dumps({"text": err_tip}), receive_id_type="chat_id")
            else:
                await feishu_service.send_message(config.app_id, config.app_secret, sender_open_id, "text", _j.dumps({"text": err_tip}))
        except Exception as e2:
            logger.error(f"[Feishu] Also failed to send error tip: {e2}")
        raise RuntimeError(f"Feishu {msg_type} event was not durably ingested") from e

    # Resolve platform user and session using a fresh db session
    async with _async_session() as db:
        agent_r = await db.execute(_select(AgentModel).where(AgentModel.id == agent_id))
        agent_obj = agent_r.scalar_one_or_none()

        # Resolve sender's Feishu user_id (more stable than open_id)
        sender_user_id_feishu = sender_user_id_from_event or ""
        extra_info: dict | None = {
            "open_id": sender_open_id,
            "external_id": sender_user_id_feishu or None,
        }
        try:
            import httpx as _hx
            async with _hx.AsyncClient() as _fc:
                _tr = await _fc.post(
                    "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
                    json={"app_id": config.app_id, "app_secret": config.app_secret},
                )
                _at = _tr.json().get("app_access_token", "")
                if _at:
                    _ur = await _fc.get(
                        f"https://open.feishu.cn/open-apis/contact/v3/users/{sender_open_id}",
                        params={"user_id_type": "open_id"},
                        headers={"Authorization": f"Bearer {_at}"},
                    )
                    _ud = _ur.json()
                    if _ud.get("code") == 0:
                        _user_info = _ud.get("data", {}).get("user", {})
                        sender_user_id_feishu = _user_info.get("user_id", "")
                        # Feishu contact API returns 'avatar' as a dict
                        # (keys: avatar_240, avatar_640, avatar_origin), NOT a plain URL.
                        _raw_avatar = _user_info.get("avatar")
                        if isinstance(_raw_avatar, dict):
                            _avatar_url = (
                                _raw_avatar.get("avatar_240")
                                or _raw_avatar.get("avatar_640")
                                or _raw_avatar.get("avatar_origin")
                                or ""
                            )
                        else:
                            _avatar_url = _raw_avatar or ""
                        extra_info = {
                            "name": _user_info.get("name"),
                            "avatar_url": _avatar_url,
                            "email": _user_info.get("email"),
                            "mobile": _user_info.get("mobile"),
                            "external_id": _user_info.get("user_id"),
                            "unionid": _user_info.get("union_id"),
                            "open_id": sender_open_id,
                        }
        except Exception:
            pass

        # Resolve channel user via unified service (uses OrgMember + SSO patterns)
        from app.services.channel_user_service import channel_user_service
        try:
            platform_user = await channel_user_service.resolve_channel_user(
                db=db,
                agent=agent_obj,
                channel_type="feishu",
                # For Feishu, external_user_id is strictly user_id (tenant-stable).
                external_user_id=sender_user_id_feishu or None,
                extra_info=extra_info,
            )
        except Exception as e:
            from app.services.channel_user_service import ChannelUserResolutionError

            if isinstance(e, ChannelUserResolutionError):
                logger.warning(f"[Feishu] File sender resolution refused: {e}")
                _reply_to = chat_id if chat_type == "group" else sender_open_id
                _rid_type = "chat_id" if chat_type == "group" else "open_id"
                await feishu_service.send_message(
                    config.app_id,
                    config.app_secret,
                    _reply_to,
                    "text",
                    json.dumps({"text": _USER_RESOLUTION_ERROR_TIP}),
                    receive_id_type=_rid_type,
                )
                return
            raise
        platform_user_id = platform_user.id

        # Conv ID — prefer user_id for session continuity.
        # NOTE: sender_user_id_feishu may have been refined by the API call above;
        # use the refined value (falls back to sender_user_id_from_event or open_id).
        if chat_type == "group" and chat_id:
            conv_id = f"feishu_group_{chat_id}"
        else:
            conv_id = f"feishu_p2p_{sender_user_id_feishu or sender_open_id}"

        _is_group_file = (chat_type == "group")
        sender_name_file = extra_info.get("name", "") if extra_info else ""

    # Per-session lock key (same formula as text path, 1-to-1 with DB session)
    lock_key = f"feishu:{conv_id}"

    # For images: call LLM so vision models can actually see the image.
    # _work covers user-row write → LLM → reply persistence (full turn, inside lock).
    if msg_type == "image":
        import json as _json_card_img

        async def _image_work() -> str:
            # ── User-row write + session setup (inside the session lock) ──
            async with _async_session() as _db_setup:
                _ag_r = await _db_setup.execute(_select(AgentModel).where(AgentModel.id == agent_id))
                _ag_obj = _ag_r.scalar_one_or_none()
                _file_user_id_img = _ag_obj.creator_id if (_is_group_file and _ag_obj) else platform_user_id
                _agent_name_img = _ag_obj.name if _ag_obj else "AI"
                ctx_size_img = (_ag_obj.context_window_size or 20) if _ag_obj else 20

                # Find/create session (so we have session_conv_id before writing user row)
                _fs_file_group_name = None
                if _is_group_file:
                    try:
                        _chat_info = await feishu_service.get_chat_info(config.app_id, config.app_secret, chat_id)
                        _real_name = (_chat_info or {}).get("name") if _chat_info else None
                        _fs_file_group_name = (
                            _real_name.strip() if _real_name and _real_name.strip()
                            else f"Feishu Group {chat_id[:12]}"
                        )
                    except Exception as _gci_err:
                        logger.warning(f"[Feishu] chat-info lookup failed (image path): {_gci_err}")
                        _fs_file_group_name = f"Feishu Group {chat_id[:12]}"

                _sess_img = await find_or_create_channel_session(
                    db=_db_setup, agent_id=agent_id, user_id=_file_user_id_img,
                    external_conv_id=conv_id, source_channel="feishu",
                    first_message_title=f"[图片] {filename}",
                    is_group=_is_group_file,
                    group_name=_fs_file_group_name,
                )
                session_conv_id_img = str(_sess_img.id)

                import base64 as _b64_img
                _b64_data = _b64_img.b64encode(file_bytes).decode("ascii")
                _image_marker = f"[image_data:data:image/jpeg;base64,{_b64_data}]"
                user_msg_content_img = f"[用户发送了图片]\n{_image_marker}"
                from app.services.chat_history import ingest_incoming_chat_message

                _image_ingested = await ingest_incoming_chat_message(
                    _db_setup,
                    session=_sess_img,
                    agent_id=agent_id,
                    user_id=platform_user_id,
                    content=f"[file:{filename}]",
                    source_channel="feishu",
                    provider_event_id=message_id or None,
                    channel_config_id=config.id,
                    actor_ref=sender_user_id_feishu or sender_open_id,
                    message_meta={
                        "message_type": "image",
                        "workspace_path": workspace_path,
                        "actor_ref_type": "user_id" if sender_user_id_feishu else "open_id",
                    },
                )
                _sess_img.last_message_at = _dt.now(_tz.utc)

                from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE
                ctx_size_img = ctx_size_img if ctx_size_img > 0 else DEFAULT_CONTEXT_WINDOW_SIZE
                from app.services.chat_history import load_history_for_llm as _load_hist_llm
                _history_img = await _load_hist_llm(
                    _db_setup,
                    agent_id=agent_id,
                    conversation_id=session_conv_id_img,
                    ctx_size=ctx_size_img,
                    is_group=_is_group_file,
                )
                await _db_setup.commit()

            # Mirror this inbound file message to anyone viewing the session on web
            # in real time (matches what a reload renders: the [file:...] row).
            from app.services.channel_llm import broadcast_channel_user_message
            await broadcast_channel_user_message(
                agent_id, session_conv_id_img, content=f"[file:{filename}]",
                sender_name=sender_name_file or None, user_id=platform_user_id,
            )

            if _image_ingested.consumed_by_onmessage:
                logger.info("[Feishu] Image event %s routed to on_message", message_id)
                return ""

            # ── Streaming card setup ──
            _reply_to = chat_id if chat_type == "group" else sender_open_id
            _rid_type_img = "chat_id" if chat_type == "group" else "open_id"
            _img_reply_to_user_msg_id = message_id if chat_type == "group" else ""
            _init_card_img = {
                "config": {"update_multi": True},
                "header": {"template": "blue", "title": {"content": "识别图片中...", "tag": "plain_text"}},
                "elements": [{"tag": "markdown", "content": "..."}]
            }
            _patch_msg_id = None
            try:
                _init_resp = await feishu_service.send_message(
                    config.app_id, config.app_secret, _reply_to, "interactive",
                    _json_card_img.dumps(_init_card_img), receive_id_type=_rid_type_img,
                    stage="image_stream_init_card",
                    reply_to_message_id=_img_reply_to_user_msg_id or None,
                )
                _patch_msg_id = _init_resp.get("data", {}).get("message_id")
            except Exception as _e_init:
                logger.error(f"[Feishu] Failed to send init card for image: {_e_init}")

            _img_stream_buf: list[str] = []
            _img_last_flush = time.time()
            _img_flush_interval = 1.0
            _img_patch_queue = _SerialPatchQueue()
            _img_heartbeat_task: asyncio.Task | None = None
            _img_llm_done = False
            _img_last_flushed_hash: int = 0
            _img_thinking_enabled = resolve_im_thinking_enabled(_ag_obj, _sess_img)

            async def _queue_image_patch(_card: dict, _stage: str):
                if not _patch_msg_id:
                    return
                _payload = _json_card_img.dumps(_card)
                async def _job():
                    try:
                        await feishu_service.patch_message(
                            config.app_id, config.app_secret, _patch_msg_id, _payload, stage=_stage,
                        )
                    except Exception as _e_patch:
                        logger.warning(f"[Feishu] Image patch failed (stage={_stage}): {_e_patch}")
                _img_patch_queue.enqueue(_job)

            async def _flush_image_stream(reason: str, force: bool = False):
                nonlocal _img_last_flush, _img_last_flushed_hash
                now = time.time()
                if not force and now - _img_last_flush < _img_flush_interval:
                    return
                _answer_text = "".join(_img_stream_buf)
                _thinking_text = "".join(_img_thinking_chunks) if _img_thinking_enabled else ""
                _card = _build_card(
                    _answer_text,
                    thinking_text=_thinking_text,
                    streaming=True,
                    agent_name=_agent_name_img,
                )
                current_hash = hash(_answer_text + _thinking_text)
                if reason == "heartbeat" and current_hash == _img_last_flushed_hash:
                    return
                _img_last_flushed_hash = current_hash
                await _queue_image_patch(_card, _stage=f"image_stream_{reason}")
                _img_last_flush = now

            _img_thinking_chunks: list[str] = []

            async def _img_on_chunk(text: str):
                _img_stream_buf.append(text)
                if _patch_msg_id:
                    await _flush_image_stream("chunk")

            async def _img_on_thinking(text: str):
                _img_thinking_chunks.append(text)
                if _patch_msg_id:
                    await _flush_image_stream("thinking")

            async def _img_heartbeat():
                while not _img_llm_done:
                    await asyncio.sleep(_img_flush_interval)
                    if _patch_msg_id:
                        await _flush_image_stream("heartbeat")

            if _patch_msg_id:
                _img_heartbeat_task = asyncio.create_task(_img_heartbeat())

            # Group chats get a platform-injected <sender> prefix (spec §4.0/§4.1).
            from app.services.sender_attribution import wrap_with_sender

            llm_user_msg_content = user_msg_content_img
            if chat_type == "group":
                llm_user_msg_content = wrap_with_sender(
                    user_msg_content_img,
                    platform_user_id,
                    sender_name_file or platform_user.display_name,
                )
            elif sender_name_file:
                llm_user_msg_content = f"[发送者: {sender_name_file}] {user_msg_content_img}"

            # ── LLM call ──
            async with _async_session() as _db_img:
                try:
                    reply_text = await _call_agent_llm(
                        _db_img, agent_id, llm_user_msg_content, history=_history_img,
                        user_id=platform_user_id, session_id=session_conv_id_img,
                        on_chunk=_img_on_chunk,
                        on_thinking=_img_on_thinking,
                        is_group=_is_group_file,
                        turn_anchor_id=_image_ingested.message.id,
                    )
                finally:
                    _img_llm_done = True
                    if _img_heartbeat_task:
                        _img_heartbeat_task.cancel()
                        try:
                            await _img_heartbeat_task
                        except Exception:
                            pass

            logger.info(f"[Feishu] Image LLM reply: {reply_text[:100]}")

            # ── Send final card / fallback ──
            if _patch_msg_id:
                try:
                    await _img_patch_queue.drain()
                except Exception as _e_drain:
                    logger.warning(f"[Feishu] Image patch queue drain failed: {_e_drain}")
                _final_card = _build_card(reply_text or "...", streaming=False, agent_name=_agent_name_img)
                await feishu_service.patch_message(
                    config.app_id, config.app_secret, _patch_msg_id,
                    _json_card_img.dumps(_final_card), stage="image_stream_final"
                )
            else:
                try:
                    await feishu_service.send_message(
                        config.app_id, config.app_secret, _reply_to, "text",
                        json.dumps({"text": reply_text}), receive_id_type=_rid_type_img,
                        stage="image_stream_fallback_text",
                    )
                except Exception as _e_fb:
                    logger.error(f"[Feishu] Failed to send image reply: {_e_fb}")

            # ── Persist reply + log ──
            from app.services.chat_history import persist_assistant_reply
            await persist_assistant_reply(
                _async_session, agent_id=agent_id, user_id=platform_user_id,
                conversation_id=session_conv_id_img, content=reply_text,
                thinking="".join(_img_thinking_chunks) or None,
                turn_anchor_id=_image_ingested.message.id,
            )
            from app.services.activity_logger import log_activity
            await log_activity(agent_id, "chat_reply", f"回复了飞书图片消息: {reply_text[:80]}",
                               detail={"channel": "feishu", "type": "image"})
            return reply_text

        await run_channel_message(lock_key, is_command=False, reactions=ChannelReactions(), work=_image_work)
        return

    # For non-image files: send simple ack and persist
    # Set up session (needed for persist_assistant_reply)
    # 群聊文件 ack 也需要获取群名称，避免会话标题退化为 "[文件] filename"
    _ack_group_name = None
    if _is_group_file and chat_id:
        try:
            _ack_chat_info = await feishu_service.get_chat_info(config.app_id, config.app_secret, chat_id)
            _ack_real_name = (_ack_chat_info or {}).get("name") if _ack_chat_info else None
            _ack_group_name = (
                _ack_real_name.strip() if _ack_real_name and _ack_real_name.strip()
                else f"Feishu Group {chat_id[:12]}"
            )
        except Exception as _ack_gci_err:
            logger.warning(f"[Feishu] chat-info lookup failed (file ack path): {_ack_gci_err}")
            _ack_group_name = f"Feishu Group {chat_id[:12]}"

    async with _async_session() as _db_ack:
        _ag_r_ack = await _db_ack.execute(_select(AgentModel).where(AgentModel.id == agent_id))
        _ag_obj_ack = _ag_r_ack.scalar_one_or_none()
        _file_user_id_ack = _ag_obj_ack.creator_id if (_is_group_file and _ag_obj_ack) else platform_user_id
        _sess_ack = await find_or_create_channel_session(
            db=_db_ack, agent_id=agent_id, user_id=_file_user_id_ack,
            external_conv_id=conv_id, source_channel="feishu",
            first_message_title=f"[文件] {filename}",
            is_group=_is_group_file,
            group_name=_ack_group_name,
        )
        session_conv_id_ack = str(_sess_ack.id)
        from app.services.chat_history import ingest_incoming_chat_message

        _file_ingested = await ingest_incoming_chat_message(
            _db_ack,
            session=_sess_ack,
            agent_id=agent_id,
            user_id=platform_user_id,
            content=f"[file:{filename}]",
            source_channel="feishu",
            provider_event_id=message_id or None,
            channel_config_id=config.id,
            actor_ref=sender_user_id_feishu or sender_open_id,
            message_meta={
                "message_type": "file",
                "workspace_path": workspace_path,
                "actor_ref_type": "user_id" if sender_user_id_feishu else "open_id",
            },
        )
        _sess_ack.last_message_at = _dt.now(_tz.utc)
        await _db_ack.commit()

    # Mirror this inbound file message to anyone viewing the session on web in
    # real time (matches what a reload renders: the [file:...] row).
    from app.services.channel_llm import broadcast_channel_user_message
    await broadcast_channel_user_message(
        agent_id, session_conv_id_ack, content=f"[file:{filename}]",
        sender_name=sender_name_file or None, user_id=platform_user_id,
    )

    if _file_ingested.consumed_by_onmessage:
        logger.info("[Feishu] File event %s routed to on_message", message_id)
        return

    await asyncio.sleep(random.uniform(1.0, 2.0))

    ack = random.choice(_FILE_ACK_MESSAGES)
    try:
        if chat_type == "group" and chat_id:
            await feishu_service.send_message(
                config.app_id, config.app_secret, chat_id, "text",
                json.dumps({"text": ack}), receive_id_type="chat_id",
            )
        else:
            await feishu_service.send_message(
                config.app_id, config.app_secret, sender_open_id, "text",
                json.dumps({"text": ack}),
            )
    except Exception as e:
        logger.error(f"[Feishu] Failed to send ack: {e}")

    # Store ack via the shared writer (consistent with every channel).
    from app.services.chat_history import persist_assistant_reply
    await persist_assistant_reply(
        _async_session, agent_id=agent_id, user_id=platform_user_id,
        conversation_id=session_conv_id_ack, content=ack,
        turn_anchor_id=_file_ingested.message.id,
    )



async def _download_post_images(agent_id, config, message_id, image_keys):
    """Download images embedded in a Feishu post message to the agent's workspace."""
    for ik in image_keys:
        try:
            file_bytes = await feishu_service.download_message_resource(
                config.app_id, config.app_secret, message_id, ik, "image"
            )
            _, workspace_path, _ = await store_agent_upload(
                agent_id,
                f"image_{ik[-8:]}.jpg",
                file_bytes,
                content_type="image/jpeg",
            )
            logger.info(f"[Feishu] Saved post image to {workspace_path} ({len(file_bytes)} bytes)")
        except Exception as e:
                logger.error(f"[Feishu] Failed to download post image {ik}: {e}")
