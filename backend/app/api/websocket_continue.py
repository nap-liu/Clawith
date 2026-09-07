"""Web adapter for the shared explicit turn continuation command."""

from app.database import async_session
from app.models.audit import ChatMessage
from app.services.turn_continue import dispatch_continue, prepare_continue


async def handle_continue(self, data):
    if not await self._check_quotas():
        return
    async with async_session() as db:
        result = await prepare_continue(
            db, agent_id=self.agent_id, session_id=self.conv_id,
            actor_user_id=self.user_id, locale=self.lang,
        )
        reply = ChatMessage(
            agent_id=self.agent_id, user_id=self.user_id,
            conversation_id=str(self.conv_id), role="assistant",
            content=result["message"], message_meta={
                "artifact_role": "command_reply", "command_action": result["action"],
                "turn_control_only": True,
            },
        )
        db.add(reply)
        await db.commit()
    try:
        await self._send_current_turn_event({
            "type": "done", "role": "assistant", "content": result["message"],
            "message_id": str(reply.id),
            **({"client_message_id": str(data.get("message_id") or data.get("client_message_id"))}
               if data.get("message_id") or data.get("client_message_id") else {}),
        }, event_kind="command_reply")
    finally:
        # A socket disappearing after acceptance must not abandon the claim.
        if result.get("_continue_anchor_id"):
            await dispatch_continue(result["_continue_anchor_id"])
