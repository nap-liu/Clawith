from project_actions_support import *  # noqa: F401,F403

async def test_rest_a2a_requires_action_scope_and_ready_dependencies(project_api: ProjectApiEnv):
    from app.models.project import ProjectMemberSnapshot, ProjectRun

    env = project_api
    project = await _create_project(env, name="Actionable REST A2A")
    await _mark_project_running(env, project["id"])
    parent = await _create_work_item(
        env,
        project["id"],
        title="Produce reviewed input evidence",
        assignee_agent_id=env.worker_id,
    )
    child = await _create_work_item(
        env,
        project["id"],
        title="Make release decision",
        assignee_agent_id=env.reviewer_id,
        dependency_ids=[parent["id"]],
    )
    request = {
        "from_agent_id": str(env.worker_id),
        "to_agent_id": str(env.reviewer_id),
        "title": "Make release decision",
        "message": "Evaluate the reviewed input evidence against the release criteria.",
        "mode": "review",
        "expected_output": "A cited approve/reject decision with material risks",
        "work_item_id": child["id"],
    }

    passive = await env.client.post(
        f"/api/projects/{project['id']}/a2a",
        json={**request, "mode": "notify"},
    )
    assert passive.status_code == 422

    blocked = await env.client.post(f"/api/projects/{project['id']}/a2a", json=request)
    assert blocked.status_code == 409
    assert "该任务的前置任务尚未完成" in blocked.text
    assert "Produce reviewed input evidence" in blocked.text

    completed = await env.client.patch(
        f"/api/projects/{project['id']}/work-items/{parent['id']}",
        json={"status": "done"},
    )
    assert completed.status_code == 200, completed.text
    queued = await env.client.post(f"/api/projects/{project['id']}/a2a", json=request)
    assert queued.status_code == 202, queued.text

    run = await env.db.get(ProjectRun, uuid.UUID(queued.json()["run_id"]))
    assert run is not None
    assert run.work_item_id == uuid.UUID(child["id"])
    assert run.input["title"] == request["title"]
    assert run.input["expected_output"] == request["expected_output"]
    assert run.input["message"].endswith(f"Expected output: {request['expected_output']}")
    event = (
        await env.db.execute(
            select(ProjectEvent).where(
                ProjectEvent.run_id == run.id,
                ProjectEvent.event_type == "a2a.queued",
            )
        )
    ).scalar_one()
    assert event.summary == "Queued project collaboration action: Make release decision"
    assert event.event_metadata["expected_output"] == request["expected_output"]

async def test_member_departure_is_audited_revocation_and_restore_starts_a_fresh_child(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.api.websocket import WebSocketChatHandler
    from app.models.chat_session import ChatSession
    from app.models.project import ProjectEvent, ProjectRun
    from app.models.subagent_run import SubagentRun
    from app.services.project_runtime_tools import load_project_runtime_scope
    from app.services.project_service import project_session_access_mode
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    env = project_api
    project = await _create_project(env, name="Member lifecycle")
    project_id = uuid.UUID(project["id"])
    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    stored_project.status = "running"
    await env.db.commit()
    blocked_item = await _create_work_item(
        env,
        project_id,
        title="Blocked member action",
        assignee_agent_id=env.worker_id,
    )

    members = (await env.client.get(f"/api/projects/{project_id}/members")).json()
    leader = next(row for row in members if row["agent_id"] == str(env.leader_id))
    worker = next(row for row in members if row["agent_id"] == str(env.worker_id))

    leader_removal = await env.client.post(
        f"/api/projects/{project_id}/members/{leader['id']}/remove",
        json={"reason": "cannot remove current leader"},
    )
    assert leader_removal.status_code == 422

    first_run_response = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"agent_id": str(env.worker_id), "trigger_type": "manual", "input": {"objective": "First"}},
    )
    assert first_run_response.status_code == 201, first_run_response.text
    first_run_payload = first_run_response.json()
    first_child_id = uuid.UUID(first_run_payload["output"]["subagent_session_id"])
    first_project_run_id = uuid.UUID(first_run_payload["id"])
    first_child_session = await env.db.get(ChatSession, first_child_id)
    assert first_child_session is not None
    owner_user = await env.db.get(User, env.owner_id)
    assert owner_user is not None
    assert await project_session_access_mode(env.db, owner_user, first_child_session) == "edit"
    live_handler = WebSocketChatHandler.__new__(WebSocketChatHandler)
    live_handler.project_session_access = "edit"
    live_handler.conv_id = str(first_child_id)
    live_handler.user_id = env.owner_id
    live_handler.read_only = False
    assert await live_handler._project_session_still_writable() is True

    # Sending from the exact project child drawer is a durable inbox append,
    # not a second generic Web LLM turn.  Client retry is idempotent, keeps the
    # active ProjectRun association explicit and returns the persisted receipt.
    live_handler.agent_id = env.worker_id
    live_handler.source_channel = "subagent"
    receipts: list[dict] = []

    async def _capture_ws(_agent_id: str, conversation_id: str, payload: dict):
        if conversation_id == str(first_child_id):
            receipts.append(payload)

    monkeypatch.setattr("app.api.websocket.manager.send_to_session", _capture_ws)
    for _retry in range(2):
        assert (
            await live_handler._enqueue_project_subagent_message(
                content="Continue from the project drawer",
                display_content="Continue from the project drawer",
                file_name="evidence.txt",
                client_message_id="drawer-client-1",
                attachments=[{"type": "file", "name": "evidence.txt", "url": "/evidence.txt"}],
            )
            is True
        )

    env.db.expire_all()
    drawer_inputs = (
        (
            await env.db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(first_child_id),
                    ChatMessage.content == "Continue from the project drawer",
                )
            )
        )
        .scalars()
        .all()
    )
    durable_child = await env.db.get(SubagentRun, first_child_id)
    canonical_turn_anchor_id = await env.db.scalar(
        select(ChatMessage.id)
        .where(
            ChatMessage.conversation_id == str(first_child_id),
            ChatMessage.message_meta["kind"].as_string() == "subagent_input",
            ChatMessage.message_meta["subagent_input_state"]
            .as_string()
            .in_(["pending", "processing"]),
        )
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .limit(1)
    )
    assert len(drawer_inputs) == 1
    assert drawer_inputs[0].message_meta["kind"] == "subagent_input"
    assert drawer_inputs[0].message_meta["subagent_input_state"] == "pending"
    assert drawer_inputs[0].message_meta["project_run_id"] == str(first_project_run_id)
    assert drawer_inputs[0].message_meta["attachments"][0]["name"] == "evidence.txt"
    assert durable_child is not None and durable_child.status == "queued"
    assert durable_child.lease_owner is None
    assert durable_child.lease_expires_at is None
    committed = [row for row in receipts if row.get("type") == "user_message_committed"]
    assert len(committed) == 2
    assert {row["message_id"] for row in committed} == {str(drawer_inputs[0].id)}
    assert all(row["turn"]["phase"] == "active" for row in committed)
    # The receipt identifies the newly committed row separately, while its
    # lifecycle projection truthfully keeps the oldest queued input as the one
    # session owner. A later drawer append cannot jump the queue.
    assert {row["turn"]["turn_anchor_id"] for row in committed} == {
        str(canonical_turn_anchor_id)
    }

    removed = await env.client.post(
        f"/api/projects/{project_id}/members/{worker['id']}/remove",
        json={"reason": "staffing change"},
    )
    assert removed.status_code == 200, removed.text
    removed_payload = removed.json()
    assert removed_payload["id"] == worker["id"]
    assert removed_payload["is_enabled"] is False
    assert removed_payload["config_snapshot"]["membership"]["state"] == "departed"

    env.db.expire_all()
    first_child = await env.db.get(SubagentRun, first_child_id)
    first_project_run = await env.db.get(ProjectRun, first_project_run_id)
    first_child_session = await env.db.get(ChatSession, first_child_id)
    current_turn = await get_conversation_turn_snapshot(
        env.db,
        agent_id=env.worker_id,
        conversation_id=str(first_child_id),
    )
    assert first_child is not None and first_child.status == "cancelled"
    assert first_project_run is not None and first_project_run.status == "cancelled"
    assert first_child_session is not None
    assert first_child_session.im_config["membership_revoked"] is True
    assert current_turn.anchor_id == canonical_turn_anchor_id
    assert current_turn.status == "cancelled"
    assert current_turn.revision >= 2
    owner_user = await env.db.get(User, env.owner_id)
    assert owner_user is not None
    assert await project_session_access_mode(env.db, owner_user, first_child_session) == "read"
    assert await live_handler._project_session_still_writable() is False
    with pytest.raises(ValueError, match="active runtime member|permanently read-only"):
        await load_project_runtime_scope(
            env.db,
            session_id=first_child_id,
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
        )

    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    mention = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "@Worker continue", "mentions": [str(env.worker_id)]},
    )
    assert mention.status_code == 422
    assigned_run = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"agent_id": str(env.worker_id), "trigger_type": "manual", "input": {"objective": "Blocked"}},
    )
    assert assigned_run.status_code == 422
    a2a = await env.client.post(
        f"/api/projects/{project_id}/a2a",
        json={
            "from_agent_id": str(env.leader_id),
            "to_agent_id": str(env.worker_id),
            "title": "Complete blocked member action",
            "message": "Blocked",
            "mode": "delegate",
            "expected_output": "A completed deliverable with evidence",
            "work_item_id": blocked_item["id"],
        },
    )
    assert a2a.status_code == 422

    duplicate = await env.client.post(
        f"/api/projects/{project_id}/members",
        json={"agent_id": str(env.worker_id)},
    )
    assert duplicate.status_code == 409
    assert "restore" in duplicate.json()["detail"].lower()

    restored = await env.client.post(
        f"/api/projects/{project_id}/members/{worker['id']}/restore",
        json={"reason": "return to project"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] == worker["id"]
    assert restored.json()["is_enabled"] is True
    assert restored.json()["config_snapshot"]["membership"]["generation"] == 2

    env.db.expire_all()
    first_child_session = await env.db.get(ChatSession, first_child_id)
    assert first_child_session is not None
    owner_user = await env.db.get(User, env.owner_id)
    assert owner_user is not None
    assert await project_session_access_mode(env.db, owner_user, first_child_session) == "read"
    assert await live_handler._project_session_still_writable() is False

    second_run_response = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"agent_id": str(env.worker_id), "trigger_type": "manual", "input": {"objective": "Fresh"}},
    )
    assert second_run_response.status_code == 201, second_run_response.text
    second_child_id = uuid.UUID(second_run_response.json()["output"]["subagent_session_id"])
    assert second_child_id != first_child_id
    second_child_session = await env.db.get(ChatSession, second_child_id)
    assert second_child_session is not None
    assert second_child_session.im_config["project_membership_generation"] == 2
    owner_user = await env.db.get(User, env.owner_id)
    assert owner_user is not None
    assert await project_session_access_mode(env.db, owner_user, second_child_session) == "edit"

    events = (await env.db.execute(select(ProjectEvent).where(ProjectEvent.project_id == project_id))).scalars().all()
    lifecycle_events = [row for row in events if row.event_type in {"member.departed", "member.restored"}]
    assert [row.event_type for row in lifecycle_events] == ["member.departed", "member.restored"]
    assert lifecycle_events[0].event_metadata["snapshot_retained"] is True
    assert lifecycle_events[1].event_metadata["old_sessions_remain_read_only"] is True

async def test_only_project_owner_can_remove_and_restore_project_agent_members(
    project_api: ProjectApiEnv,
):
    from app.models.chat_session import ChatSession
    from app.services.project_service import project_session_access_mode

    env = project_api
    project = await _create_project(env, name="Editor membership controls")
    project_id = project["id"]
    stored_project = await env.db.get(Project, uuid.UUID(project_id))
    assert stored_project is not None
    stored_project.status = "running"
    await env.db.commit()
    members = (await env.client.get(f"/api/projects/{project_id}/members")).json()
    reviewer = next(row for row in members if row["agent_id"] == str(env.reviewer_id))
    reviewer_run = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"agent_id": str(env.reviewer_id), "trigger_type": "manual", "input": {"objective": "Review"}},
    )
    assert reviewer_run.status_code == 201, reviewer_run.text
    reviewer_child_id = uuid.UUID(reviewer_run.json()["output"]["subagent_session_id"])
    reviewer_session = await env.db.get(ChatSession, reviewer_child_id)
    assert reviewer_session is not None

    grant = await env.client.post(
        f"/api/projects/{project_id}/access-grants",
        json={"user_id": str(env.viewer_id), "role": "view"},
    )
    assert grant.status_code == 201, grant.text
    viewer_user = await env.db.get(User, env.viewer_id)
    assert viewer_user is not None
    assert await project_session_access_mode(env.db, viewer_user, reviewer_session) == "read"
    env.authenticate_as(env.viewer_id)
    forbidden = await env.client.post(
        f"/api/projects/{project_id}/members/{reviewer['id']}/remove",
        json={"reason": "viewer cannot"},
    )
    assert forbidden.status_code == 404

    env.authenticate_as(env.owner_id)
    grant_id = grant.json()["id"]
    grant_row = await env.db.get(ProjectAccessGrant, uuid.UUID(grant_id))
    assert grant_row is not None
    grant_row.role = "edit"
    await env.db.commit()
    viewer_user = await env.db.get(User, env.viewer_id)
    reviewer_session = await env.db.get(ChatSession, reviewer_child_id)
    assert viewer_user is not None and reviewer_session is not None
    assert await project_session_access_mode(env.db, viewer_user, reviewer_session) == "edit"
    env.authenticate_as(env.viewer_id)
    removed = await env.client.post(
        f"/api/projects/{project_id}/members/{reviewer['id']}/remove",
        json={"reason": "editor staffing"},
    )
    assert removed.status_code == 404

    env.authenticate_as(env.owner_id)
    removed = await env.client.post(
        f"/api/projects/{project_id}/members/{reviewer['id']}/remove",
        json={"reason": "owner staffing"},
    )
    assert removed.status_code == 200, removed.text
    viewer_user = await env.db.get(User, env.viewer_id)
    reviewer_session = await env.db.get(ChatSession, reviewer_child_id)
    assert viewer_user is not None and reviewer_session is not None
    assert await project_session_access_mode(env.db, viewer_user, reviewer_session) == "read"
    restored = await env.client.post(
        f"/api/projects/{project_id}/members/{reviewer['id']}/restore",
        json={"reason": "editor restore"},
    )
    assert restored.status_code == 200, restored.text
    viewer_user = await env.db.get(User, env.viewer_id)
    reviewer_session = await env.db.get(ChatSession, reviewer_child_id)
    assert viewer_user is not None and reviewer_session is not None
    assert await project_session_access_mode(env.db, viewer_user, reviewer_session) == "read"
