from project_actions_support import *  # noqa: F401,F403

async def test_project_group_reconcile_refreshes_preloaded_session_pointer(
    project_api: ProjectApiEnv,
):
    from app.models.chat_session import ChatSession
    from app.models.project import ProjectRun
    from app.services.conversation_turn_lifecycle import transition_conversation_turn
    from app.services.project_group_turn_lifecycle import reconcile_project_group_turn

    env = project_api
    project = await _create_project(env, name="Project pointer refresh")
    project_id = uuid.UUID(project["id"])
    await _mark_project_running(env, project_id)
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    first = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Generation A", "mentions": [], "attachments": []},
    )
    assert first.status_code == 201, first.text
    first_anchor_id = uuid.UUID(first.json()["turn"]["turn_anchor_id"])

    old_runs = list(
        (
            await env.db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project_id,
                    ProjectRun.input["group_session_id"].as_string() == group["id"],
                )
            )
        ).scalars()
    )
    for run in old_runs:
        run.status = "succeeded"
        run.finished_at = datetime.now(UTC)
    await env.db.commit()

    async with env.session_factory() as poll_db, env.session_factory() as writer_db:
        preloaded = await poll_db.get(ChatSession, uuid.UUID(group["id"]))
        assert preloaded is not None
        assert preloaded.im_config["conversation_turn"]["turn_anchor_id"] == str(first_anchor_id)

        second_anchor = ChatMessage(
            agent_id=env.leader_id,
            user_id=env.owner_id,
            sender_user_id=env.owner_id,
            role="user",
            content="Generation B",
            conversation_id=group["id"],
            message_meta={
                "kind": "project_group_message",
                "project_id": str(project_id),
                "visible_to_group": True,
            },
        )
        writer_db.add(second_anchor)
        await writer_db.flush()
        writer_db.add(
            ProjectRun(
                tenant_id=env.tenant_id,
                project_id=project_id,
                agent_id=env.reviewer_id,
                initiated_by_user_id=env.owner_id,
                execution_user_id=env.owner_id,
                status="queued",
                trigger_type="group_mention",
                input={
                    "group_session_id": group["id"],
                    "group_message_id": str(second_anchor.id),
                },
                output={"group_session_id": group["id"]},
            )
        )
        await writer_db.flush()
        await transition_conversation_turn(
            writer_db,
            agent_id=env.leader_id,
            conversation_id=group["id"],
            turn_anchor_id=first_anchor_id,
            status="completed",
        )
        await transition_conversation_turn(
            writer_db,
            agent_id=env.leader_id,
            conversation_id=group["id"],
            turn_anchor_id=second_anchor.id,
            status="running",
        )
        await writer_db.commit()

        projection = await reconcile_project_group_turn(
            poll_db,
            project_id=project_id,
            session=preloaded,
        )
        await poll_db.commit()

    assert projection.snapshot.phase == "active"
    assert projection.snapshot.generation == 2
    assert projection.snapshot.anchor_id == second_anchor.id
    assert projection.run_count == 1
    assert projection.active_agent_ids == (env.reviewer_id,)

async def test_project_group_dispatch_outbox_recovers_on_idempotent_replay(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.project import ProjectMemberSnapshot, ProjectRun
    from app.services import subagent_runtime

    env = project_api
    project = await _create_project(env, name="Recoverable project dispatch")
    project_id = project["id"]
    await _mark_project_running(env, project_id)
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    live_events: list[tuple[str, str, dict]] = []

    async def capture_live(agent_id, conversation_id, payload):
        live_events.append((str(agent_id), str(conversation_id), payload))

    monkeypatch.setattr(
        "app.api.websocket.manager.send_to_session",
        capture_live,
    )
    real_dispatch = subagent_runtime.dispatch_project_run

    async def simulated_process_exit(_run_id):
        raise RuntimeError("simulated exit after outbox commit")

    monkeypatch.setattr(subagent_runtime, "dispatch_project_run", simulated_process_exit)
    first = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "This message must survive a dispatcher exit",
            "mentions": [str(env.worker_id)],
            "client_message_id": "recoverable-group-message",
        },
    )
    assert first.status_code == 201, first.text
    assert first.json()["awakened_agent_ids"] == []
    pending = (
        (
            await env.db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == uuid.UUID(project_id),
                    ProjectRun.trigger_type.in_(["group_leader_message", "group_mention"]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(pending) == 2
    assert all(row.status == "queued" and row.input["dispatch"]["task"] for row in pending)
    pending_ids = [row.id for row in pending]
    worker_member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == uuid.UUID(project_id),
                ProjectMemberSnapshot.agent_id == env.worker_id,
            )
        )
    ).scalar_one()
    worker_member.is_enabled = False
    await env.db.commit()
    live_events.clear()

    monkeypatch.setattr(subagent_runtime, "dispatch_project_run", real_dispatch)
    for pending_id in pending_ids:
        await real_dispatch(pending_id)
    group_turn_events = [
        payload
        for _agent_id, conversation_id, payload in live_events
        if conversation_id == group["id"] and payload.get("type") == "turn_state"
    ]
    assert group_turn_events
    assert group_turn_events[-1]["turn"]["phase"] == "idle"
    env.db.expire_all()
    recovered = (await env.db.execute(select(ProjectRun).where(ProjectRun.id.in_(pending_ids)))).scalars().all()
    recovered_by_trigger = {row.trigger_type: row for row in recovered}
    assert recovered_by_trigger["group_mention"].status == "failed"
    assert recovered_by_trigger["group_leader_message"].status == "cancelled"
    assert recovered_by_trigger["group_leader_message"].output == {
        "group_session_id": group["id"],
        "status": "skipped",
        "skip_reason": "explicit_mentions_route_to_specialists",
    }

async def test_planning_group_message_only_wakes_owner_without_execution_tools(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.project import ProjectRun
    from app.services.subagent_runtime import prepare_subagent_tools

    env = project_api
    project = await _create_project(env, name="Human-controlled planning")
    project_id = project["id"]
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()

    async def inherited_tools(_agent_id, *, assignment_snapshot=None):
        return [
            {
                "type": "function",
                "function": {
                    "name": "inherited_write_tool",
                    "description": "Must not be available while planning",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    monkeypatch.setattr("app.services.agent_tools.get_agent_tools_for_llm", inherited_tools)
    response = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Help me turn this request into a reviewable delivery plan.",
            "mentions": [],
            "client_message_id": "planning-owner-only",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["awakened_agent_ids"] == [str(env.leader_id)]
    assert len(body["subagent_runs"]) == 1
    assert body["subagent_runs"][0]["agent_id"] == str(env.leader_id)

    project_run = await env.db.get(ProjectRun, uuid.UUID(body["subagent_runs"][0]["project_run_id"]))
    assert project_run is not None
    assert project_run.trigger_type == "group_leader_message"
    planning_task = project_run.input["dispatch"]["task"]
    assert "project is still in planning" in planning_task
    assert "Do not create or update work items" in planning_task
    assert "do not begin delivery" in planning_task

    child_session_id = uuid.UUID(body["subagent_runs"][0]["session_id"])
    assert (
        await prepare_subagent_tools(
            env.leader_id,
            child_session_id,
            execution_user_id=env.owner_id,
        )
        == []
    )

    mentioned = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Ask the specialist to start now.",
            "mentions": [str(env.worker_id)],
        },
    )
    assert mentioned.status_code == 422
    assert "project owner" in mentioned.json()["detail"]

    stored_project = await env.db.get(Project, uuid.UUID(project_id))
    assert stored_project is not None
    stored_project.status = "paused"
    await env.db.commit()
    paused = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Summarize the current paused state.", "mentions": []},
    )
    assert paused.status_code == 201, paused.text
    paused_body = paused.json()
    assert paused_body["awakened_agent_ids"] == [str(env.leader_id)]
    assert len(paused_body["subagent_runs"]) == 1
    paused_run = await env.db.get(
        ProjectRun,
        uuid.UUID(paused_body["subagent_runs"][0]["project_run_id"]),
    )
    assert paused_run is not None
    assert paused_run.agent_id == env.leader_id
    assert paused_run.input["dispatch"]["execution_tools_enabled"] is False
    assert paused_run.input["dispatch"]["read_only_conversation"] is True
    assert "project is paused" in paused_run.input["dispatch"]["task"]
    assert "Do not restart delivery" in paused_run.input["dispatch"]["task"]
    await env.db.refresh(stored_project)
    assert stored_project.status == "paused"
    paused_mention = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Wake the specialist.", "mentions": [str(env.worker_id)]},
    )
    assert paused_mention.status_code == 422

    # The boundary belongs to the queued message, not the project's later
    # status. Resuming must not retroactively add tools to this advisory turn.
    stored_project.status = "running"
    await env.db.commit()
    assert (
        await prepare_subagent_tools(
            env.leader_id,
            uuid.UUID(paused_body["subagent_runs"][0]["session_id"]),
            execution_user_id=env.owner_id,
        )
        == []
    )

    stored_project.status = "completed"
    await env.db.commit()
    completed = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Explain the completed result.", "mentions": []},
    )
    assert completed.status_code == 201, completed.text
    completed_body = completed.json()
    assert completed_body["awakened_agent_ids"] == [str(env.leader_id)]
    assert len(completed_body["subagent_runs"]) == 1
    completed_run = await env.db.get(
        ProjectRun,
        uuid.UUID(completed_body["subagent_runs"][0]["project_run_id"]),
    )
    assert completed_run is not None
    assert completed_run.agent_id == env.leader_id
    assert completed_run.input["dispatch"]["execution_tools_enabled"] is False
    assert completed_run.input["dispatch"]["read_only_conversation"] is True
    assert "project is completed" in completed_run.input["dispatch"]["task"]
    assert "Do not restart delivery" in completed_run.input["dispatch"]["task"]
    await env.db.refresh(stored_project)
    assert stored_project.status == "completed"
    completed_mention = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Wake the specialist.", "mentions": [str(env.worker_id)]},
    )
    assert completed_mention.status_code == 422

async def test_planning_leader_session_never_gets_project_runtime_tools(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.services.project_runtime_tools import (
        PROJECT_RUNTIME_TOOL_NAMES,
        execute_project_runtime_tool,
    )
    from app.services.subagent_runtime import prepare_subagent_tools

    env = project_api
    project = await _create_project(env, name="Planning scope isolation")
    project_id = project["id"]

    async def no_normal_tools(_agent_id, *, assignment_snapshot=None):
        return []

    monkeypatch.setattr("app.services.agent_tools.get_agent_tools_for_llm", no_normal_tools)
    response = await env.client.get(f"/api/projects/{project_id}/leader-session")
    assert response.status_code == 200, response.text
    assert response.json()["source_channel"] == "web"
    assert response.json()["read_only"] is True
    assert response.json()["planning_transport"] == "project_group"

    planning_session_id = uuid.UUID(response.json()["id"])
    planning_tools = {
        item["function"]["name"]
        for item in await prepare_subagent_tools(
            env.leader_id,
            planning_session_id,
            execution_user_id=env.owner_id,
        )
    }
    assert planning_tools.isdisjoint(PROJECT_RUNTIME_TOOL_NAMES)
    with pytest.raises(ValueError, match="authorized project Subagent runtime"):
        await execute_project_runtime_tool(
            "project_get_context",
            {},
            agent_id=env.leader_id,
            execution_user_id=env.owner_id,
            session_id=str(planning_session_id),
            tool_call_id="planning-session-scope-bypass",
            turn_anchor_id=None,
        )

async def test_owner_can_resume_waiting_project(project_api: ProjectApiEnv):
    from app.models.project import Project, ProjectEvent

    env = project_api
    project = await _create_project(env, name="Waiting project can resume")
    project_id = uuid.UUID(project["id"])
    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    stored_project.status = "waiting"
    await env.db.commit()

    resumed = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"status": "running"},
    )

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "running"
    event = await env.db.scalar(
        select(ProjectEvent)
        .where(
            ProjectEvent.project_id == project_id,
            ProjectEvent.event_type == "project.resumed",
        )
        .order_by(ProjectEvent.created_at.desc())
    )
    assert event is not None
    assert event.event_metadata["previous_status"] == "waiting"
