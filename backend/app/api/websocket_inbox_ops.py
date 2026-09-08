"""WebSocket admission adapter for the shared durable turn inbox."""

import copy

from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.services.chat_attachments import validate_client_attachments
from app.services.llm.failure_outcome import render_message
from app.services.turn_inbox import schedule_durable_turn_resume


async def receive_turn_message(websocket):
    try:
        return await websocket.receive_json()
    except RuntimeError:
        # A failed send can close the application side before receive observes
        # websocket.disconnect. Both represent transport loss, not turn failure.
        if WebSocketState.DISCONNECTED in (websocket.application_state, websocket.client_state):
            raise WebSocketDisconnect(code=1006) from None
        raise


async def publish_inbox_receipt(api, handler, result, snapshot):
    row = result.message
    meta = dict(row.message_meta or {})
    await api.publish_conversation_turn_event(
        agent_id=handler.agent_id,
        conversation_id=handler.conv_id,
        payload={
            "type": "user_message_committed",
            "client_message_id": handler.current_client_message_id,
            "message_id": str(row.id), "id": str(row.id), "role": "user",
            "content": row.content,
            "display_content": meta.get("display_content", ""),
            "attachments": meta.get("attachments", []),
            "sender_user_id": str(handler.user_id),
        },
        snapshot=snapshot,
        event_kind="turn_user_committed",
    )
    await handler._safe_send(api.with_turn_envelope({
        "type": "turn_receipt", "status": "accepted",
        "message_id": str(row.id),
        "client_message_id": handler.current_client_message_id,
        "inbox_state": meta.get("turn_inbox_state"),
    }, snapshot, event_kind="turn_receipt"))


async def receive_followup(api, active_handler, data):
    """Persist each received frame without mutating the running turn's context."""
    handler = copy.copy(active_handler)
    handler.pending_initial_assistant = None
    handler.current_client_message_id = str(data.get("message_id") or data.get("client_message_id") or "") or None
    try:
        if handler.read_only or data.get("kind") == "onboarding_trigger":
            return
        if getattr(handler, "project_session_access", None) is not None:
            if not await handler._project_session_still_writable():
                raise PermissionError("Session is no longer writable")
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            return
        if content.strip().lower() == "/continue":
            from app.api.websocket_continue import handle_continue

            await handle_continue(handler, data)
            return
        attachments = None
        if "attachments" in data:
            attachments = await validate_client_attachments(handler.agent_id, data["attachments"])
        if not await handler._check_quotas():
            return
        model = handler.llm_model
        _, _, _, confirmation, _, snapshot = await handler._save_user_message(
            content, str(data.get("display_content") or ""), str(data.get("file_name") or ""), False,
            client_message_id=handler.current_client_message_id,
            model_id=str(model.id) if model else None,
            reasoning_effort=getattr(model, "reasoning_effort", None),
            attachments=attachments,
        )
        if confirmation is not None:
            await handler._send_current_turn_event({
                "type": "confirmation_required",
                "content": render_message("chat.confirmationRequired", handler.lang),
                "message_id": handler.current_client_message_id,
                "name": "request_confirmation", "call_id": str(confirmation.row_id),
                "args": confirmation.args, "status": "running",
            })
            return
        result = handler.last_ingest_result
        await publish_inbox_receipt(api, handler, result, snapshot)
        # A STOP/terminal race can leave this frame as the next admitted root.
        # The existing durable resume path owns it, including reconnect recovery.
        if result.created and not result.consumed_by_onmessage:
            schedule_durable_turn_resume(result.message)
    except Exception:
        api.logger.exception("[WS] Follow-up admission failed")
        await handler._send_current_turn_event({
            "type": "error", "code": "message_rejected", "retryable": True,
            "content": render_message("chat.messageRejected", handler.lang),
        })
