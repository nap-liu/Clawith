"""Native Gateway admission uses the ordinary durable A2A Turn."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.participant import Participant
from app.services.chat_history import ingest_incoming_chat_message

PAIR_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


async def admit_native_gateway_turn(
    db, source_agent_id, source_agent_name, target_agent_id,
    target_agent_name, execution_user_id, content, source_event_id,
):
    """Persist the input before acceptance; the caller commits its receipt too."""
    source_id = uuid.UUID(str(source_agent_id))
    target_id = uuid.UUID(str(target_agent_id))
    owner_id = uuid.UUID(str(execution_user_id))
    pair = sorted((source_id, target_id), key=str)
    tenant_rows = list((await db.execute(
        select(Agent.id, Agent.tenant_id).where(Agent.id.in_(pair))
    )).all())
    if len(tenant_rows) != 2 or len({row.tenant_id for row in tenant_rows}) != 1:
        raise PermissionError("Gateway A2A tenant identity mismatch")
    session_id = uuid.uuid5(PAIR_NAMESPACE, f"{pair[0]}_{pair[1]}")
    await db.execute(
        pg_insert(ChatSession).values(
            id=session_id, agent_id=pair[0], peer_agent_id=pair[1],
            source_channel="agent", title=f"{source_agent_name} ↔ {target_agent_name}",
            created_at=datetime.now(timezone.utc),
        ).on_conflict_do_nothing(index_elements=["id"])
    )
    session = await db.get(ChatSession, session_id, with_for_update=True)
    if (session.source_channel != "agent"
            or {session.agent_id, session.peer_agent_id} != set(pair)):
        raise PermissionError("Gateway A2A session identity mismatch")
    participant = await db.scalar(select(Participant).where(
        Participant.type == "agent", Participant.ref_id == source_id,
    ))
    prompt = (
        "--- Agent-to-Agent Communication Alert ---\n"
        f"You are receiving a direct message from another digital employee ({source_agent_name}). "
        "CRITICAL INSTRUCTION: Your direct text reply will automatically be delivered back to them. "
        "DO NOT use the `send_message_to_agent` tool to reply to this conversation. Just reply naturally in text.\n"
        "If they are asking you to create or analyze a file, deliver the file using `send_file_to_agent` after writing it."
        f"\n\n[Message from agent: {source_agent_name}]\n{content}"
    )
    reply_id = uuid.uuid5(PAIR_NAMESPACE, f"gateway-direct-reply:{source_event_id}")
    ingested = await ingest_incoming_chat_message(
        db, session=session, agent_id=session.agent_id, user_id=owner_id,
        content=prompt, source_channel="agent", provider_event_id=source_event_id,
        channel_config_id="gateway-direct",
        actor_ref=str(participant.id if participant else source_id),
        participant_id=participant.id if participant else None,
        message_meta={
            "execution_agent_id": str(target_id),
            "gateway_direct_reply": {
                "message_id": str(reply_id), "agent_id": str(source_id),
                "sender_agent_id": str(target_id),
            },
        },
    )
    ingested.message.sender_user_id = None
    ingested.message.sender_agent_id = source_id
    session.last_message_at = datetime.now(timezone.utc)
    await db.flush()
    return ingested.message
