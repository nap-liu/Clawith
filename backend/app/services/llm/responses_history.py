"""Move completed Responses output into the existing message audit trail."""

from sqlalchemy import select

from app.models.audit import ChatMessage

SNAPSHOT_KEY = "responses_snapshot"
PENDING_KEY = "pending_responses_snapshot"


async def checkpoint_response(session_factory, *, agent_id, conversation_id, anchor_id, response):
    """Checkpoint a completed plain round until the normal terminal writer commits."""
    snapshot = getattr(response, SNAPSHOT_KEY, None)
    if not agent_id or not conversation_id or not anchor_id:
        return
    async with session_factory() as db:
        row = (await db.execute(select(ChatMessage).where(
            ChatMessage.id == anchor_id,
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
        ).with_for_update())).scalar_one_or_none()
        if row is None or (row.message_meta or {}).get("turn_status") != "running":
            return
        meta = dict(row.message_meta or {})
        if snapshot:
            meta[PENDING_KEY] = snapshot
        else:
            # A recovered turn may finish on Chat or a different protocol.
            # Its final answer supersedes an earlier, uncommitted Responses round.
            meta.pop(PENDING_KEY, None)
        row.message_meta = meta
        await db.commit()


def transfer_response_snapshot(anchor, final_meta, *, completed):
    """Consume the exact anchor checkpoint once, in the terminal transaction."""
    if anchor is None:
        return
    meta = dict(anchor.message_meta or {})
    snapshot = meta.pop(PENDING_KEY, None)
    if snapshot is not None:
        anchor.message_meta = meta
        if completed:
            final_meta[SNAPSHOT_KEY] = snapshot
