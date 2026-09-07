import asyncio
import uuid
from datetime import UTC, datetime

from project_actions_support import (
    ProjectApiEnv,
    _create_project,
)
from project_actions_support import (
    project_api as project_api,  # noqa: PLC0414 -- pytest fixture re-export
)
from project_actions_support import (
    pytestmark as pytestmark,  # noqa: PLC0414 -- preserve module pytest marks
)
from sqlalchemy import select

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.project import ProjectMemberSnapshot
from app.models.subagent_run import SubagentRun
from app.services.a2a_file_delivery import (
    append_a2a_file_delivery_message,
    resolve_a2a_file_origin_scope,
)


async def test_concurrent_member_file_deliveries_keep_their_exact_parent_sessions(
    project_api: ProjectApiEnv,
):
    """Concurrent project members must not collapse onto another A2A thread."""
    env = project_api
    project = await _create_project(env, name="Concurrent exact file routing")
    project_id = uuid.UUID(project["id"])
    senders = [(env.worker_id, "Worker"), (env.reviewer_id, "Reviewer")]
    routes: list[tuple[uuid.UUID, str, ChatSession, ChatSession]] = []

    for index, (sender_id, sender_name) in enumerate(senders):
        member = (
            await env.db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project_id,
                    ProjectMemberSnapshot.agent_id == sender_id,
                )
            )
        ).scalar_one()
        access_agent_id = min(sender_id, env.leader_id, key=str)
        peer_agent_id = max(sender_id, env.leader_id, key=str)
        decoy = ChatSession(
            project_id=project_id,
            agent_id=access_agent_id,
            peer_agent_id=peer_agent_id,
            source_channel="agent",
            title=f"{sender_name} old thread",
            external_conv_id=f"a2a-decoy-{index}",
        )
        parent = ChatSession(
            project_id=project_id,
            agent_id=access_agent_id,
            peer_agent_id=peer_agent_id,
            source_channel="agent",
            title=f"{sender_name} current thread",
            external_conv_id=f"a2a-current-{index}",
        )
        child = ChatSession(
            project_id=project_id,
            agent_id=sender_id,
            source_channel="subagent",
            title=f"{sender_name} project child",
            external_conv_id=f"subagent-file-{index}",
        )
        env.db.add_all([decoy, parent, child])
        await env.db.flush()
        env.db.add(
            SubagentRun(
                id=child.id,
                parent_session_id=parent.id,
                project_id=project_id,
                project_member_id=member.id,
                execution_user_id=env.owner_id,
                origin_tool_call_id=f"file-parent-{index}",
                mode="async",
                status="running",
            )
        )
        routes.append((sender_id, sender_name, parent, child))
    await env.db.commit()

    async def deliver(index: int, sender_id: uuid.UUID, sender_name: str, child: ChatSession) -> uuid.UUID:
        async with env.session_factory() as db:
            scope = await resolve_a2a_file_origin_scope(
                db,
                origin_session_id=child.id,
                sender_agent_id=sender_id,
            )
            session_id = await append_a2a_file_delivery_message(
                db,
                sender_agent_id=sender_id,
                target_agent_id=env.leader_id,
                sender_creator_id=env.owner_id,
                sender_name=sender_name,
                target_name="Leader",
                project_id=scope.project_id,
                preferred_session_id=scope.preferred_session_id,
                source_path=f"workspace/report-{index}.md",
                delivered_path=f"workspace/inbox/files/report-{index}.md",
                delivered_name=f"report-{index}.md",
                delivery_note="Review this exact project artifact",
                file_size=128 + index,
                created_at=datetime.now(UTC),
                external_event_key=f"test-project-file-{project_id}-{index}",
                origin_session_id=str(child.id),
                tool_call_id=f"send-file-{index}",
            )
            await db.commit()
            return session_id

    delivered_session_ids = await asyncio.gather(
        *(deliver(index, sender_id, sender_name, child) for index, (sender_id, sender_name, _parent, child) in enumerate(routes))
    )
    assert delivered_session_ids == [route[2].id for route in routes]

    messages = (
        (
            await env.db.execute(
                select(ChatMessage).where(
                    ChatMessage.external_event_key.in_(
                        [f"test-project-file-{project_id}-0", f"test-project-file-{project_id}-1"]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    assert {uuid.UUID(row.conversation_id) for row in messages} == {route[2].id for route in routes}
    assert {row.sender_agent_id for row in messages} == {env.worker_id, env.reviewer_id}
    assert all(len(row.message_meta["attachments"]) == 1 for row in messages)
