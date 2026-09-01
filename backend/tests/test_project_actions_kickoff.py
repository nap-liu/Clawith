from project_actions_support import *  # noqa: F401,F403

async def test_kickoff_requires_leader_discussion_then_freezes_and_starts(project_api: ProjectApiEnv):
    from app.models.project import Project, ProjectRun, ProjectRunMemberSnapshot
    from app.models.subagent_run import SubagentRun
    from app.services.subagent_runtime import prepare_subagent_tools

    env = project_api
    project = await _create_project(env, name="Plan before execution")
    project_id = project["id"]
    assert project["status"] == "planning"

    first = await env.client.get(f"/api/projects/{project_id}/leader-session")
    second = await env.client.get(f"/api/projects/{project_id}/leader-session")
    assert first.status_code == 200, first.text
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["source_channel"] == "web"
    assert first.json()["agent_id"] == str(env.leader_id)
    assert first.json()["read_only"] is True
    assert first.json()["planning_transport"] == "project_group"

    bypass = await env.client.patch(f"/api/projects/{project_id}", json={"status": "running"})
    assert bypass.status_code == 409

    no_discussion = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "Start now"},
    )
    assert no_discussion.status_code == 422

    session_id = first.json()["id"]
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    planning_started_at = datetime.now(UTC)
    env.db.add_all(
        [
            ChatMessage(
                agent_id=uuid.UUID(group["access_agent_id"]),
                user_id=env.owner_id,
                sender_user_id=env.owner_id,
                role="user",
                content="Plan the delivery around traceable evidence.",
                conversation_id=group["id"],
                message_meta={"kind": "project_group_message", "visible_to_group": True},
                created_at=planning_started_at,
            ),
            ChatMessage(
                agent_id=uuid.UUID(group["access_agent_id"]),
                sender_agent_id=env.leader_id,
                role="assistant",
                content="I will coordinate the team and commit every deliverable.",
                conversation_id=group["id"],
                message_meta={"kind": "project_subagent_reply", "visible_to_group": True},
                created_at=planning_started_at + timedelta(seconds=1),
            ),
        ]
    )
    await env.db.commit()

    confirmed = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "Approved. Execute this plan."},
    )
    assert confirmed.status_code == 202, confirmed.text
    body = confirmed.json()
    assert body["status"] == "running"
    assert body["leader_session_id"] == session_id
    assert body["discussion_source"] == "project_group"
    assert body["discussion_session_id"] == group["id"]
    assert body["awakened_agent_ids"] == [str(env.leader_id)]
    assert body["subagent_session_id"] == body["subagent_run_id"]
    assert body["git_start_commit"] != body["transcript_commit"]

    env.db.expire_all()
    stored_project = await env.db.get(Project, uuid.UUID(project_id))
    run = await env.db.get(ProjectRun, uuid.UUID(body["run_id"]))
    child = await env.db.get(SubagentRun, uuid.UUID(body["subagent_run_id"]))
    assert stored_project is not None and stored_project.status == "running"
    assert stored_project.settings["planning"]["state"] == "confirmed"
    assert stored_project.settings["planning"]["launch_confirmed"] is True
    assert stored_project.settings["kickoff"]["project_run_id"] == body["run_id"]
    assert run is not None and run.trigger_type == "leader_kickoff"
    assert run.input["conversation_snapshot"]["message_count"] == 2
    assert run.input["conversation_snapshot"]["transcript_sha256"]
    kickoff_task = run.input["dispatch"]["task"]
    assert "first substantive delivery decision" in kickoff_task
    assert "explicit dependency-aware work-item plan" in kickoff_task
    assert "Do not ask members to acknowledge, wait, or provide routine progress updates" in kickoff_task
    assert "Creating or assigning a work item does not wake its assignee" in kickoff_task
    assert "exactly one project A2A task_delegate" in kickoff_task
    assert "Do not mark a delegated work item in progress without that exact handoff" in kickoff_task
    assert "Do not expose internal narration" in kickoff_task
    assert "report progress to the group" not in kickoff_task
    assert run.output["transcript_commit"] == body["transcript_commit"]
    snapshots = (
        (await env.db.execute(select(ProjectRunMemberSnapshot).where(ProjectRunMemberSnapshot.run_id == run.id)))
        .scalars()
        .all()
    )
    assert len(snapshots) == 3
    assert child is not None and child.project_id == uuid.UUID(project_id)
    assert child.project_member_id == next(item.project_member_id for item in snapshots if item.is_leader)
    kickoff_tool_names = {
        item["function"]["name"]
        for item in await prepare_subagent_tools(
            env.leader_id,
            child.id,
            execution_user_id=env.owner_id,
        )
    }
    assert {"project_get_context", "project_create_work_item", "project_update_plan"} <= kickoff_tool_names

    transcript = subprocess.run(
        [
            "git",
            "-C",
            str(project_repo_path(env.tenant_id, uuid.UUID(project_id))),
            "show",
            f"{body['transcript_commit']}:docs/kickoff-transcript.md",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Plan the delivery around traceable evidence." in transcript
    assert "I will coordinate the team" in transcript
    assert "Approved. Execute this plan." in transcript

    event = (
        await env.db.execute(
            select(ProjectEvent).where(
                ProjectEvent.project_id == uuid.UUID(project_id),
                ProjectEvent.event_type == "project.kickoff.confirmed",
            )
        )
    ).scalar_one()
    assert event.run_id == run.id
    assert event.event_metadata["awakened_agent_ids"] == [str(env.leader_id)]
    assert event.event_metadata["transcript_commit"] == body["transcript_commit"]
    kickoff_group_message = (
        await env.db.execute(
            select(ChatMessage).where(ChatMessage.external_event_key.like(f"project-kickoff:{project_id}:%"))
        )
    ).scalar_one()
    assert kickoff_group_message.message_meta["mentions"] == []
    assert kickoff_group_message.message_meta["awakened_agent_ids"] == [str(env.leader_id)]

    repeated = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "Do not start twice"},
    )
    assert repeated.status_code == 409

async def test_kickoff_accepts_human_leader_discussion_from_project_group(
    project_api: ProjectApiEnv,
):
    from app.models.project import ProjectRun

    env = project_api
    project = await _create_project(env, name="Group planning source")
    group = (await env.client.get(f"/api/projects/{project['id']}/group-session")).json()
    planning_started_at = datetime.now(UTC)
    env.db.add_all(
        [
            ChatMessage(
                agent_id=uuid.UUID(group["access_agent_id"]),
                user_id=env.owner_id,
                sender_user_id=env.owner_id,
                role="user",
                content="Keep the plan small and make every result traceable.",
                conversation_id=group["id"],
                message_meta={"kind": "project_group_message", "visible_to_group": True},
                created_at=planning_started_at,
            ),
            ChatMessage(
                agent_id=uuid.UUID(group["access_agent_id"]),
                sender_agent_id=env.leader_id,
                role="assistant",
                content="I will deliver in two milestones with Git evidence.",
                conversation_id=group["id"],
                message_meta={"kind": "project_subagent_reply", "visible_to_group": True},
                created_at=planning_started_at + timedelta(seconds=1),
            ),
            # Participant replies remain in the group audit log but are not
            # part of the Human/Leader agreement frozen at kickoff.
            ChatMessage(
                agent_id=uuid.UUID(group["access_agent_id"]),
                sender_agent_id=env.worker_id,
                role="assistant",
                content="Worker side note",
                conversation_id=group["id"],
                message_meta={"kind": "project_subagent_reply", "visible_to_group": True},
                created_at=planning_started_at + timedelta(seconds=2),
            ),
        ]
    )
    await env.db.commit()

    confirmed = await env.client.post(
        f"/api/projects/{project['id']}/kickoff/confirm",
        json={"confirmation": "方案确认，开始执行。"},
    )
    assert confirmed.status_code == 202, confirmed.text
    payload = confirmed.json()
    assert payload["status"] == "running"
    assert payload["discussion_source"] == "project_group"
    assert payload["discussion_session_id"] == group["id"]

    run = await env.db.get(ProjectRun, uuid.UUID(payload["run_id"]))
    assert run is not None
    assert run.input["conversation_snapshot"]["source"] == "project_group"
    assert run.input["conversation_snapshot"]["session_id"] == group["id"]
    assert run.input["conversation_snapshot"]["message_count"] == 2

    transcript = subprocess.run(
        [
            "git",
            "-C",
            str(project_repo_path(env.tenant_id, uuid.UUID(project["id"]))),
            "show",
            f"{payload['transcript_commit']}:docs/kickoff-transcript.md",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Keep the plan small" in transcript
    assert "I will deliver in two milestones" in transcript
    assert "Worker side note" not in transcript

async def test_kickoff_waits_for_active_project_planning_run(project_api: ProjectApiEnv):
    from app.models.project import ProjectRun

    env = project_api
    project = await _create_project(env, name="Planning run barrier")
    project_id = uuid.UUID(project["id"])
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    env.db.add_all(
        [
            ChatMessage(
                agent_id=uuid.UUID(group["access_agent_id"]),
                user_id=env.owner_id,
                sender_user_id=env.owner_id,
                role="user",
                content="Prepare the final delivery plan.",
                conversation_id=group["id"],
                message_meta={"kind": "project_group_message", "visible_to_group": True},
            ),
            ChatMessage(
                agent_id=uuid.UUID(group["access_agent_id"]),
                sender_agent_id=env.leader_id,
                role="assistant",
                content="The plan is ready for approval.",
                conversation_id=group["id"],
                message_meta={"kind": "project_subagent_reply", "visible_to_group": True},
            ),
            ProjectRun(
                tenant_id=env.tenant_id,
                project_id=project_id,
                agent_id=env.leader_id,
                initiated_by_user_id=env.owner_id,
                status="running",
                trigger_type="group_leader_message",
                input={"title": "Finish planning"},
            ),
        ]
    )
    await env.db.commit()

    blocked = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "Start only after planning settles"},
    )
    assert blocked.status_code == 409
    assert "still being processed" in blocked.json()["detail"]

async def test_legacy_leader_session_kickoff_waits_for_terminal_reply(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Legacy planning turn barrier")
    project_id = uuid.UUID(project["id"])
    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    stored_project.settings = {
        **dict(stored_project.settings or {}),
        "planning": {
            **dict(dict(stored_project.settings or {}).get("planning") or {}),
            "conversation_mode": "leader_session",
        },
    }
    await env.db.commit()
    leader_session = (await env.client.get(f"/api/projects/{project_id}/leader-session")).json()
    assert leader_session["read_only"] is True
    assert leader_session["planning_transport"] == "leader_session"

    planning_started_at = datetime.now(UTC)
    anchor = ChatMessage(
        agent_id=env.leader_id,
        user_id=env.owner_id,
        sender_user_id=env.owner_id,
        role="user",
        content="Finish the plan before kickoff.",
        conversation_id=leader_session["id"],
        message_meta={},
        created_at=planning_started_at,
    )
    env.db.add(anchor)
    await env.db.flush()
    anchor_id = anchor.id
    await env.db.commit()

    blocked = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "Do not overlap turns"},
    )
    assert blocked.status_code == 409
    assert "still being processed" in blocked.json()["detail"]

    env.db.add(
        ChatMessage(
            agent_id=env.leader_id,
            sender_agent_id=env.leader_id,
            role="assistant",
            content="The plan is now complete.",
            conversation_id=leader_session["id"],
            message_meta={"turn_anchor_id": str(anchor_id), "turn_status": "completed"},
            created_at=planning_started_at + timedelta(seconds=1),
        )
    )
    await env.db.commit()
    confirmed = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "The completed plan is approved"},
    )
    assert confirmed.status_code == 202, confirmed.text
    assert confirmed.json()["discussion_source"] == "leader_session"

async def test_kickoff_outbox_recovers_initializing_project(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.project import Project, ProjectEvent, ProjectRun
    from app.services import subagent_runtime

    env = project_api
    project = await _create_project(env, name="Recoverable kickoff")
    project_id = project["id"]
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    planning_started_at = datetime.now(UTC)
    env.db.add_all(
        [
            ChatMessage(
                agent_id=uuid.UUID(group["access_agent_id"]),
                user_id=env.owner_id,
                sender_user_id=env.owner_id,
                role="user",
                content="Agree the plan before autonomous execution.",
                conversation_id=group["id"],
                message_meta={"kind": "project_group_message", "visible_to_group": True},
                created_at=planning_started_at,
            ),
            ChatMessage(
                agent_id=uuid.UUID(group["access_agent_id"]),
                sender_agent_id=env.leader_id,
                role="assistant",
                content="The traceable plan is ready for confirmation.",
                conversation_id=group["id"],
                message_meta={"kind": "project_subagent_reply", "visible_to_group": True},
                created_at=planning_started_at + timedelta(seconds=1),
            ),
        ]
    )
    await env.db.commit()
    real_dispatch = subagent_runtime.dispatch_project_run

    async def simulated_process_exit(_run_id):
        raise RuntimeError("simulated exit after kickoff outbox commit")

    monkeypatch.setattr(subagent_runtime, "dispatch_project_run", simulated_process_exit)
    first = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "Confirmed once"},
    )
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "initializing"
    run_id = uuid.UUID(first.json()["run_id"])
    stored = await env.db.get(Project, uuid.UUID(project_id))
    pending = await env.db.get(ProjectRun, run_id)
    assert stored is not None and stored.status == "initializing"
    assert pending is not None and pending.input["dispatch"]["task"]

    monkeypatch.setattr(subagent_runtime, "dispatch_project_run", real_dispatch)
    recovered = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "A retry must recover, not start twice"},
    )
    assert recovered.status_code == 202, recovered.text
    assert recovered.json()["status"] == "running"
    assert uuid.UUID(recovered.json()["run_id"]) == run_id
    env.db.expire_all()
    stored = await env.db.get(Project, uuid.UUID(project_id))
    events = (
        (
            await env.db.execute(
                select(ProjectEvent).where(
                    ProjectEvent.project_id == uuid.UUID(project_id),
                    ProjectEvent.event_type == "project.kickoff.confirmed",
                )
            )
        )
        .scalars()
        .all()
    )
    assert stored is not None and stored.status == "running"
    assert len(events) == 1
