from project_actions_support import *  # noqa: F401,F403

async def test_project_group_routes_human_to_leader_and_reuses_durable_children(
    project_api: ProjectApiEnv,
):
    from app.models.chat_session import ChatSession
    from app.models.project import ProjectRun

    env = project_api
    project = await _create_project(env, name="Project Agent Group")
    project_id = project["id"]
    await _mark_project_running(env, project_id)

    group_response = await env.client.get(f"/api/projects/{project_id}/group-session")
    assert group_response.status_code == 200, group_response.text
    group = group_response.json()
    assert group["source_channel"] == "project"
    assert group["max_mentions"] == 3

    passive = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Visible update only",
            "mentions": [],
            "attachments": [],
            "client_message_id": "passive-confirmation-anchor",
        },
    )
    assert passive.status_code == 201, passive.text
    passive_body = passive.json()
    assert passive_body["awakened_agent_ids"] == [str(env.leader_id)]
    assert passive_body["default_leader_agent_id"] == str(env.leader_id)
    assert len(passive_body["subagent_runs"]) == 1
    assert passive_body["subagent_runs"][0]["agent_id"] == str(env.leader_id)
    assert passive_body["message"]["message_meta"]["visible_to_group"] is True
    assert passive_body["message"]["message_meta"]["wake_policy"] == "default_leader_plus_structured_mentions"
    assert passive_body["turn"]["phase"] == "active"
    assert passive_body["turn"]["generation"] == 1
    cohort_anchor_id = passive_body["turn"]["turn_anchor_id"]
    passive_run = await env.db.get(
        ProjectRun,
        uuid.UUID(passive_body["subagent_runs"][0]["project_run_id"]),
    )
    assert passive_run is not None
    passive_run.status = "waiting"
    passive_child_session_id = passive_body["subagent_runs"][0]["session_id"]
    pending_tool = ChatMessage(
        agent_id=env.leader_id,
        user_id=env.owner_id,
        role="tool_call",
        content=json.dumps(
            {
                "name": "request_confirmation",
                "args": {"force_confirmation": True},
                "status": "pending",
            }
        ),
        conversation_id=passive_child_session_id,
    )
    env.db.add(pending_tool)
    await env.db.commit()
    suspended = await env.client.get(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages"
    )
    assert suspended.status_code == 200
    assert suspended.json()["turn"]["phase"] == "suspended"
    assert suspended.json()["turn"]["generation"] == 1
    replay = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Visible update only",
            "mentions": [],
            "attachments": [],
            "client_message_id": "passive-confirmation-anchor",
        },
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["idempotent_replay"] is True
    blocked = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Must wait for confirmation",
            "mentions": [],
            "attachments": [],
            "client_message_id": "blocked-while-confirmation-pending",
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "project_group_confirmation_pending"
    assert blocked.json()["detail"]["client_message_id"] == "blocked-while-confirmation-pending"
    passive_run.status = "queued"
    pending_tool.content = json.dumps(
        {
            "name": "request_confirmation",
            "args": {"force_confirmation": True},
            "status": "done",
            "result": "confirmed for lifecycle test",
        }
    )
    await env.db.commit()

    mentioned = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Worker build and Reviewer check",
            "llm_content": "[brief.md extracted]\nBuild the evidence and review every acceptance item.",
            "mentions": [str(env.worker_id), str(env.reviewer_id), str(env.worker_id)],
            "attachments": [{"name": "brief.md", "path": "brief.md"}],
            "client_message_id": "mention-1",
        },
    )
    assert mentioned.status_code == 201, mentioned.text
    body = mentioned.json()
    assert body["awakened_agent_ids"] == [
        str(env.worker_id),
        str(env.reviewer_id),
    ]
    assert body["message"]["message_meta"]["wake_policy"] == "structured_mentions_only"
    assert body["message"]["message_meta"]["owner_deferred_until_specialist_result"] is True
    assert len(body["subagent_runs"]) == 3
    assert all(row["project_run_id"] for row in body["subagent_runs"])
    assert body["message"]["display_content"] == "Worker build and Reviewer check"
    assert body["turn"]["phase"] == "active"
    assert body["turn"]["generation"] == 1
    assert body["turn"]["turn_anchor_id"] == cohort_anchor_id
    worker_child = next(row for row in body["subagent_runs"] if row["agent_id"] == str(env.worker_id))
    worker_input = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == worker_child["session_id"],
                ChatMessage.message_meta["project_run_id"].as_string() == worker_child["project_run_id"],
            )
        )
    ).scalar_one()
    assert worker_input.content.startswith("[brief.md extracted]")
    worker_session = await env.db.get(ChatSession, uuid.UUID(worker_child["session_id"]))
    assert worker_session is not None
    assert worker_session.im_config["project_name_snapshot"] == "Project Agent Group"
    assert worker_session.im_config["project_member_name_snapshot"] == "Worker"
    assert worker_session.im_config["project_member_role_snapshot"] == "Build deliverables"
    assert worker_session.im_config["project_role_snapshot"] == "participant"

    replay = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Worker build and Reviewer check",
            "mentions": [str(env.worker_id), str(env.reviewer_id)],
            "client_message_id": "mention-1",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["subagent_runs"] == body["subagent_runs"]

    reused = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Worker follow-up", "mentions": [str(env.worker_id)]},
    )
    assert reused.status_code == 201, reused.text
    reused_worker = next(row for row in reused.json()["subagent_runs"] if row["agent_id"] == str(env.worker_id))
    assert reused_worker["session_id"] == worker_child["session_id"]
    assert reused_worker["project_run_id"] != worker_child["project_run_id"]

    leader_deduped = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Leader and Worker only once",
            "mentions": [str(env.leader_id), str(env.worker_id), str(env.leader_id)],
        },
    )
    assert leader_deduped.status_code == 201, leader_deduped.text
    assert leader_deduped.json()["awakened_agent_ids"] == [
        str(env.leader_id),
        str(env.worker_id),
    ]

    spoofed_agent = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Agent-visible update only",
            "sender_agent_id": str(env.worker_id),
            "mentions": [],
        },
    )
    assert spoofed_agent.status_code == 422

    stored_project = await env.db.get(Project, uuid.UUID(project_id))
    assert stored_project is not None
    stored_project.settings = {
        **dict(stored_project.settings or {}),
        "policies": {
            **dict((stored_project.settings or {}).get("policies") or {}),
            "max_a2a_wakes": 12,
        },
    }
    await env.db.commit()
    expanded_group = await env.client.get(f"/api/projects/{project_id}/group-session")
    assert expanded_group.json()["max_mentions"] == 4
    stored_project.settings = {
        **dict(stored_project.settings or {}),
        "policies": {
            **dict((stored_project.settings or {}).get("policies") or {}),
            "max_a2a_wakes": 2,
        },
    }
    await env.db.commit()
    limited_group = await env.client.get(f"/api/projects/{project_id}/group-session")
    assert limited_group.json()["max_mentions"] == 1
    over_budget = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Leader plus two participants exceeds total budget",
            "mentions": [str(env.worker_id), str(env.reviewer_id)],
        },
    )
    assert over_budget.status_code == 422

    self_mention = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "No self wake",
            "sender_agent_id": str(env.worker_id),
            "mentions": [str(env.worker_id)],
        },
    )
    assert self_mention.status_code == 422

    history = await env.client.get(f"/api/projects/{project_id}/group-sessions/{group['id']}/messages?limit=500")
    assert history.status_code == 200
    assert len(history.json()["items"]) == 4
    assert history.json()["turn"]["turn_anchor_id"] == cohort_anchor_id
    assert history.json()["turn"]["phase"] == "active"
    attachment_message = next(item for item in history.json()["items"] if item["attachments"])
    assert attachment_message["attachments"][0]["name"] == "brief.md"
    cohort_runs = list(
        (
            await env.db.execute(
                select(ProjectRun).where(
                    ProjectRun.input["group_session_id"].as_string() == str(group["id"])
                )
            )
        ).scalars()
    )
    for cohort_run in cohort_runs:
        cohort_run.status = "succeeded"
        cohort_run.finished_at = datetime.now(UTC)
    await env.db.commit()
    completed_history = await env.client.get(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages?limit=500"
    )
    assert completed_history.status_code == 200
    assert completed_history.json()["turn"]["phase"] == "idle"
    assert completed_history.json()["turn"]["status"] == "completed"
    assert completed_history.json()["turn"]["generation"] == 1

async def test_project_group_partial_completion_versions_cohort_projection(
    project_api: ProjectApiEnv,
):
    from app.models.project import ProjectRun

    env = project_api
    project = await _create_project(env, name="Versioned group cohort")
    project_id = project["id"]
    await _mark_project_running(env, project_id)
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    created = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Run two specialists",
            "mentions": [str(env.worker_id), str(env.reviewer_id)],
            "client_message_id": "versioned-cohort-1",
        },
    )
    assert created.status_code == 201, created.text
    initial = created.json()["turn"]
    assert initial["phase"] == "active"
    assert len(initial["active_agent_ids"]) >= 2

    runs = list(
        (
            await env.db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == uuid.UUID(project_id),
                    ProjectRun.trigger_type.in_(["group_leader_message", "group_mention"]),
                    ProjectRun.input["group_session_id"].as_string() == group["id"],
                )
            )
        ).scalars()
    )
    first = next(run for run in runs if run.agent_id == env.worker_id)
    run_ids = [run.id for run in runs]
    first.status = "failed"
    first.finished_at = datetime.now(UTC)
    await env.db.commit()
    partial = (
        await env.client.get(
            f"/api/projects/{project_id}/group-sessions/{group['id']}/messages"
        )
    ).json()["turn"]
    assert partial["phase"] == "active"
    assert partial["revision"] > initial["revision"]
    assert str(env.worker_id) not in partial["active_agent_ids"]

    env.db.expire_all()
    remaining = list(
        (
            await env.db.execute(
                select(ProjectRun).where(
                    ProjectRun.id.in_(run_ids),
                    ProjectRun.status.not_in(["succeeded", "failed", "cancelled"]),
                )
            )
        ).scalars()
    )
    for run in remaining:
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
    await env.db.commit()
    terminal = (
        await env.client.get(
            f"/api/projects/{project_id}/group-sessions/{group['id']}/messages"
        )
    ).json()["turn"]
    assert terminal["phase"] == "idle"
    assert terminal["revision"] > partial["revision"]
    assert terminal["active_agent_ids"] == []

async def test_project_group_history_keeps_terminal_turn_after_late_owner_delivery(
    project_api: ProjectApiEnv,
):
    from app.models.project import ProjectRun

    env = project_api
    project = await _create_project(env, name="Late owner delivery")
    project_id = uuid.UUID(project["id"])
    await _mark_project_running(env, str(project_id))
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    created = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Complete before deferred owner delivery", "mentions": []},
    )
    assert created.status_code == 201, created.text
    anchor_id = uuid.UUID(created.json()["message"]["id"])

    runs = list(
        (
            await env.db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project_id,
                    ProjectRun.input["group_message_id"].as_string() == str(anchor_id),
                )
            )
        ).scalars()
    )
    for run in runs:
        run.status = "succeeded"
        run.finished_at = datetime.now(UTC)
    await env.db.commit()
    terminal = await env.client.get(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages"
    )
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["turn"]["status"] == "completed"

    late_reply = ChatMessage(
        agent_id=env.leader_id,
        sender_agent_id=env.worker_id,
        role="assistant",
        content="Deferred specialist result",
        conversation_id=group["id"],
        message_meta={
            "kind": "project_subagent_reply",
            "timeline_anchor_id": str(anchor_id),
            "leader_batch_state": "pending",
            "default_leader_agent_id": str(env.leader_id),
        },
        created_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    env.db.add(late_reply)
    await env.db.commit()

    history = await env.client.get(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages"
    )
    assert history.status_code == 200, history.text
    assert history.json()["turn"]["status"] == "completed"
    assert history.json()["turn"]["phase"] == "idle"
    assert history.json()["turn"]["run_count"] == 0

    from app.services import subagent_runtime

    assert (
        await subagent_runtime._dispatch_project_leader_batch(
            uuid.UUID(group["id"]),
            debounce_seconds=0,
        )
        is True
    )
    continued = await env.client.get(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages"
    )
    assert continued.status_code == 200, continued.text
    assert continued.json()["turn"]["status"] == "running"
    assert continued.json()["turn"]["phase"] == "active"
    assert continued.json()["turn"]["generation"] == 2

async def test_explicit_mention_cohort_waits_for_leader_reply_batch_terminal(
    project_api: ProjectApiEnv,
):
    from app.models.project import ProjectRun
    from app.services.project_group_turn_lifecycle import (
        reconcile_and_publish_project_run_group_turn,
    )

    env = project_api
    project = await _create_project(env, name="Mention owner summary cohort")
    project_id = uuid.UUID(project["id"])
    await _mark_project_running(env, str(project_id))
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    created = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Worker produce the result before the owner summarizes it",
            "mentions": [str(env.worker_id)],
            "client_message_id": "mention-owner-summary-cohort",
        },
    )
    assert created.status_code == 201, created.text
    anchor_id = uuid.UUID(created.json()["message"]["id"])
    runs = list(
        (
            await env.db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project_id,
                    ProjectRun.input["group_message_id"].as_string()
                    == str(anchor_id),
                )
            )
        ).scalars()
    )
    owner_run = next(run for run in runs if run.trigger_type == "group_leader_message")
    specialist_run = next(run for run in runs if run.trigger_type == "group_mention")
    assert owner_run.status == "cancelled"
    assert owner_run.output["skip_reason"] == "explicit_mentions_route_to_specialists"

    specialist_run.status = "succeeded"
    specialist_run.finished_at = datetime.now(UTC)
    await env.db.commit()

    reply = ChatMessage(
        agent_id=env.leader_id,
        sender_agent_id=env.worker_id,
        role="assistant",
        content="Specialist result ready for owner summary",
        conversation_id=group["id"],
        message_meta={
            "kind": "project_subagent_reply",
            "timeline_anchor_id": str(anchor_id),
            "leader_batch_state": "pending",
            "default_leader_agent_id": str(env.leader_id),
            "source_project_run_ids": [str(specialist_run.id)],
        },
    )
    env.db.add(reply)
    await env.db.commit()
    pending_projection = await reconcile_and_publish_project_run_group_turn(
        specialist_run.id
    )
    assert pending_projection is not None
    assert pending_projection.snapshot.phase == "active"
    assert pending_projection.snapshot.status == "running"
    assert pending_projection.active_agent_ids == (env.leader_id,)

    reply.message_meta = {
        **dict(reply.message_meta or {}),
        "leader_batch_state": "claimed",
    }
    batch_run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.leader_id,
        initiated_by_user_id=env.owner_id,
        execution_user_id=env.owner_id,
        status="queued",
        trigger_type="leader_reply_batch",
        input={
            "group_session_id": group["id"],
            "group_message_id": str(anchor_id),
            "source_group_message_ids": [str(reply.id)],
        },
        output={"group_session_id": group["id"]},
    )
    env.db.add(batch_run)
    await env.db.commit()
    claimed_projection = await reconcile_and_publish_project_run_group_turn(batch_run.id)
    assert claimed_projection is not None
    assert claimed_projection.snapshot.phase == "active"
    assert claimed_projection.run_count == 2
    assert claimed_projection.snapshot.revision > pending_projection.snapshot.revision

    reply.message_meta = {
        **dict(reply.message_meta or {}),
        "leader_batch_state": "delivered",
    }
    batch_run.status = "succeeded"
    batch_run.finished_at = datetime.now(UTC)
    await env.db.commit()
    terminal_projection = await reconcile_and_publish_project_run_group_turn(batch_run.id)
    assert terminal_projection is not None
    assert terminal_projection.snapshot.phase == "idle"
    assert terminal_projection.snapshot.status == "completed"
    assert terminal_projection.snapshot.anchor_id == anchor_id
    assert terminal_projection.run_count == 0

async def test_project_group_reconcile_recomputes_cohort_after_session_mutex(
    project_api: ProjectApiEnv,
):
    from app.models.chat_session import ChatSession
    from app.models.project import ProjectRun
    from app.services.project_group_turn_lifecycle import reconcile_project_group_turn

    env = project_api
    project = await _create_project(env, name="Project cohort mutex")
    project_id = uuid.UUID(project["id"])
    await _mark_project_running(env, project_id)
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    first = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "First cohort", "mentions": [], "attachments": []},
    )
    assert first.status_code == 201, first.text
    first_turn = first.json()["turn"]
    first_anchor_id = uuid.UUID(first_turn["turn_anchor_id"])

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
    assert old_runs
    for run in old_runs:
        run.status = "succeeded"
        run.finished_at = datetime.now(UTC)
    await env.db.commit()

    async with env.session_factory() as writer_db, env.session_factory() as poll_db:
        locked_session = (
            await writer_db.execute(
                select(ChatSession)
                .where(ChatSession.id == uuid.UUID(group["id"]))
                .with_for_update()
            )
        ).scalar_one()
        second_anchor = ChatMessage(
            agent_id=env.leader_id,
            user_id=env.owner_id,
            sender_user_id=env.owner_id,
            role="user",
            content="Second message joins while poll waits",
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
                agent_id=env.worker_id,
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

        poll_session = await poll_db.get(ChatSession, locked_session.id)
        poll_pid = await poll_db.scalar(text("SELECT pg_backend_pid()"))
        poll_task = asyncio.create_task(
            reconcile_project_group_turn(
                poll_db,
                project_id=project_id,
                session=poll_session,
            )
        )

        async with env.session_factory() as observer_db:
            for _ in range(100):
                wait_type = await observer_db.scalar(
                    text(
                        "SELECT wait_event_type FROM pg_stat_activity "
                        "WHERE pid = :pid"
                    ),
                    {"pid": poll_pid},
                )
                if wait_type == "Lock":
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("project cohort reconcile did not wait on the session mutex")

        await writer_db.commit()
        projection = await asyncio.wait_for(poll_task, timeout=2)
        await poll_db.commit()

    assert projection.snapshot.phase == "active"
    assert projection.snapshot.generation == 1
    assert projection.snapshot.anchor_id == first_anchor_id
    assert projection.run_count == 1
    assert projection.active_agent_ids == (env.worker_id,)
