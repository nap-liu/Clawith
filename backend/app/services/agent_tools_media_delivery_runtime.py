from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from loguru import logger
from sqlalchemy import select

from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.services.agent_tools_channel_file_receipts import (
    _build_outbound_operation_key,
)
from app.services.agent_tools_media_delivery_core import (
    _PLATFORM_SESSION_CHANNELS,
    _describe_media_delivery_result,
    _sniff_media_file_mime,
)
from app.services.agent_tools_media_delivery_support import (
    _outbound_media_db_session,
    _outbound_operation_lifecycle_lock,
)
from app.services.chat_attachments import (
    attachment_from_workspace_path,
    canonical_media_mime,
)
from app.services.im_delivery import (
    DeliveryReceiptPersistenceError,
    IMDeliveryPart,
    IMDeliveryResult,
    _merge_delivery_into_meta,
)
from app.services.media_tool_contract import normalize_media_display_title
from app.services.turn_runtime import TurnRuntime, deliver_message_with_receipt


async def _send_media_to_session(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    file_path: Path,
    workspace_path: str,
    media_kind: str,
    caption: str,
    cover_path: Path | None,
    intent_id: str,
    origin_session_id: str | None,
    origin_turn_anchor_id: uuid.UUID | None,
    allow_download: bool = False,
    source_mode: str = "workspace",
    tool_args: dict | None = None,
) -> str:
    """Serialize one complete media delivery, including the provider call."""
    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=intent_id or None,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    async with _outbound_operation_lifecycle_lock(operation_key):
        return await _send_media_to_session_under_lifecycle_lock(
            agent_id=agent_id,
            session_id=session_id,
            file_path=file_path,
            workspace_path=workspace_path,
            media_kind=media_kind,
            caption=caption,
            cover_path=cover_path,
            intent_id=intent_id,
            origin_session_id=origin_session_id,
            origin_turn_anchor_id=origin_turn_anchor_id,
            allow_download=allow_download,
            source_mode=source_mode,
            tool_args=tool_args,
        )


async def _send_media_to_session_under_lifecycle_lock(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    file_path: Path,
    workspace_path: str,
    media_kind: str,
    caption: str,
    cover_path: Path | None,
    intent_id: str,
    origin_session_id: str | None,
    origin_turn_anchor_id: uuid.UUID | None,
    allow_download: bool = False,
    source_mode: str = "workspace",
    tool_args: dict | None = None,
) -> str:
    """Deliver media through one exact Session with a durable no-duplicate claim."""
    try:
        target_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
                "media_kind": media_kind,
                "intent_id": intent_id,
            },
            ensure_ascii=False,
        )

    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=intent_id or None,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    sniffed_mime = _sniff_media_file_mime(file_path)
    mime_type = canonical_media_mime(file_path.name, media_kind, sniffed_mime)
    attachment = attachment_from_workspace_path(
        workspace_path,
        display_name=file_path.name,
        mime_type=mime_type,
        size_bytes=file_path.stat().st_size,
    )
    persisted_args = dict(tool_args or {"media_type": media_kind, "file_path": workspace_path})
    display_title = normalize_media_display_title(persisted_args.get("title"))
    if not operation_key:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "MISSING_DELIVERY_INTENT_ID",
                "media_kind": media_kind,
                "intent_id": intent_id,
            },
            ensure_ascii=False,
        )

    receipt_id: uuid.UUID
    channel = ""
    external_conv_id = ""
    target_is_group = False
    delivery_mode = "platform"
    dingtalk_app_id = ""
    dingtalk_app_secret = ""
    target_id = ""
    conversation_type = ""
    async with _outbound_media_db_session() as db:
        existing = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.external_event_key == operation_key).with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            meta = existing.message_meta if isinstance(existing.message_meta, dict) else {}
            state = str(meta.get("delivery_status") or "unknown")
            if state == "sent":
                caption_status = str(meta.get("caption_status") or "not_requested")
                existing_display_title = normalize_media_display_title(meta.get("display_title"))
                existing_result = _describe_media_delivery_result(
                    {
                        "type": "platform_media_delivery",
                        "version": 1,
                        "status": "already_sent",
                        "code": ("MEDIA_SENT_CAPTION_FAILED" if caption_status == "failed" else "MEDIA_ALREADY_SENT"),
                        "caption_status": caption_status,
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(existing.conversation_id),
                        "channel": str(meta.get("source_channel") or ""),
                        "path": workspace_path,
                        "filename": file_path.name,
                        **({"title": existing_display_title} if existing_display_title else {}),
                        "mime_type": mime_type,
                        "size": file_path.stat().st_size,
                        "message_id": str(existing.id),
                        "allow_download": meta.get("allow_download") is True,
                        "source_mode": str(meta.get("source_mode") or source_mode),
                    }
                )
                return json.dumps(existing_result, ensure_ascii=False)
            if state == "pending":
                next_meta = {
                    **meta,
                    "delivery_status": "unknown",
                    "delivery_code": "MEDIA_DELIVERY_STATE_UNKNOWN",
                }
                existing.message_meta = next_meta
                unknown_result = _describe_media_delivery_result(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "unknown",
                        "code": "MEDIA_DELIVERY_STATE_UNKNOWN",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(existing.conversation_id),
                        "channel": str(next_meta.get("source_channel") or ""),
                        "message_id": str(existing.id),
                    }
                )
                if existing.role == "tool_call":
                    try:
                        existing_call = json.loads(existing.content or "")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        existing_call = {}
                    existing.content = json.dumps(
                        {
                            "name": str(existing_call.get("name") or "send_media"),
                            "call_id": str(existing_call.get("call_id") or intent_id),
                            "args": existing_call.get("args")
                            or {
                                "media_type": media_kind,
                                "file_path": workspace_path,
                            },
                            "status": "done",
                            "result": json.dumps(unknown_result, ensure_ascii=False),
                            "reasoning_content": existing_call.get("reasoning_content"),
                        },
                        ensure_ascii=False,
                    )
                await db.commit()
                return json.dumps(unknown_result, ensure_ascii=False)
            existing_result = _describe_media_delivery_result(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": state if state in {"failed", "unknown", "unsupported"} else "unknown",
                    "code": (
                        "MEDIA_DELIVERY_STATE_UNKNOWN"
                        if state == "unknown"
                        else str(meta.get("delivery_code") or "MEDIA_DELIVERY_FAILED")
                    ),
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "session_id": str(existing.conversation_id),
                    "channel": str(meta.get("source_channel") or ""),
                    "message_id": str(existing.id),
                }
            )
            return json.dumps(existing_result, ensure_ascii=False)

        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == target_session_id,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                },
                ensure_ascii=False,
            )

        channel = str(session.source_channel or "").strip()
        external_conv_id = str(session.external_conv_id or "").strip()
        is_platform = channel in _PLATFORM_SESSION_CHANNELS
        target_is_group = bool(session.is_group)
        if is_platform:
            delivery_mode = "platform"
        elif channel == "dingtalk":
            if not external_conv_id or "__archived_" in external_conv_id:
                return json.dumps(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "failed",
                        "code": "SESSION_ROUTE_UNAVAILABLE",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(session.id),
                        "channel": channel,
                    },
                    ensure_ascii=False,
                )
            config = (
                await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == "dingtalk",
                        ChannelConfig.is_configured.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if not config or not config.app_id or not config.app_secret:
                return json.dumps(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "unsupported",
                        "code": "CHANNEL_MEDIA_UNSUPPORTED",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(session.id),
                        "channel": channel,
                    },
                    ensure_ascii=False,
                )
            expected_prefix = "dingtalk_group_" if target_is_group else "dingtalk_p2p_"
            wrong_prefix = "dingtalk_p2p_" if target_is_group else "dingtalk_group_"
            if not external_conv_id.startswith(expected_prefix) or external_conv_id.startswith(wrong_prefix):
                return json.dumps(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "failed",
                        "code": "SESSION_ROUTE_MISMATCH",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(session.id),
                        "channel": channel,
                    },
                    ensure_ascii=False,
                )
            target_id = external_conv_id[len(expected_prefix) :].strip()
            if not target_id:
                return json.dumps(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "failed",
                        "code": "SESSION_ROUTE_UNAVAILABLE",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(session.id),
                        "channel": channel,
                    },
                    ensure_ascii=False,
                )
            conversation_type = "2" if target_is_group else "1"
            dingtalk_app_id = str(config.app_id)
            dingtalk_app_secret = str(config.app_secret)
            delivery_mode = "native"
        else:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "unsupported",
                    "code": "CHANNEL_MEDIA_UNSUPPORTED",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "session_id": str(session.id),
                    "channel": channel,
                },
                ensure_ascii=False,
            )

        receipt: ChatMessage | None = None
        if str(origin_session_id or "") == str(session.id) and origin_turn_anchor_id:
            running_rows = (
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.agent_id == agent_id,
                            ChatMessage.conversation_id == str(session.id),
                            ChatMessage.role == "tool_call",
                            ChatMessage.external_event_key.is_(None),
                            ChatMessage.message_meta["turn_anchor_id"].as_string() == str(origin_turn_anchor_id),
                        )
                        .order_by(ChatMessage.created_at.desc())
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
            for candidate in running_rows:
                try:
                    candidate_payload = json.loads(candidate.content or "")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(candidate_payload, dict)
                    and str(candidate_payload.get("name") or "") == "send_media"
                    and str(candidate_payload.get("call_id") or "") == intent_id
                ):
                    receipt = candidate
                    break

        is_cross_session_claim = str(origin_session_id or "") != str(session.id)
        from app.services.im_delivery import IMDeliveryResult, attach_delivery_to_meta

        claim_meta = {
            "direction": "outbound",
            "source_channel": channel,
            "target_session_id": str(session.id),
            "origin_session_id": str(origin_session_id or ""),
            "tool_call_id": intent_id,
            "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
            # Cross-session mirrors are UI delivery records, not part of the
            # target Agent's reasoning history. A current-session row is the
            # caller's canonical tool result and must remain in LLM replay.
            "delivery_claim": is_cross_session_claim,
            "delivery_status": "pending",
        }
        claim_meta = attach_delivery_to_meta(
            claim_meta,
            IMDeliveryResult.pending(channel),
        )
        if receipt is None:
            receipt = ChatMessage(
                agent_id=agent_id,
                user_id=session.user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "send_media",
                        "call_id": intent_id,
                        "args": persisted_args,
                        "status": "running",
                        "result": "",
                        "reasoning_content": None,
                    },
                    ensure_ascii=False,
                ),
                conversation_id=str(session.id),
                external_event_key=operation_key,
                message_meta=claim_meta,
            )
            db.add(receipt)
        else:
            receipt.external_event_key = operation_key
            receipt.message_meta = {**dict(receipt.message_meta or {}), **claim_meta}
        session.last_message_at = datetime.now(timezone.utc)
        await db.flush()
        receipt_meta = dict(receipt.message_meta or {})
        receipt_meta["attachments"] = [attachment]
        receipt_meta["media_kind"] = media_kind
        receipt_meta["delivery_mode"] = delivery_mode
        receipt_meta["caption_status"] = "pending" if caption.strip() else "not_requested"
        receipt_meta["requested_caption"] = caption.strip()
        receipt_meta["target_is_group"] = target_is_group
        receipt_meta["delivery_code"] = "MEDIA_DELIVERY_PENDING"
        receipt_meta["allow_download"] = allow_download
        receipt_meta["source_mode"] = source_mode
        receipt_meta["display_title"] = display_title
        receipt.message_meta = receipt_meta
        await db.commit()
        receipt_id = receipt.id

    sent = True
    code = "MEDIA_SENT"
    uncertain = False
    caption_sent = channel != "dingtalk" or not bool(caption.strip())
    caption_delivery_result: IMDeliveryResult | None = None
    media_delivery_parts: list[IMDeliveryPart] = []
    media_delivery_result = IMDeliveryResult.unsupported_delivery(
        channel or "web",
        "platform_session",
        conversation_ref=str(target_session_id),
    )
    if channel == "dingtalk":
        from app.services.dingtalk_stream import (
            DINGTALK_VOICE_MAX_BYTES,
            _send_dingtalk_media_message,
            _send_dingtalk_native_video,
            _upload_dingtalk_media,
        )

        async def _record_media_part(provider_result: dict) -> None:
            process_key = str(provider_result.get("processQueryKey") or "") or None
            part = IMDeliveryPart(
                transport=(
                    "dingtalk_openapi_group"
                    if target_is_group
                    else "dingtalk_openapi_oto"
                ),
                provider_message_id=process_key,
                conversation_ref=target_id,
                artifact_role=f"media_{media_kind}",
                recallable=bool(process_key),
            )
            media_delivery_parts.append(part)
            async with _outbound_media_db_session() as part_db:
                part_receipt = await part_db.get(
                    ChatMessage,
                    receipt_id,
                    with_for_update=True,
                )
                if part_receipt is None:
                    raise DeliveryReceiptPersistenceError(
                        "provider artifact receipt persistence failed"
                    )
                part_receipt.message_meta = _merge_delivery_into_meta(
                    part_receipt.message_meta,
                    IMDeliveryResult(
                        ok=True,
                        channel=channel,
                        parts=(part,),
                        status="pending",
                    ),
                )
                await part_db.commit()

        try:
            if media_kind == "video":
                sent, code = await _send_dingtalk_native_video(
                    dingtalk_app_id,
                    dingtalk_app_secret,
                    target_id,
                    file_path,
                    conversation_type,
                    cover_image_path=cover_path,
                    raise_on_transport_error=True,
                    on_result=_record_media_part,
                )
            elif file_path.stat().st_size > DINGTALK_VOICE_MAX_BYTES:
                sent, code = False, "MEDIA_TOO_LARGE"
            else:
                media_id = await _upload_dingtalk_media(
                    dingtalk_app_id,
                    dingtalk_app_secret,
                    str(file_path),
                    "voice",
                    raise_on_transport_error=True,
                )
                sent = bool(media_id) and await _send_dingtalk_media_message(
                    dingtalk_app_id,
                    dingtalk_app_secret,
                    target_id,
                    str(media_id or ""),
                    "voice",
                    conversation_type,
                    filename=file_path.name,
                    raise_on_transport_error=True,
                    on_result=_record_media_part,
                )
                code = "MEDIA_SENT" if sent else (
                    "MEDIA_SEND_FAILED" if media_id else "MEDIA_UPLOAD_FAILED"
                )
        except Exception as exc:
            logger.opt(exception=True).error("[SessionMedia] Provider result is unknown")
            sent, uncertain, code = False, True, "MEDIA_DELIVERY_STATE_UNKNOWN"
            media_delivery_result = IMDeliveryResult.unknown(channel, type(exc).__name__)
        else:
            if sent:
                if not media_delivery_parts:
                    media_delivery_parts.append(
                        IMDeliveryPart(
                            transport=(
                                "dingtalk_openapi_group"
                                if target_is_group
                                else "dingtalk_openapi_oto"
                            ),
                            conversation_ref=target_id,
                            artifact_role=f"media_{media_kind}",
                            recallable=False,
                        )
                    )
                media_delivery_result = IMDeliveryResult.sent(
                    channel,
                    *media_delivery_parts,
                )
            else:
                media_delivery_result = IMDeliveryResult.failed(channel, code)

        if sent and caption.strip():
            runtime = TurnRuntime(
                session_found=True,
                source_channel=channel,
                conversation_id=str(target_session_id),
                external_conv_id=external_conv_id,
                is_group=target_is_group,
            )
            caption_operation_key = f"{operation_key}:caption"
            async with _outbound_media_db_session() as caption_db:
                caption_row = (
                    await caption_db.execute(
                        select(ChatMessage).where(
                            ChatMessage.external_event_key == caption_operation_key
                        )
                    )
                ).scalar_one_or_none()
                if caption_row is None:
                    media_receipt = await caption_db.get(ChatMessage, receipt_id)
                    caption_row = ChatMessage(
                        agent_id=agent_id,
                        user_id=media_receipt.user_id if media_receipt is not None else None,
                        role="assistant",
                        content=caption.strip(),
                        conversation_id=str(target_session_id),
                        external_event_key=caption_operation_key,
                        message_meta=attach_delivery_to_meta(
                            {
                                "attachments": [],
                                "media_caption_for": str(receipt_id),
                                "artifact_role": "media_caption",
                            },
                            IMDeliveryResult.pending(channel),
                        ),
                    )
                    caption_db.add(caption_row)
                    await caption_db.flush()
                caption_message_id = caption_row.id
                await caption_db.commit()
            try:
                caption_delivery_result = await deliver_message_with_receipt(
                    agent_id=agent_id,
                    runtime=runtime,
                    message=caption.strip(),
                )
                caption_sent = caption_delivery_result.ok
            except Exception as exc:
                logger.opt(exception=True).warning("[SessionMedia] Caption delivery failed")
                caption_delivery_result = IMDeliveryResult.unknown(
                    channel,
                    type(exc).__name__,
                )
                caption_sent = False

    final_status = "sent" if sent else ("unknown" if uncertain else "failed")
    final_code = "MEDIA_SENT_CAPTION_FAILED" if sent and not caption_sent else code
    events: list[dict] = []
    async with _outbound_media_db_session() as db:
        final_receipt = await db.get(ChatMessage, receipt_id, with_for_update=True)
        if final_receipt is None:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "unknown",
                    "code": "MEDIA_DELIVERY_STATE_UNKNOWN",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "session_id": str(target_session_id),
                    "channel": channel,
                },
                ensure_ascii=False,
            )
        final_meta = dict(final_receipt.message_meta or {})
        try:
            final_tool_payload = json.loads(final_receipt.content or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            final_tool_payload = {}
        if not isinstance(final_tool_payload, dict):
            final_tool_payload = {}
        if str(final_meta.get("delivery_status") or "") != "pending":
            try:
                terminal_call = json.loads(final_receipt.content or "")
                terminal_result = json.loads(terminal_call.get("result") or "")
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                terminal_result = None
            if isinstance(terminal_result, dict):
                return json.dumps(
                    _describe_media_delivery_result(terminal_result),
                    ensure_ascii=False,
                )
            return json.dumps(_describe_media_delivery_result({
                "type": "media_delivery_result", "version": 1,
                "status": "unknown", "code": "MEDIA_DELIVERY_STATE_UNKNOWN",
                "media_kind": media_kind, "intent_id": intent_id,
                "session_id": str(target_session_id), "channel": channel,
                "message_id": str(final_receipt.id),
            }), ensure_ascii=False)
        final_meta = _merge_delivery_into_meta(final_meta, media_delivery_result)
        final_meta["delivery_status"] = final_status
        final_meta["delivery_code"] = final_code
        final_meta["caption_status"] = (
            "not_requested" if not caption.strip() else ("sent" if caption_sent else "failed")
        )
        final_receipt.message_meta = final_meta
        result_payload: dict
        if sent:
            result_payload = {
                "type": "platform_media_delivery",
                "version": 1,
                "status": final_status,
                "code": final_code,
                "media_kind": media_kind,
                "path": workspace_path,
                "filename": file_path.name,
                **({"title": display_title} if display_title else {}),
                "mime_type": mime_type,
                "size": file_path.stat().st_size,
                "message_id": str(final_receipt.id),
                "allow_download": allow_download,
                "intent_id": intent_id,
                "session_id": str(target_session_id),
                "channel": channel,
                "conversation_type": "group" if target_is_group else "person",
                "delivery_mode": delivery_mode,
                "source_mode": source_mode,
            }
            result_payload = _describe_media_delivery_result(result_payload)
        else:
            result_payload = _describe_media_delivery_result(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": final_status,
                    "code": final_code,
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "session_id": str(target_session_id),
                    "channel": channel,
                    "message_id": str(final_receipt.id),
                }
            )
        final_receipt.content = json.dumps(
            {
                **final_tool_payload,
                "name": "send_media",
                "call_id": intent_id,
                "args": persisted_args,
                "status": "done",
                "result": json.dumps(result_payload, ensure_ascii=False),
                "reasoning_content": final_tool_payload.get("reasoning_content"),
            },
            ensure_ascii=False,
        )
        caption_row: ChatMessage | None = None
        if sent and caption.strip():
            caption_operation_key = f"{operation_key}:caption"
            caption_row = (
                await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == caption_operation_key))
            ).scalar_one_or_none()
            if caption_row is None and channel != "dingtalk":
                caption_delivery_result = IMDeliveryResult.unsupported_delivery(
                    channel or "web",
                    "platform_session",
                    conversation_ref=str(target_session_id),
                )
                caption_row = ChatMessage(
                    agent_id=agent_id,
                    user_id=final_receipt.user_id,
                    role="assistant",
                    content=caption.strip(),
                    conversation_id=str(target_session_id),
                    external_event_key=caption_operation_key,
                    message_meta={
                        "attachments": [],
                        "media_caption_for": str(receipt_id),
                        "artifact_role": "media_caption",
                    },
                )
                db.add(caption_row)
                await db.flush()
            if caption_row is not None and caption_delivery_result is not None:
                caption_row.message_meta = _merge_delivery_into_meta(
                    caption_row.message_meta,
                    caption_delivery_result,
                )
        await db.commit()
        await db.refresh(final_receipt)
        events.append(
            {
                "type": "tool_call",
                "id": str(final_receipt.id),
                "message_id": str(final_receipt.id),
                "name": "send_media",
                "call_id": intent_id,
                "args": persisted_args,
                "status": "done",
                "result": json.dumps(result_payload, ensure_ascii=False),
                "created_at": final_receipt.created_at.isoformat() if final_receipt.created_at else None,
            }
        )
        if sent:
            from app.services.chat_message_serializer import serialize_chat_message_for_client
            if caption_sent and caption_row is not None:
                await db.refresh(caption_row)
                caption_event = serialize_chat_message_for_client(caption_row, source_channel=channel)
                caption_event["type"] = "assistant_message_committed"
                events.append(caption_event)

    try:
        from app.api.websocket import manager as ws_manager

        for event in events:
            event["session_id"] = str(target_session_id)
            await ws_manager.send_to_session(str(agent_id), str(target_session_id), event)
    except Exception:
        logger.opt(exception=True).warning("[SessionMedia] Web live mirror failed")

    return json.dumps(result_payload, ensure_ascii=False)
__all__ = [
    "_send_media_to_session",
    "_send_media_to_session_under_lifecycle_lock",
]
