"""Microsoft Teams Bot Channel API routes."""

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent as AgentModel
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.channel_config import ChannelConfigPublic as ChannelConfigOut
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_llm import _call_agent_llm
from app.services.im_thinking_output import BufferedIMThinkingSender, resolve_im_thinking_enabled
from app.services.im_markdown_media import project_agent_images_for_im
from app.services.teams_delivery import _send_teams_message_single_chunk
from app.services.agent_tools import channel_file_sender as _cfs_s
from app.core.security import hash_password as _hp
from pathlib import Path as _Path
import asyncio as _asyncio
import random as _random

settings = get_settings()

router = APIRouter(tags=["microsoft_teams"])

TEAMS_MSG_LIMIT = 28000  # Teams message char limit (approx 28KB)

async def _get_teams_access_token(config: ChannelConfig) -> str | None:
    """Get or refresh Microsoft Teams access token.
    
    Supports:
    - Client credentials (app_id + app_secret) - default
    - Managed Identity (when use_managed_identity is True in extra_config)
    Token is cached in Redis (preferred) with in-memory fallback.
    Key: clawith:token:teams:{agent_id}
    """
    from app.core.token_cache import get_cached_token, set_cached_token

    agent_id = str(config.agent_id)
    cache_key = f"clawith:token:teams:{agent_id}"

    cached = await get_cached_token(cache_key)
    if cached:
        logger.debug(f"Teams: Using cached access token for agent {agent_id}")
        return cached

    # Check if managed identity should be used
    use_managed_identity = config.extra_config.get("use_managed_identity", False)
    
    if use_managed_identity:
        # Use Azure Managed Identity
        try:
            from azure.identity.aio import DefaultAzureCredential
            from azure.core.credentials import AccessToken
            
            credential = DefaultAzureCredential()
            # For Bot Framework, we need the token for the Bot Framework API
            # Managed identity needs to be granted permissions to the Bot Framework API
            scope = "https://api.botframework.com/.default"
            token: AccessToken = await credential.get_token(scope)
            
            # expires_on is a Unix timestamp; TTL = expires_on - now - 60s buffer
            ttl = max(int(token.expires_on - time.time()) - 60, 60)
            await set_cached_token(cache_key, token.token, ttl)
            logger.info(f"Teams: Successfully obtained access token via managed identity for agent {agent_id}, expires at {token.expires_on}")
            await credential.close()
            return token.token
        except ImportError:
            logger.error(f"Teams: azure-identity package not installed. Install it with: pip install azure-identity")
            return None
        except Exception as e:
            logger.exception(f"Teams: Failed to get access token via managed identity for agent {agent_id}: {e}")
            return None
    
    # Use client credentials (app_id + app_secret)
    app_id = config.app_id
    app_secret = config.app_secret
    if not app_id or not app_secret:
        logger.error(f"Teams: Missing app_id or app_secret for agent {agent_id}")
        return None

    # Get tenant_id from config (per-agent), environment variable, or default to "common" (multi-tenant)
    tenant_id = config.extra_config.get("tenant_id") or os.environ.get("TEAMS_TENANT_ID") or "common"
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": app_id,
        "client_secret": app_secret,
        "grant_type": "client_credentials",
        "scope": "https://api.botframework.com/.default",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(token_url, data=data)
            if resp.status_code != 200:
                error_body = resp.text
                try:
                    error_json = resp.json()
                    error_description = error_json.get("error_description", "No description")
                    error_code = error_json.get("error", "unknown")
                    logger.error(f"Teams: OAuth token request failed for agent {agent_id}: status={resp.status_code}, error={error_code}, description={error_description}")
                except:
                    logger.error(f"Teams: OAuth token request failed for agent {agent_id}: status={resp.status_code}, response={error_body[:500]}")
                logger.error(f"Teams: Token URL={token_url}, tenant_id={tenant_id}, client_id={app_id[:20]}...")
                return None
            token_data = resp.json()
            access_token = token_data["access_token"]
            expires_in = token_data["expires_in"]

            # TTL = expires_in - 60s buffer
            ttl = max(expires_in - 60, 60)
            await set_cached_token(cache_key, access_token, ttl)
            logger.info(f"Teams: Successfully obtained access token for agent {agent_id}, expires in {expires_in}s, TTL={ttl}s")
            return access_token
    except httpx.HTTPStatusError as e:
        error_body = e.response.text if hasattr(e, 'response') and e.response else "No response body"
        try:
            if hasattr(e, 'response') and e.response:
                error_json = e.response.json()
                error_description = error_json.get("error_description", "No description")
                error_code = error_json.get("error", "unknown")
                logger.error(f"Teams: OAuth token HTTP error for agent {agent_id}: status={e.response.status_code}, error={error_code}, description={error_description}")
        except:
            logger.error(f"Teams: OAuth token HTTP error for agent {agent_id}: status={e.response.status_code if hasattr(e, 'response') and e.response else 'unknown'}, response={error_body[:500]}")
        logger.error(f"Teams: Token URL={token_url}, tenant_id={tenant_id}, client_id={app_id[:20]}...")
        return None
    except Exception as e:
        logger.exception(f"Teams: Failed to get access token for agent {agent_id}: {e}")
        return None


async def _send_teams_message(
    config: ChannelConfig,
    conversation_id: str,
    activity: dict,
    *,
    on_result=None,
) -> list[dict]:
    """Send an activity (message) to Microsoft Teams."""
    access_token = await _get_teams_access_token(config)
    if not access_token:
        logger.error(f"Teams: No access token for agent {config.agent_id}, cannot send message")
        raise ValueError("No access token available")

    service_url = config.extra_config.get("service_url")
    if not service_url:
        logger.error(f"Teams: No service_url in config for agent {config.agent_id}, cannot send message")
        raise ValueError(f"No service_url in config for agent {config.agent_id}")

    # Ensure activity has required fields
    if "type" not in activity:
        activity["type"] = "message"
    if "timestamp" not in activity:
        activity["timestamp"] = datetime.now(timezone.utc).isoformat() + "Z"

    # Teams API expects 'replyToId' for replies, not 'conversation.id'
    # If it's a reply, ensure the 'id' field is set to the message being replied to
    if activity.get("replyToId") and "id" not in activity:
        activity["id"] = str(uuid.uuid4())  # Generate a new ID for the reply activity

    # Teams has a 28KB limit for message activities. Chunk if needed.
    text_content = activity.get("text", "")
    results: list[dict] = []
    if len(text_content.encode("utf-8")) > TEAMS_MSG_LIMIT:
        chunks = [text_content[i:i + TEAMS_MSG_LIMIT] for i in range(0, len(text_content), TEAMS_MSG_LIMIT)]
        for i, chunk in enumerate(chunks):
            chunk_activity = {**activity, "text": chunk}
            if i > 0:  # Only the first chunk is a direct reply, subsequent are new messages
                chunk_activity.pop("replyToId", None)
            result = await _send_teams_message_single_chunk(
                access_token, service_url, conversation_id, chunk_activity
            )
            results.append(result)
            if on_result is not None:
                await on_result(result)
    else:
        result = await _send_teams_message_single_chunk(
            access_token, service_url, conversation_id, activity
        )
        results.append(result)
        if on_result is not None:
            await on_result(result)
    return results


# ─── Config CRUD ────────────────────────────────────────

@router.post("/agents/{agent_id}/teams-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_teams_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure Microsoft Teams bot for an agent. Fields: app_id, app_secret."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    app_id = data.get("app_id", "").strip()
    app_secret = data.get("app_secret", "").strip()
    tenant_id = data.get("tenant_id", "").strip()  # Optional: for single-tenant apps
    use_managed_identity = data.get("use_managed_identity", False)  # Optional: use Azure Managed Identity
    
    # Validate: either managed identity OR app_id + app_secret required
    if not use_managed_identity and (not app_id or not app_secret):
        raise HTTPException(status_code=422, detail="Either use_managed_identity must be enabled, or app_id and app_secret are required")

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "microsoft_teams",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.app_id = app_id if not use_managed_identity else existing.app_id
        existing.app_secret = app_secret if not use_managed_identity else existing.app_secret
        existing.is_configured = True
        # Store tenant_id and use_managed_identity in extra_config
        if not existing.extra_config:
            existing.extra_config = {}
        if tenant_id:
            existing.extra_config["tenant_id"] = tenant_id
        elif "tenant_id" in existing.extra_config and not tenant_id:
            # Remove tenant_id if not provided (use default)
            existing.extra_config.pop("tenant_id", None)
        existing.extra_config["use_managed_identity"] = use_managed_identity
        await db.flush()
        return ChannelConfigOut.model_validate(existing)

    extra_config = {}
    if tenant_id:
        extra_config["tenant_id"] = tenant_id
    if use_managed_identity:
        extra_config["use_managed_identity"] = True
    
    config = ChannelConfig(
        agent_id=agent_id,
        channel_type="microsoft_teams",
        app_id=app_id if not use_managed_identity else None,
        app_secret=app_secret if not use_managed_identity else None,
        is_configured=True,
        extra_config=extra_config,
    )
    db.add(config)
    await db.flush()
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/teams-channel", response_model=ChannelConfigOut)
async def get_teams_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get Microsoft Teams channel configuration for an agent."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "microsoft_teams",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Microsoft Teams not configured")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/teams-channel/webhook-url")
async def get_teams_webhook_url(
    agent_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the Microsoft Teams webhook URL for an agent."""
    await check_agent_access(db, current_user, agent_id)
    from app.services.platform_service import platform_service
    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/teams/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/teams-channel", status_code=204)
async def delete_teams_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete Microsoft Teams channel configuration for an agent."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "microsoft_teams",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Microsoft Teams not configured")
    await db.delete(config)
    await db.commit()


# ─── Event Webhook ──────────────────────────────────────

@router.post("/channel/teams/{agent_id}/webhook")
async def teams_event_webhook(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Microsoft Teams Bot Framework callbacks."""
    try:
        body_bytes = await request.body()
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError as e:
            logger.error(f"Teams: Failed to parse JSON body: {e}, body={body_bytes[:200]}")
            return Response(status_code=400, content="Invalid JSON")
        
        # Microsoft Teams Bot Framework sends the activity directly in the body (not wrapped in "activity" key)
        # Check if body itself is the activity (has "type" field) or if it's wrapped
        if isinstance(body, dict) and "type" in body:
            activity = body
        elif isinstance(body, dict) and "activity" in body:
            activity = body["activity"]
        else:
            logger.warning(f"Teams: Unexpected body structure for agent {agent_id}: {list(body.keys()) if isinstance(body, dict) else type(body)}")
            activity = body if isinstance(body, dict) else {}
        
        # Get channel config
        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "microsoft_teams",
            )
        )
        config = result.scalar_one_or_none()
        if not config:
            logger.warning(f"Teams: Webhook received for unconfigured agent {agent_id}")
            return Response(status_code=404)

        from app.services.webhook_security import (
            WebhookVerificationError,
            verify_teams_service_url,
            verify_teams_webhook,
        )
        try:
            claims = await verify_teams_webhook(
                request.headers.get("authorization"), config.app_id, activity.get("channelId")
            )
            service_url = verify_teams_service_url(activity.get("serviceUrl"), claims)
        except WebhookVerificationError as exc:
            logger.warning(f"Teams: rejected unauthenticated webhook for agent {agent_id}: {exc}")
            return Response(status_code=401, content="Unauthorized")

        logger.info(f"Teams: Authenticated webhook received for agent {agent_id}, activity type={activity.get('type')}")

        # Extract serviceUrl from the activity for sending replies
        if service_url:
            if config.extra_config.get("service_url") != service_url:
                config.extra_config = {**(config.extra_config or {}), "service_url": service_url}
                config.is_connected = True
                await db.flush()
                await db.commit()
                logger.info(f"Teams: Updated service_url for agent {agent_id} to {service_url}")

        activity_id = activity.get("id")

        # Only process message activities
        if activity.get("type") != "message":
            return {"ok": True}
        if not activity_id:
            logger.warning(f"Teams: rejected message without activity id for agent {agent_id}")
            return Response(status_code=400, content="Activity id is required")

        # Ignore bot's own messages
        # Check if the message is from the bot itself (either by app_id or by comparing with recipient)
        bot_id = config.app_id
        if not bot_id:
            # If no app_id, use the recipient ID from the activity (the bot is the recipient)
            bot_id = activity.get("recipient", {}).get("id")
        if bot_id and activity.get("from", {}).get("id") == bot_id:
            return {"ok": True}

        user_text = activity.get("text", "").strip()
        if not user_text:
            return {"ok": True}

        # Extract conversation and sender info
        conversation_id = activity.get("conversation", {}).get("id")
        sender_id = activity.get("from", {}).get("id")
        sender_name = activity.get("from", {}).get("name", f"Teams User {sender_id[:8]}")
        reply_to_id = activity.get("id")  # The ID of the incoming message to reply to

        if not conversation_id or not sender_id:
            logger.warning(f"Teams: Missing conversation_id or sender_id in activity for agent {agent_id}")
            return {"ok": True}

        logger.info(f"Teams: Message from={sender_id}, conversation={conversation_id}: {user_text[:80]}")

        # Load agent (must happen before user resolution for tenant_id)
        agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
        agent_obj = agent_r.scalar_one_or_none()
        from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE
        ctx_size = (agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE) if agent_obj else DEFAULT_CONTEXT_WINDOW_SIZE

        # Find-or-create platform user for this Teams sender via unified service
        from app.services.channel_user_service import channel_user_service
        _extra_info = {"name": sender_name}
        platform_user = await channel_user_service.resolve_channel_user(
            db=db,
            agent=agent_obj,
            channel_type="teams",
            external_user_id=sender_id,
            extra_info=_extra_info,
        )

        # Update display_name if we now have a better name
        if sender_name and platform_user.display_name and platform_user.display_name.startswith("Teams User ") and sender_name != platform_user.display_name:
            platform_user.display_name = sender_name
            await db.flush()
        platform_user_id = platform_user.id

        # Detect group vs P2P chat
        _conv_type = activity.get("conversation", {}).get("conversationType", "")
        _is_group_teams = (_conv_type in ("groupChat", "channel"))

        # 同一 Teams 会话的多轮消息串行化（防止并发入队导致工具调用历史交错）
        from app.services.channel_dispatch import (
            ChannelReactions,
            channel_session_lock_key,
            run_channel_message,
        )
        from app.services.channel_commands import is_channel_command, prepare_channel_command_reply

        lock_key = channel_session_lock_key(
            agent_id,
            "microsoft_teams",
            conversation_id,
        )

        # Early-return for channel commands (/new, /reset):
        # archive the session and send a canned reply — no LLM, no lock needed.
        if is_channel_command(user_text):
            cmd_result = await prepare_channel_command_reply(
                db=db, command=user_text, agent_id=agent_id,
                user_id=platform_user_id, external_conv_id=conversation_id,
                external_user_id=sender_id,
                source_channel="microsoft_teams",
                provider_event_id=activity_id,
                is_group=_is_group_teams,
            )
            await db.commit()
            if not cmd_result["should_deliver"]:
                return {"ok": True}
            use_mi_cmd = config.extra_config.get("use_managed_identity", False)
            has_creds_cmd = (config.app_id and config.app_secret) or use_mi_cmd
            if has_creds_cmd:
                bot_channel_account_cmd = activity.get("recipient", {})
                if not bot_channel_account_cmd.get("id") and config.app_id:
                    bot_channel_account_cmd = {"id": config.app_id}
                user_account_cmd = activity.get("from", {})
                if not user_account_cmd.get("id"):
                    user_account_cmd = {"id": sender_id, "name": sender_name}
                cmd_reply_activity = {
                    "type": "message",
                    "from": bot_channel_account_cmd,
                    "conversation": {"id": conversation_id},
                    "recipient": user_account_cmd,
                    "replyToId": reply_to_id,
                    "text": cmd_result["message"],
                }
                from app.services.im_delivery import (
                    IMDeliveryPart,
                    PersistedDeliveryRecorder,
                )
                _cmd_recorder = PersistedDeliveryRecorder(
                    cmd_result["message_id"],
                    "microsoft_teams",
                )
                _cmd_service_url = str(config.extra_config.get("service_url") or "")

                async def _record_command_part(item: dict) -> None:
                    await _cmd_recorder.append(IMDeliveryPart(
                        transport="microsoft_teams",
                        provider_message_id=str(item.get("id") or "") or None,
                        conversation_ref=conversation_id,
                        artifact_role="command_reply",
                        recallable=bool(item.get("id")),
                        metadata={"service_url": _cmd_service_url},
                    ))

                try:
                    await _send_teams_message(
                        config,
                        conversation_id,
                        cmd_reply_activity,
                        on_result=_record_command_part,
                    )
                    await _cmd_recorder.sent()
                except Exception as _cmd_e:
                    await _cmd_recorder.failed(_cmd_e)
                    logger.error(f"Teams: Failed to send command reply: {_cmd_e}")
            else:
                from app.services.im_delivery import IMDeliveryResult, register_delivery
                await register_delivery(
                    cmd_result["message_id"],
                    IMDeliveryResult.failed("teams", "command_route_unavailable"),
                )
            return {"ok": True}

        async def _work() -> str:
            # 正常消息轮次：会话解析 → 写入用户行 → LLM → 持久化回复 → 发送
            # Find-or-create session for this Teams conversation
            sess = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=platform_user_id if not _is_group_teams else (agent_obj.creator_id if agent_obj else platform_user_id),
                external_conv_id=conversation_id,
                source_channel="microsoft_teams",
                first_message_title=user_text,
                is_group=_is_group_teams,
                group_name=activity.get("conversation", {}).get("name") or (f"Teams Group {conversation_id[:8]}" if _is_group_teams else None),
            )
            session_conv_id = str(sess.id)
            from app.services.chat_history import load_history_for_llm
            history = await load_history_for_llm(
                db,
                agent_id=agent_id,
                conversation_id=session_conv_id,
                ctx_size=ctx_size,
                is_group=False,  # group-chat sender wrap not enabled for Teams yet
            )

            from app.services.chat_history import ingest_incoming_chat_message

            ingested = await ingest_incoming_chat_message(
                db,
                session=sess,
                agent_id=agent_id,
                user_id=platform_user_id,
                content=user_text,
                source_channel="microsoft_teams",
                provider_event_id=activity_id,
                channel_config_id=config.id,
                actor_ref=sender_id,
                reply_to_external_message_id=activity.get("replyToId"),
            )
            sess.last_message_at = datetime.now(timezone.utc)
            await db.commit()

            # Mirror this inbound message to anyone viewing the session on web in
            # real time — the agent reply already streams there; this makes the
            # user's own message show up live too, not only on reload.
            from app.services.channel_llm import broadcast_channel_user_message
            await broadcast_channel_user_message(
                agent_id, session_conv_id, message=ingested.message,
                sender_name=sender_name or None, user_id=platform_user_id,
            )

            if ingested.consumed_by_onmessage:
                logger.info(
                    "Teams: inbound event %s routed to %d on_message execution(s)",
                    activity_id,
                    len(ingested.execution_ids),
                )
                return ""

            # Set channel_file_sender contextvar for agent → user file delivery
            async def _teams_file_sender(file_path, msg: str = ""):
                from app.services.im_delivery import IMDeliveryPart, IMDeliveryResult
                from app.services.agent_tools import record_channel_file_part

                _fp = _Path(file_path)
                use_mi = config.extra_config.get("use_managed_identity", False)
                has_creds = (config.app_id and config.app_secret) or use_mi
                if not has_creds or not conversation_id:
                    return
                # For simplicity, just send file info as text for now
                file_msg_activity = {
                    "type": "message",
                    "conversation": {"id": conversation_id},
                    "replyToId": reply_to_id,
                    "text": f"Agent sent file: {_fp.name} (Note: file content not directly supported yet, but I can tell you about it: {msg})",
                }
                parts = []

                async def _record_result(result: dict) -> None:
                    part = IMDeliveryPart(
                        transport="microsoft_teams",
                        provider_message_id=str(result.get("id") or "") or None,
                        conversation_ref=conversation_id,
                        artifact_role="file_fallback",
                        recallable=bool(result.get("id")),
                        metadata={"service_url": str(config.extra_config.get("service_url") or "")},
                    )
                    parts.append(part)
                    await record_channel_file_part(part)

                await _send_teams_message(
                    config,
                    conversation_id,
                    file_msg_activity,
                    on_result=_record_result,
                )
                if not parts:
                    part = IMDeliveryPart(
                        transport="microsoft_teams",
                        conversation_ref=conversation_id,
                        artifact_role="file_fallback",
                        recallable=False,
                        metadata={"service_url": str(config.extra_config.get("service_url") or "")},
                    )
                    parts.append(part)
                    await record_channel_file_part(part)
                return IMDeliveryResult.sent("teams", *parts)

            _cfs_s_token = _cfs_s.set(_teams_file_sender)

            # Call LLM
            _thinking_chunks: list[str] = []

            _thinking_sender = BufferedIMThinkingSender.for_runtime(
                enabled=resolve_im_thinking_enabled(agent_obj, sess),
                agent_id=agent_id,
                user_id=platform_user_id,
                conversation_id=session_conv_id,
                turn_anchor_id=ingested.message.id,
            )

            async def _collect_thinking(text: str):
                _thinking_chunks.append(text)
                await _thinking_sender.push(text)

            try:
                reply_text = await _call_agent_llm(
                    db, agent_id, user_text,
                    history=history, user_id=platform_user_id, session_id=session_conv_id,
                    on_thinking=_collect_thinking,
                    turn_anchor_id=ingested.message.id,
                )
                logger.info(f"Teams: LLM reply generated: {reply_text[:80]}")
            except Exception as e:
                logger.exception(f"Teams: Failed to call LLM for agent {agent_id}: {e}")
                reply_text = "Sorry, I encountered an error processing your message."
            finally:
                await _thinking_sender.flush()
                _cfs_s.reset(_cfs_s_token)

            # Save reply
            from app.services.im_delivery import (
                IMDeliveryPart,
                IMDeliveryResult,
                append_delivery_part,
                attach_delivery_to_meta,
                register_delivery,
            )

            assistant_message_id = None
            try:
                # Save assistant reply via the shared writer. Its own session stamps
                # created_at at save time (after the tool loop), so the reply orders
                # AFTER the turn's tool calls instead of being folded into the web UI's
                # analysis card.
                from app.services.chat_history import persist_assistant_reply
                from app.database import async_session as _areply_session
                assistant_message_id = await persist_assistant_reply(
                    _areply_session, agent_id=agent_id, user_id=platform_user_id,
                    conversation_id=session_conv_id, content=reply_text,
                    thinking="".join(_thinking_chunks) or None,
                    message_meta=attach_delivery_to_meta(
                        {},
                        IMDeliveryResult.pending("microsoft_teams"),
                    ),
                    turn_anchor_id=ingested.message.id,
                    required=True,
                )
                sess.last_message_at = datetime.now(timezone.utc)
                await db.commit()
                logger.info(f"Teams: Saved reply to database for conversation {conversation_id}")
            except Exception as e:
                logger.exception(f"Teams: Failed to save reply to database: {e}")
                await db.rollback()
                raise
            delivery_reply_text = await project_agent_images_for_im(agent_id, reply_text)

            # Send to Teams
            delivery_result = IMDeliveryResult.failed("microsoft_teams", "channel_config_unavailable")
            use_managed_identity = config.extra_config.get("use_managed_identity", False)
            has_credentials = (config.app_id and config.app_secret) or use_managed_identity
            if has_credentials and conversation_id:
                try:
                    # Get bot's channel account ID from the incoming activity's recipient field
                    # The recipient in the incoming message is the bot itself
                    bot_channel_account = activity.get("recipient", {})
                    if not bot_channel_account.get("id"):
                        # Fallback: use app_id if recipient not available
                        if config.app_id:
                            bot_channel_account = {"id": config.app_id}
                        else:
                            logger.error(f"Teams: Cannot determine bot channel account ID - no recipient in activity and no app_id configured")
                            raise ValueError("Cannot determine bot channel account ID")

                    # Get the user (sender) from the incoming activity's from field
                    user_account = activity.get("from", {})
                    if not user_account.get("id"):
                        user_account = {"id": sender_id, "name": sender_name}

                    reply_activity = {
                        "type": "message",
                        "from": bot_channel_account,  # Required: Bot's channel account ID (from incoming activity's recipient)
                        "conversation": {"id": conversation_id},
                        "recipient": user_account,  # The user who sent the message (from incoming activity's from)
                        "replyToId": reply_to_id,  # Reply to the specific incoming message
                        "text": delivery_reply_text,
                    }
                    logger.info(f"Teams: Attempting to send reply to conversation {conversation_id}, from={bot_channel_account.get('id')}, recipient={user_account.get('id')}")
                    service_url = str((config.extra_config or {}).get("service_url") or "")

                    async def _record_teams_part(response: dict) -> None:
                        part = IMDeliveryPart(
                            transport="microsoft_teams",
                            provider_message_id=str(response.get("id") or "") or None,
                            conversation_ref=conversation_id,
                            artifact_role="chunk",
                            recallable=bool(response.get("id")),
                            metadata={"service_url": service_url},
                        )
                        if not await append_delivery_part(assistant_message_id, part):
                            raise RuntimeError("delivery_part_persistence_failed")

                    responses = await _send_teams_message(
                        config,
                        conversation_id,
                        reply_activity,
                        on_result=_record_teams_part,
                    )
                    delivery_result = IMDeliveryResult.sent(
                        "microsoft_teams",
                        *(
                            IMDeliveryPart(
                                transport="microsoft_teams",
                                provider_message_id=str(response.get("id") or "") or None,
                                conversation_ref=conversation_id,
                                artifact_role="chunk",
                                recallable=bool(response.get("id")),
                                metadata={"service_url": service_url},
                            )
                            for response in responses
                        ),
                    )
                    logger.info(f"Teams: Successfully sent reply to Teams")
                except Exception as e:
                    logger.exception(f"Teams: Failed to send message to Teams: {e}")
                    delivery_result = IMDeliveryResult.from_exception("microsoft_teams", e)
            else:
                use_mi = config.extra_config.get("use_managed_identity", False)
                logger.warning(f"Teams: Cannot send reply - missing credentials (managed_identity={use_mi}, app_id={bool(config.app_id)}, app_secret={bool(config.app_secret)}), conversation_id={bool(conversation_id)}")
            if assistant_message_id is not None:
                await register_delivery(assistant_message_id, delivery_result)

            return reply_text

        await run_channel_message(lock_key, is_command=False, reactions=ChannelReactions(), work=_work)
        return {"ok": True}
    except Exception as e:
        logger.exception(f"Teams: Unhandled exception in webhook handler for agent {agent_id}: {e}")
        return Response(status_code=500, content="Internal server error")
