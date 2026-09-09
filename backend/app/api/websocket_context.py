"""Small WebSocket projection for optional host-reference admission errors."""

from app.services.external_chat_context import MessageConflict
from app.services.llm.failure_outcome import render_message


async def reject_context_message(handler, error):
    conflict = isinstance(error, MessageConflict)
    await handler._send_current_turn_event({
        "type": "error",
        "code": "message_conflict" if conflict else "invalid_external_context",
        "client_message_id": handler.current_client_message_id,
        "retryable": False,
        "content": render_message("chat.messageConflict" if conflict else "chat.invalidExternalContext", handler.lang),
    })
