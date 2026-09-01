from __future__ import annotations

import json
import mimetypes
from pathlib import Path
import uuid

from app.services.agent_tools_channel_file_receipts import (
    _claim_channel_file_receipt,
    _normalize_tool_workspace_rel_path,
    _platform_file_delivery_result,
    _supports_exact_file_session_route,
    append_delivery_part,
    channel_file_part_recorder,
    channel_file_sender,
    register_delivery,
)
from app.services.agent_tools_file_delivery_runtime import _send_file_to_recipient, _send_file_to_session
from app.services.agent_tools_file_support import _agent_workspace_root
from app.services.agent_tools_media_delivery_core import _sniff_media_file_kind
from app.services.chat_attachments import infer_attachment_kind
from app.services.im_delivery import IMDeliveryPart, IMDeliveryResult
from app.services.recipient_resolver import RecipientResolutionError
from app.services.user_output import sanitize_user_visible_text

async def _send_channel_file(
    agent_id: uuid.UUID,
    ws: Path,
    arguments: dict,
    *,
    tool_call_id: str | None = None,
    origin_session_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send a file to a person or back to the current channel.

    Priority:
    1. If session_id is provided, use that exact existing person/group route.
    2. If canonical user_id is provided, resolve one exact internal route.
    3. If the current Session has an exact external file route, use that route.
    4. If a legacy non-DingTalk channel sender is set, use it directly.
    5. Fall back to a structured web/H5 result when no explicit target is requested.
    """
    raw_rel_path = arguments.get("file_path", "")
    rel_path = raw_rel_path.strip() if isinstance(raw_rel_path, str) else ""
    accompany_msg = sanitize_user_visible_text(str(arguments.get("message") or ""))
    canonical_user_id = str(arguments.get("user_id") or "").strip()
    requested_session_id = str(arguments.get("session_id") or "").strip()
    channel = str(arguments.get("channel") or "").strip().lower() or None
    if canonical_user_id and requested_session_id:
        return RecipientResolutionError(
            "ambiguous_file_target",
            "Cannot provide both session_id and user_id",
        ).as_json()
    if channel and not canonical_user_id:
        return RecipientResolutionError(
            "channel_requires_user_target",
            "channel can only be used with user_id",
        ).as_json()
    if not rel_path:
        return "Error: file_path is required"
    rel_path = _normalize_tool_workspace_rel_path(rel_path)
    if not rel_path:
        return "Error: Invalid file_path"

    # Resolve file path within agent workspace
    file_path = (ws / rel_path).resolve()
    ws_resolved = ws.resolve()
    try:
        file_path.relative_to(ws_resolved)
    except ValueError:
        file_path = (_agent_workspace_root(agent_id) / rel_path).resolve()
        if not file_path.exists():
            return f"Error: File not found: {rel_path}"
    if not file_path.exists():
        return f"Error: File not found: {rel_path}"

    detected_kind = _sniff_media_file_kind(file_path) or infer_attachment_kind(
        file_path.name, mimetypes.guess_type(file_path.name)[0]
    )
    if detected_kind in {"audio", "video"}:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "WRONG_MEDIA_TOOL",
                "media_kind": detected_kind,
                "message": f"Use send_media with media_type='{detected_kind}'.",
            },
            ensure_ascii=False,
        )

    async def _deliver_to_exact_session(target_session_id: str) -> str:
        receipt_id = await _claim_channel_file_receipt(
            agent_id=agent_id,
            tool_call_id=str(tool_call_id or ""),
            origin_session_id=str(origin_session_id or ""),
            origin_turn_anchor_id=origin_turn_anchor_id,
        )
        if receipt_id is None:
            return "Failed to send file: durable delivery receipt unavailable"

        async def _record_part(part: IMDeliveryPart) -> None:
            await append_delivery_part(receipt_id, part)

        recorder_token = channel_file_part_recorder.set(_record_part)
        try:
            delivery_text, delivery_result = await _send_file_to_session(
                agent_id,
                file_path,
                target_session_id,
                accompany_msg,
            )
            if not await register_delivery(receipt_id, delivery_result):
                raise RuntimeError("delivery finalization failed")
            return delivery_text
        except Exception as exc:
            await register_delivery(
                receipt_id,
                IMDeliveryResult.from_exception("im", exc),
            )
            safe_error = sanitize_user_visible_text(str(exc)).strip()
            return f"Failed to send file: {safe_error or type(exc).__name__}"
        finally:
            channel_file_part_recorder.reset(recorder_token)

    # Priority 1: exact existing Session (person or group).
    if requested_session_id:
        return await _deliver_to_exact_session(requested_session_id)

    # Priority 2: explicit canonical recipient.
    if canonical_user_id:
        receipt_id = await _claim_channel_file_receipt(
            agent_id=agent_id,
            tool_call_id=str(tool_call_id or ""),
            origin_session_id=str(origin_session_id or ""),
            origin_turn_anchor_id=origin_turn_anchor_id,
        )
        if receipt_id is None:
            return "Failed to send file: durable delivery receipt unavailable"
        async def _record_part(part: IMDeliveryPart) -> None:
            await append_delivery_part(receipt_id, part)

        recorder_token = channel_file_part_recorder.set(_record_part)
        try:
            delivery_text, delivery_result = await _send_file_to_recipient(
                agent_id,
                file_path,
                canonical_user_id,
                accompany_msg,
                channel=channel,
            )
            if not await register_delivery(receipt_id, delivery_result):
                raise RuntimeError("delivery finalization failed")
            return delivery_text
        except Exception as exc:
            await register_delivery(
                receipt_id,
                IMDeliveryResult.from_exception("im", exc),
            )
            safe_error = sanitize_user_visible_text(str(exc)).strip()
            return f"Failed to send file: {safe_error or type(exc).__name__}"
        finally:
            channel_file_part_recorder.reset(recorder_token)

    # Priority 3: current durable external Session. DingTalk uses the same
    # proactive route here as explicit cross-session delivery.
    if origin_session_id and await _supports_exact_file_session_route(
        agent_id,
        origin_session_id,
    ):
        return await _deliver_to_exact_session(origin_session_id)

    # Priority 4: legacy channel-initiated sender for transports that have not
    # yet migrated to exact Session routing. DingTalk no longer registers one.
    sender = channel_file_sender.get()
    if sender is not None:
        receipt_id = await _claim_channel_file_receipt(
            agent_id=agent_id,
            tool_call_id=str(tool_call_id or ""),
            origin_session_id=str(origin_session_id or ""),
            origin_turn_anchor_id=origin_turn_anchor_id,
        )
        if receipt_id is None:
            return "Failed to send file: durable delivery receipt unavailable"
        async def _record_part(part: IMDeliveryPart) -> None:
            await append_delivery_part(receipt_id, part)

        recorder_token = channel_file_part_recorder.set(_record_part)
        try:
            delivery_result = await sender(file_path, accompany_msg)
            if not isinstance(delivery_result, IMDeliveryResult):
                delivery_result = IMDeliveryResult.unsupported_delivery(
                    "im",
                    "legacy_channel_file",
                )
            if not await register_delivery(receipt_id, delivery_result):
                raise RuntimeError("delivery finalization failed")
            if not delivery_result.ok:
                return f"Failed to send file: {delivery_result.error or delivery_result.status}"
            return f"File '{file_path.name}' sent to user via channel."
        except Exception as e:
            await register_delivery(
                receipt_id,
                IMDeliveryResult.from_exception("im", e),
            )
            safe_error = sanitize_user_visible_text(str(e)).strip()
            return f"Failed to send file: {safe_error or type(e).__name__}"
        finally:
            channel_file_part_recorder.reset(recorder_token)

    # Priority 5: Web/H5 chat fallback — return a structured platform file
    # delivery payload. The frontend builds the authenticated download URL.
    base_abs = _agent_workspace_root(agent_id).resolve()
    try:
        file_rel = file_path.resolve().relative_to(base_abs).as_posix()
    except ValueError:
        file_rel = rel_path
    return _platform_file_delivery_result(file_path, file_rel, accompany_msg)

__all__ = ["_send_channel_file"]
