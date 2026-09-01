from __future__ import annotations

import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.org import OrgMember
from app.services.agent_tools_channel_file_receipts import record_channel_file_part
from app.services.agent_tools_file_support import _agent_workspace_root
from app.services.channel_session import find_or_create_channel_session
from app.services.im_delivery import DeliveryReceiptPersistenceError, IMDeliveryPart, IMDeliveryResult
from app.services.recipient_resolver import RecipientResolutionError, resolve_human_channel_recipient
from app.services.user_output import sanitize_user_visible_text


async def _send_file_to_session(
    agent_id: uuid.UUID,
    file_path: Path,
    session_id: str,
    message: str = "",
) -> tuple[str, IMDeliveryResult]:
    """Deliver a generic file through one exact Agent-owned IM Session."""
    try:
        target_session_id = uuid.UUID(session_id)
    except (TypeError, ValueError):
        error = RecipientResolutionError(
            "session_not_found_or_forbidden",
            "Target Session does not exist or is not accessible to this Agent",
        )
        return error.as_json(), IMDeliveryResult.failed("im", error.code)

    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == target_session_id,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            error = RecipientResolutionError(
                "session_not_found_or_forbidden",
                "Target Session does not exist or is not accessible to this Agent",
            )
            return error.as_json(), IMDeliveryResult.failed("im", error.code)

        channel = str(session.source_channel or "").strip()
        external_conv_id = str(session.external_conv_id or "").strip()
        if not external_conv_id or "__archived_" in external_conv_id:
            error = RecipientResolutionError(
                "session_route_unavailable",
                "Target Session has no active delivery route",
            )
            return error.as_json(), IMDeliveryResult.failed(channel or "im", error.code)
        config_channel = "microsoft_teams" if channel == "teams" else channel
        config = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == config_channel,
                    ChannelConfig.is_configured.is_(True),
                )
            )
        ).scalar_one_or_none()

    if not config:
        error = RecipientResolutionError(
            "channel_unconfigured",
            f"Source agent has no configured {channel} channel",
        )
        return error.as_json(), IMDeliveryResult.failed(channel or "im", error.code)

    if channel == "feishu":
        expected_prefix = "feishu_group_" if session.is_group else "feishu_p2p_"
        if not external_conv_id.startswith(expected_prefix):
            error = RecipientResolutionError(
                "session_route_mismatch",
                "Target Session route does not match its person/group type",
            )
            return error.as_json(), IMDeliveryResult.failed("feishu", error.code)
        receive_id = external_conv_id[len(expected_prefix) :].strip()
        if not receive_id:
            error = RecipientResolutionError(
                "session_route_unavailable", "Target Session has no active delivery route"
            )
            return error.as_json(), IMDeliveryResult.failed("feishu", error.code)
        receive_id_type = (
            "chat_id" if session.is_group else ("open_id" if receive_id.startswith("ou_") else "user_id")
        )
        from app.services.feishu_service import feishu_service
        delivery_parts: list[IMDeliveryPart] = []

        async def _record_feishu_result(
            artifact_role: str,
            provider_result: dict,
        ) -> None:
            provider_id = str(
                (provider_result.get("data") or {}).get("message_id")
                or provider_result.get("message_id")
                or ""
            ) or None
            part = IMDeliveryPart(
                transport="feishu_message",
                provider_message_id=provider_id,
                conversation_ref=receive_id,
                artifact_role=artifact_role,
                recallable=bool(provider_id),
            )
            delivery_parts.append(part)
            await record_channel_file_part(part)

        try:
            await feishu_service.upload_and_send_file(
                config.app_id,
                config.app_secret,
                receive_id,
                file_path,
                receive_id_type=receive_id_type,
                accompany_msg=message,
                on_result=_record_feishu_result,
            )
            if not delivery_parts:
                part = IMDeliveryPart(
                    transport="feishu_message",
                    conversation_ref=receive_id,
                    artifact_role="channel_file",
                    recallable=False,
                )
                delivery_parts.append(part)
                await record_channel_file_part(part)
            return (
                f"File '{file_path.name}' sent to Session {session.id} via Feishu.",
                IMDeliveryResult.sent("feishu", *delivery_parts),
            )
        except DeliveryReceiptPersistenceError:
            raise
        except Exception as exc:
            return (
                f"Failed to send file via Feishu: {exc}",
                IMDeliveryResult.from_exception("feishu", exc),
            )

    if channel == "slack":
        if not external_conv_id.startswith("slack_"):
            error = RecipientResolutionError(
                "session_route_mismatch", "Target Session route is not a Slack conversation"
            )
            return error.as_json(), IMDeliveryResult.failed("slack", error.code)
        slack_channel_id = external_conv_id[len("slack_") :].strip()
        if not slack_channel_id:
            error = RecipientResolutionError(
                "session_route_unavailable", "Target Session has no active delivery route"
            )
            return error.as_json(), IMDeliveryResult.failed("slack", error.code)
        return await _send_file_via_slack_channel(
            config,
            file_path,
            slack_channel_id,
            message,
            display_name=f"Session {session.id}",
        )

    if channel == "dingtalk":
        expected_prefix = "dingtalk_group_" if session.is_group else "dingtalk_p2p_"
        if not external_conv_id.startswith(expected_prefix):
            error = RecipientResolutionError(
                "session_route_mismatch",
                "Target Session route does not match its person/group type",
            )
            return error.as_json(), IMDeliveryResult.failed("dingtalk", error.code)
        target_id = external_conv_id[len(expected_prefix) :].strip()
        if not target_id:
            error = RecipientResolutionError(
                "session_route_unavailable", "Target Session has no active delivery route"
            )
            return error.as_json(), IMDeliveryResult.failed("dingtalk", error.code)
        from app.services.dingtalk_stream import (
            _send_dingtalk_media_message,
            _upload_dingtalk_media,
        )

        media_type = (
            "image"
            if file_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
            else "file"
        )
        media_id = await _upload_dingtalk_media(
            config.app_id, config.app_secret, str(file_path), media_type
        )
        if not media_id:
            return (
                "Failed to send file via DingTalk: media upload failed",
                IMDeliveryResult.failed("dingtalk", "media_upload_failed"),
            )
        delivery_parts: list[IMDeliveryPart] = []

        async def _record_dingtalk_result(provider_result: dict) -> None:
            process_key = str(provider_result.get("processQueryKey") or "") or None
            part = IMDeliveryPart(
                transport=(
                    "dingtalk_openapi_group"
                    if session.is_group
                    else "dingtalk_openapi_oto"
                ),
                provider_message_id=process_key,
                conversation_ref=target_id,
                artifact_role="channel_file",
                recallable=bool(process_key),
            )
            delivery_parts.append(part)
            await record_channel_file_part(part)

        sent = await _send_dingtalk_media_message(
            config.app_id,
            config.app_secret,
            target_id,
            media_id,
            media_type,
            "2" if session.is_group else "1",
            filename=file_path.name,
            raise_on_transport_error=True,
            on_result=_record_dingtalk_result,
        )
        if not sent:
            return (
                "Failed to send file via DingTalk: media send failed",
                IMDeliveryResult.failed("dingtalk", "media_send_failed"),
            )
        if not delivery_parts:
            part = IMDeliveryPart(
                transport=(
                    "dingtalk_openapi_group"
                    if session.is_group
                    else "dingtalk_openapi_oto"
                ),
                conversation_ref=target_id,
                artifact_role="channel_file",
                recallable=False,
            )
            delivery_parts.append(part)
            await record_channel_file_part(part)
        caption_error: str | None = None
        caption_uncertain = False
        if message:
            try:
                from app.services.turn_runtime import send_dingtalk_proactive_markdown

                result = await send_dingtalk_proactive_markdown(
                    app_id=config.app_id,
                    app_secret=config.app_secret,
                    target_id=target_id,
                    is_group=bool(session.is_group),
                    message=message,
                )
                if result.get("errcode") != 0:
                    caption_error = str(
                        result.get("errmsg")
                        or result.get("errcode")
                        or "caption_send_failed"
                    )
                    logger.warning(
                        "[send_channel_file] DingTalk caption delivery failed: {}",
                        result.get("errcode"),
                    )
                else:
                    process_key = str(result.get("processQueryKey") or "") or None
                    part = IMDeliveryPart(
                        transport=(
                            "dingtalk_openapi_group"
                            if session.is_group
                            else "dingtalk_openapi_oto"
                        ),
                        provider_message_id=process_key,
                        conversation_ref=target_id,
                        artifact_role="file_caption",
                        recallable=bool(process_key),
                    )
                    delivery_parts.append(part)
                    await record_channel_file_part(part)
            except DeliveryReceiptPersistenceError:
                raise
            except Exception as exc:
                caption_error = type(exc).__name__
                caption_uncertain = (
                    IMDeliveryResult.from_exception("dingtalk", exc).status == "unknown"
                )
                logger.warning("[send_channel_file] DingTalk caption delivery failed")
        if caption_error:
            caption_status = "unknown" if caption_uncertain else "partial"
            caption_outcome = (
                "its caption delivery is uncertain"
                if caption_uncertain
                else "its caption failed"
            )
            return (
                f"File '{file_path.name}' sent to Session {session.id} via DingTalk, "
                f"but {caption_outcome}.",
                IMDeliveryResult(
                    ok=False,
                    channel="dingtalk",
                    parts=tuple(delivery_parts),
                    status=caption_status,
                    error=f"caption_failed:{caption_error}",
                ),
            )
        return (
            f"File '{file_path.name}' sent to Session {session.id} via DingTalk.",
            IMDeliveryResult.sent("dingtalk", *delivery_parts),
        )

    error = RecipientResolutionError(
        "file_route_unsupported",
        f"File delivery is not implemented for {channel}",
        available_channels=[channel] if channel else [],
    )
    return error.as_json(), IMDeliveryResult.failed(channel or "im", error.code)


async def _send_file_to_recipient(
    agent_id: uuid.UUID,
    file_path: Path,
    user_id: str,
    message: str = "",
    *,
    channel: str | None = None,
) -> tuple[str, IMDeliveryResult]:
    """Resolve one canonical recipient route and send without name lookup."""
    dingtalk_session_id: str | None = None
    async with async_session() as db:
        try:
            route = await resolve_human_channel_recipient(db, agent_id, user_id, channel=channel)
        except RecipientResolutionError as exc:
            return exc.as_json(), IMDeliveryResult.failed(channel or "im", exc.code)
        config_result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == ("microsoft_teams" if route.channel == "teams" else route.channel),
                ChannelConfig.is_configured.is_(True),
            )
        )
        config = config_result.scalar_one_or_none()
        if not config:
            error = RecipientResolutionError(
                "channel_unconfigured",
                f"Source agent has no configured {route.channel} channel",
            )
            return error.as_json(), IMDeliveryResult.failed(route.channel, error.code)
        if route.channel == "dingtalk":
            target_staff_id = str(route.member.external_id or "").strip()
            if not target_staff_id:
                error = RecipientResolutionError(
                    "recipient_unreachable",
                    "Canonical user has no usable DingTalk endpoint",
                )
                return error.as_json(), IMDeliveryResult.failed("dingtalk", error.code)
            session = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=route.user.id,
                external_conv_id=f"dingtalk_p2p_{target_staff_id}",
                source_channel="dingtalk",
                first_message_title=message.strip()[:30] or file_path.name[:30],
            )
            await db.commit()
            dingtalk_session_id = str(session.id)
    if dingtalk_session_id:
        return await _send_file_to_session(
            agent_id,
            file_path,
            dingtalk_session_id,
            message,
        )
    if route.channel == "feishu":
        return await _send_file_via_feishu(agent_id, config, file_path, route.member, route.user.display_name, message)
    if route.channel == "slack":
        return await _send_file_via_slack(
            config, file_path, route.member, route.user.display_name, message
        )
    error = RecipientResolutionError(
        "file_route_unsupported",
        f"File delivery is not implemented for {route.channel}",
        available_channels=[route.channel],
    )
    return error.as_json(), IMDeliveryResult.failed(route.channel, error.code)


async def _send_file_via_feishu(
    agent_id, config, file_path: Path, member: OrgMember, display_name: str, message: str
) -> tuple[str, IMDeliveryResult]:
    """Send a file to an already-authorized internal Feishu endpoint."""
    receive_id = (member.external_id or member.open_id or "").strip()
    id_type = "user_id" if member.external_id else "open_id"
    if not receive_id:
        error = RecipientResolutionError(
            "recipient_unreachable", "Canonical user has no usable Feishu endpoint"
        )
        return error.as_json(), IMDeliveryResult.failed("feishu", error.code)
    from app.services.feishu_service import feishu_service
    delivery_parts: list[IMDeliveryPart] = []

    async def _record_result(artifact_role: str, provider_result: dict) -> None:
        provider_id = str(
            (provider_result.get("data") or {}).get("message_id")
            or provider_result.get("message_id")
            or ""
        ) or None
        part = IMDeliveryPart(
            transport="feishu_message",
            provider_message_id=provider_id,
            conversation_ref=receive_id,
            artifact_role=artifact_role,
            recallable=bool(provider_id),
        )
        delivery_parts.append(part)
        await record_channel_file_part(part)
    try:
        await feishu_service.upload_and_send_file(
            config.app_id,
            config.app_secret,
            receive_id,
            file_path,
            receive_id_type=id_type,
            accompany_msg=message,
            on_result=_record_result,
        )
        if not delivery_parts:
            part = IMDeliveryPart(
                transport="feishu_message",
                conversation_ref=receive_id,
                artifact_role="channel_file",
                recallable=False,
            )
            delivery_parts.append(part)
            await record_channel_file_part(part)
        return (
            f"File '{file_path.name}' sent to {display_name} via Feishu.",
            IMDeliveryResult.sent("feishu", *delivery_parts),
        )
    except DeliveryReceiptPersistenceError:
        raise
    except Exception as e:
        # If upload fails, try sending a download link as fallback
        import json as _j

        from app.config import get_settings as _gs

        _s = _gs()
        safe_upload_error = sanitize_user_visible_text(str(e)).strip()[:200] or type(e).__name__
        base_url = getattr(_s, 'BASE_URL', '').rstrip('/') or ''
        base_abs = _agent_workspace_root(agent_id).resolve()
        try:
            _rel = str(file_path.resolve().relative_to(base_abs))
        except ValueError:
            _rel = file_path.name
        parts = []
        if message:
            parts.append(message)
        if base_url:
            dl_url = f"{base_url}/api/agents/{agent_id}/files/download?path={_rel}"
            parts.append(f"{file_path.name}\n{dl_url}")
        parts.append(
            f"File upload failed ({safe_upload_error}). If you need direct file sending, "
            "enable im:resource permission in Feishu."
        )
        try:
            fallback_result = await feishu_service.send_message(
                config.app_id, config.app_secret,
                receive_id, "text",
                _j.dumps({"text": "\n\n".join(parts)}, ensure_ascii=False),
                receive_id_type=id_type,
            )
            provider_id = str(
                (fallback_result.get("data") or {}).get("message_id")
                or fallback_result.get("message_id")
                or ""
            ) or None
            part = IMDeliveryPart(
                transport="feishu_message",
                provider_message_id=provider_id,
                conversation_ref=receive_id,
                artifact_role="file_fallback",
                recallable=bool(provider_id),
            )
            await record_channel_file_part(part)
            return (
                f"File upload to Feishu failed, sent download link to {display_name} instead.",
                IMDeliveryResult.sent("feishu", part),
            )
        except Exception as fallback_exc:
            return (
                f"Failed to send file to {display_name} via Feishu: {e}",
                IMDeliveryResult.from_exception("feishu", fallback_exc),
            )


async def _send_file_via_slack(
    config, file_path: Path, member: OrgMember, display_name: str, message: str
) -> tuple[str, IMDeliveryResult]:
    """Send file to an already-authorized internal Slack endpoint."""
    import httpx

    bot_token = config.app_secret or ""
    if not bot_token:
        error = RecipientResolutionError(
            "channel_unconfigured", "Slack bot token is missing"
        )
        return error.as_json(), IMDeliveryResult.failed("slack", error.code)
    slack_user_id = (member.external_id or "").strip()
    if not slack_user_id:
        error = RecipientResolutionError(
            "recipient_unreachable", "Canonical user has no usable Slack endpoint"
        )
        return error.as_json(), IMDeliveryResult.failed("slack", error.code)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Open a DM channel
            dm_resp = await client.post(
                "https://slack.com/api/conversations.open",
                headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                json={"users": slack_user_id},
            )
            dm_data = dm_resp.json()
            if not dm_data.get("ok"):
                error = str(dm_data.get("error") or "conversations_open_failed")
                return f"Slack conversations.open failed: {error}", IMDeliveryResult.failed("slack", error)
            channel_id = dm_data["channel"]["id"]

        return await _send_file_via_slack_channel(
            config,
            file_path,
            channel_id,
            message,
            display_name=display_name,
        )
    except Exception as e:
        return (
            f"Failed to send file via Slack: {e}",
            IMDeliveryResult.from_exception("slack", e),
        )


async def _send_file_via_slack_channel(
    config,
    file_path: Path,
    channel_id: str,
    message: str,
    *,
    display_name: str,
) -> tuple[str, IMDeliveryResult]:
    """Upload a generic file to one already-resolved Slack conversation."""
    import httpx

    bot_token = config.app_secret or ""
    if not bot_token:
        error = RecipientResolutionError(
            "channel_unconfigured", "Slack bot token is missing"
        )
        return error.as_json(), IMDeliveryResult.failed("slack", error.code)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            upload_url_resp = await client.post(
                "https://slack.com/api/files.getUploadURLExternal",
                headers={"Authorization": f"Bearer {bot_token}"},
                data={"filename": file_path.name, "length": str(file_path.stat().st_size)},
            )
            upload_data = upload_url_resp.json()
            if not upload_data.get("ok"):
                error = str(upload_data.get("error") or "file_upload_failed")
                return f"Slack file upload failed: {error}", IMDeliveryResult.failed("slack", error)
            await client.post(
                upload_data["upload_url"],
                content=file_path.read_bytes(),
                headers={"Content-Type": "application/octet-stream"},
            )
            complete = await client.post(
                "https://slack.com/api/files.completeUploadExternal",
                headers={"Authorization": f"Bearer {bot_token}"},
                json={
                    "files": [{"id": upload_data["file_id"]}],
                    "channel_id": channel_id,
                    "initial_comment": message or "",
                },
            )
            if not complete.json().get("ok"):
                error = str(complete.json().get("error") or "file_upload_complete_failed")
                return f"Slack file upload complete failed: {error}", IMDeliveryResult.failed("slack", error)
            part = IMDeliveryPart(
                transport="slack_file",
                provider_message_id=str(upload_data["file_id"]),
                conversation_ref=channel_id,
                artifact_role="channel_file",
                recallable=False,
            )
            await record_channel_file_part(part)
            return (
                f"File '{file_path.name}' sent to {display_name} via Slack.",
                IMDeliveryResult.sent("slack", part),
            )
    except Exception as e:
        return f"Failed to send file via Slack: {e}", IMDeliveryResult.from_exception("slack", e)


__all__ = [
    "_send_file_to_session",
    "_send_file_to_recipient",
    "_send_file_via_feishu",
    "_send_file_via_slack",
    "_send_file_via_slack_channel",
]
