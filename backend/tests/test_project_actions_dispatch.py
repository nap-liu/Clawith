from project_actions_support import *  # noqa: F401,F403

async def test_pending_project_dispatch_scan_cannot_starve_after_fifty_active_runs(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.project import ProjectRun
    from app.services import project_service, subagent_runtime

    env = project_api
    project = await _create_project(env, name="Fair durable dispatch scan")
    project_id = uuid.UUID(project["id"])
    await _mark_project_running(env, project_id)
    old_created_at = datetime(2026, 1, 1, tzinfo=UTC)
    new_created_at = datetime(2026, 1, 2, tzinfo=UTC)

    def dispatch_payload(task: str) -> dict[str, dict[str, str]]:
        return {
            "dispatch": {
                "group_session_id": str(uuid.uuid4()),
                "project_member_id": str(uuid.uuid4()),
                "turn_anchor_id": str(uuid.uuid4()),
                "task": task,
            }
        }

    old_active_runs = [
        ProjectRun(
            tenant_id=env.tenant_id,
            project_id=project_id,
            agent_id=env.leader_id,
            initiated_by_user_id=env.owner_id,
            status="running",
            trigger_type="leader_kickoff",
            input=dispatch_payload(f"Already dispatched {index}"),
            output={"subagent_run_id": str(uuid.uuid4())},
            created_at=old_created_at,
        )
        for index in range(55)
    ]
    pending_run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.leader_id,
        initiated_by_user_id=env.owner_id,
        status="queued",
        trigger_type="leader_kickoff",
        input=dispatch_payload("Must not be starved"),
        output={},
        created_at=new_created_at,
    )
    env.db.add_all([*old_active_runs, pending_run])
    await env.db.commit()

    # Keep this regression focused on candidate scanning. Terminal repair is
    # covered independently and must not affect active dispatched rows here.
    monkeypatch.setattr(project_service, "reconcile_project_run_terminal_state", lambda _run: False)

    pending_ids = await subagent_runtime._pending_project_dispatch_runs()

    assert pending_ids == [pending_run.id]
    assert await subagent_runtime._pending_project_dispatch_runs() == [pending_run.id]

    pending_run.output = {"subagent_run_id": str(uuid.uuid4())}
    await env.db.commit()
    assert await subagent_runtime._pending_project_dispatch_runs() == []

async def test_dispatch_does_not_regress_child_completed_project_run(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.project import Project, ProjectMemberSnapshot, ProjectRun
    from app.services import subagent_runtime

    env = project_api
    project = await _create_project(env, name="Fast child completion")
    project_id = uuid.UUID(project["id"])
    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    stored_project.status = "running"
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    leader_member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project_id,
                ProjectMemberSnapshot.agent_id == env.leader_id,
            )
        )
    ).scalar_one()
    anchor = ChatMessage(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(group["access_agent_id"]),
        user_id=env.owner_id,
        sender_user_id=env.owner_id,
        role="user",
        content="Complete immediately",
        conversation_id=group["id"],
        message_meta={"kind": "project_run_request", "mentions": [str(env.leader_id)]},
    )
    run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.leader_id,
        initiated_by_user_id=env.owner_id,
        status="queued",
        trigger_type="manual",
        input={
            "dispatch": {
                "group_session_id": group["id"],
                "project_member_id": str(leader_member.id),
                "turn_anchor_id": str(anchor.id),
                "task": "Complete immediately",
            }
        },
    )
    env.db.add_all([anchor, run])
    await env.db.commit()
    project_run_id = run.id
    child_id = uuid.uuid4()

    async def complete_before_dispatch_commit(**_kwargs):
        async with env.session_factory() as race_db:
            raced = await race_db.get(ProjectRun, project_run_id)
            assert raced is not None
            raced.status = "succeeded"
            raced.finished_at = datetime.now(UTC)
            raced.output = {"result": "Fast result"}
            await race_db.commit()
        return SimpleNamespace(id=child_id, status="completed"), True

    monkeypatch.setattr(subagent_runtime, "create_subagent", complete_before_dispatch_commit)
    result = await subagent_runtime.dispatch_project_run(project_run_id)
    assert result["status"] == "succeeded"
    env.db.expire_all()
    persisted = await env.db.get(ProjectRun, project_run_id)
    assert persisted is not None and persisted.status == "succeeded"
    assert persisted.finished_at is not None
    assert persisted.output["result"] == "Fast result"
    assert persisted.output["subagent_session_id"] == str(child_id)

async def test_dispatch_does_not_regress_fast_waiting_project_run(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.project import Project, ProjectMemberSnapshot, ProjectRun
    from app.services import subagent_runtime

    env = project_api
    project = await _create_project(env, name="Fast child waiting")
    project_id = uuid.UUID(project["id"])
    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    stored_project.status = "running"
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    leader_member = await env.db.scalar(
        select(ProjectMemberSnapshot).where(
            ProjectMemberSnapshot.project_id == project_id,
            ProjectMemberSnapshot.agent_id == env.leader_id,
        )
    )
    assert leader_member is not None
    anchor = ChatMessage(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(group["access_agent_id"]),
        user_id=env.owner_id,
        sender_user_id=env.owner_id,
        role="user",
        content="Wait immediately",
        conversation_id=group["id"],
        message_meta={"kind": "project_run_request"},
    )
    run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.leader_id,
        initiated_by_user_id=env.owner_id,
        status="queued",
        trigger_type="manual",
        input={
            "dispatch": {
                "group_session_id": group["id"],
                "project_member_id": str(leader_member.id),
                "turn_anchor_id": str(anchor.id),
                "task": "Wait immediately",
            }
        },
    )
    env.db.add_all([anchor, run])
    await env.db.commit()
    project_run_id = run.id
    child_id = uuid.uuid4()

    async def wait_before_dispatch_commit(**_kwargs):
        async with env.session_factory() as race_db:
            raced = await race_db.get(ProjectRun, project_run_id)
            assert raced is not None
            raced.status = "waiting"
            raced.started_at = datetime.now(UTC)
            race_db.add(
                ChatMessage(
                    agent_id=env.leader_id,
                    user_id=env.owner_id,
                    sender_user_id=env.owner_id,
                    role="user",
                    content="Wait immediately",
                    conversation_id=str(child_id),
                    message_meta={
                        "kind": subagent_runtime.SUBAGENT_INPUT,
                        "subagent_input_state": subagent_runtime.INPUT_PROCESSING,
                        "project_run_id": str(project_run_id),
                    },
                )
            )
            await race_db.commit()
        return SimpleNamespace(id=child_id, status=subagent_runtime.RUN_WAITING), True

    monkeypatch.setattr(subagent_runtime, "create_subagent", wait_before_dispatch_commit)
    result = await subagent_runtime.dispatch_project_run(project_run_id)

    assert result["status"] == "waiting"
    env.db.expire_all()
    persisted = await env.db.get(ProjectRun, project_run_id)
    assert persisted is not None and persisted.status == "waiting"
    assert persisted.started_at is not None

async def test_project_dispatch_respects_project_parallel_task_limit(
    project_api: ProjectApiEnv,
):
    from app.models.project import Project, ProjectMemberSnapshot, ProjectRun
    from app.services import subagent_runtime
    from app.services.project_service import freeze_run_members

    env = project_api
    project = await _create_project(env, name="Bounded project concurrency")
    project_id = uuid.UUID(project["id"])
    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    stored_project.status = "running"
    stored_project.settings = {
        **dict(stored_project.settings or {}),
        "runtime": {"max_parallel_runs": 1},
    }
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    leader_member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project_id,
                ProjectMemberSnapshot.agent_id == env.leader_id,
            )
        )
    ).scalar_one()
    anchor = ChatMessage(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(group["access_agent_id"]),
        user_id=env.owner_id,
        sender_user_id=env.owner_id,
        role="user",
        content="Run after capacity is available",
        conversation_id=group["id"],
        message_meta={"kind": "project_run_request"},
    )
    active = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.worker_id,
        initiated_by_user_id=env.owner_id,
        status="running",
        trigger_type="leader_kickoff",
        started_at=datetime.now(UTC),
    )
    pending = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.leader_id,
        initiated_by_user_id=env.owner_id,
        status="queued",
        trigger_type="manual",
        input={
            "dispatch": {
                "group_session_id": group["id"],
                "project_member_id": str(leader_member.id),
                "turn_anchor_id": str(anchor.id),
                "task": "Run after capacity is available",
            }
        },
    )
    env.db.add_all([anchor, active, pending])
    await env.db.flush()
    await freeze_run_members(env.db, stored_project, pending)
    await env.db.commit()
    active_id = active.id
    pending_id = pending.id

    dispatched = await subagent_runtime.dispatch_project_run(pending_id)
    assert dispatched["status"] == "queued"
    child_id = uuid.UUID(dispatched["subagent_run_id"])
    env.db.expire_all()
    still_pending = await env.db.get(ProjectRun, pending_id)
    assert still_pending is not None and still_pending.status == "queued"
    assert still_pending.started_at is None
    assert still_pending.output["subagent_run_id"] == str(child_id)
    assert await subagent_runtime._claim_subagent(child_id) is None

    stored_active = await env.db.get(ProjectRun, active_id)
    assert stored_active is not None
    stored_active.status = "succeeded"
    stored_active.finished_at = datetime.now(UTC)
    await env.db.commit()
    assert await subagent_runtime._claim_subagent(child_id) == child_id
    claimed_input = await subagent_runtime._load_or_start_input(child_id)
    assert claimed_input is not None
    claimed_anchor, _recovering = claimed_input
    env.db.expire_all()
    started = await env.db.get(ProjectRun, pending_id)
    assert started is not None and started.status == "running"
    assert started.started_at is not None

    await subagent_runtime._requeue_capacity_blocked_subagent(child_id, claimed_anchor.id)
    env.db.expire_all()
    requeued = await env.db.get(ProjectRun, pending_id)
    assert requeued is not None and requeued.status == "queued"
    assert requeued.started_at is None
    input_row = await env.db.get(ChatMessage, claimed_anchor.id)
    assert input_row is not None
    assert input_row.message_meta["subagent_input_state"] == subagent_runtime.INPUT_PENDING

async def test_active_child_inputs_durably_advance_only_their_exact_project_runs(
    project_api: ProjectApiEnv,
):
    """Worker recovery must not leave the Runs UI stuck at queued."""
    from app.models.project import ProjectMemberSnapshot, ProjectRun
    from app.models.subagent_run import SubagentRun
    from app.services import subagent_runtime
    from app.services.project_service import freeze_run_members

    env = project_api
    project = await _create_project(env, name="Project run worker state")
    project_id = uuid.UUID(project["id"])
    await _mark_project_running(env, project_id)
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    leader_member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project_id,
                ProjectMemberSnapshot.agent_id == env.leader_id,
            )
        )
    ).scalar_one()
    anchor = ChatMessage(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(group["access_agent_id"]),
        user_id=env.owner_id,
        sender_user_id=env.owner_id,
        role="user",
        content="Run the exact durable input",
        conversation_id=group["id"],
        message_meta={"kind": "project_run_request"},
    )
    project_run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.leader_id,
        initiated_by_user_id=env.owner_id,
        status="queued",
        trigger_type="manual",
        input={
            "dispatch": {
                "group_session_id": group["id"],
                "project_member_id": str(leader_member.id),
                "turn_anchor_id": str(anchor.id),
                "task": "Run the exact durable input",
            }
        },
    )
    unrelated_run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.leader_id,
        initiated_by_user_id=env.owner_id,
        status="queued",
        trigger_type="manual",
        input={"objective": "Must remain queued until its own input runs"},
    )
    terminal_finished_at = datetime.now(UTC)
    terminal_run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.leader_id,
        initiated_by_user_id=env.owner_id,
        status="succeeded",
        trigger_type="manual",
        input={"objective": "Already finished"},
        finished_at=terminal_finished_at,
    )
    env.db.add_all([anchor, project_run, unrelated_run, terminal_run])
    await env.db.flush()
    project_row = await env.db.get(Project, project_id)
    assert project_row is not None
    await freeze_run_members(env.db, project_row, project_run)
    await env.db.commit()
    project_run_id = project_run.id
    unrelated_run_id = unrelated_run.id
    terminal_run_id = terminal_run.id

    dispatched = await subagent_runtime.dispatch_project_run(project_run_id)
    child_id = uuid.UUID(dispatched["subagent_session_id"])
    assert dispatched["status"] == "queued"

    # Reproduce a process exit after the exact input became processing but
    # before an older worker updated ProjectRun.
    assert await subagent_runtime._claim_subagent(child_id) == child_id
    async with env.session_factory() as crash_db:
        child = await crash_db.get(SubagentRun, child_id, with_for_update=True)
        child_input = (
            await crash_db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(child_id),
                    ChatMessage.message_meta["project_run_id"].as_string() == str(project_run_id),
                )
            )
        ).scalar_one()
        child_input.message_meta = {
            **dict(child_input.message_meta or {}),
            "subagent_input_state": "processing",
            "subagent_turn_anchor_id": str(child_input.id),
            "turn_status": "running",
        }
        child.lease_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
        await crash_db.commit()

    assert await subagent_runtime._claim_subagent(child_id) == child_id
    recovered = await subagent_runtime._load_or_start_input(child_id)
    assert recovered is not None and recovered[1] is True

    env.db.expire_all()
    running = await env.db.get(ProjectRun, project_run_id)
    untouched = await env.db.get(ProjectRun, unrelated_run_id)
    terminal = await env.db.get(ProjectRun, terminal_run_id)
    assert running is not None and running.status == "running"
    assert running.started_at is not None
    first_started_at = running.started_at
    assert untouched is not None and untouched.status == "queued"
    assert untouched.started_at is None
    assert terminal is not None and terminal.status == "succeeded"
    assert terminal.finished_at is not None
    terminal_persisted_finished_at = terminal.finished_at

    # Inputs appended while the reusable child is already executing are
    # consumed at a round boundary. They use the same exact-id transition;
    # a referenced terminal Run must still remain terminal.
    env.db.add_all(
        [
            ChatMessage(
                agent_id=env.leader_id,
                user_id=env.owner_id,
                sender_user_id=env.owner_id,
                role="user",
                content="Start the second exact run",
                conversation_id=str(child_id),
                message_meta={
                    "kind": "subagent_input",
                    "subagent_input_state": "pending",
                    "project_run_id": str(unrelated_run_id),
                },
            ),
            ChatMessage(
                agent_id=env.leader_id,
                user_id=env.owner_id,
                sender_user_id=env.owner_id,
                role="user",
                content="Do not regress the terminal run",
                conversation_id=str(child_id),
                message_meta={
                    "kind": "subagent_input",
                    "subagent_input_state": "pending",
                    "project_run_id": str(terminal_run_id),
                },
            ),
        ]
    )
    await env.db.commit()
    injected = await subagent_runtime._drain_subagent_inbox(
        child_id,
        recovered[0].id,
    )
    assert {row["content"] for row in injected} == {
        "Start the second exact run",
        "Do not regress the terminal run",
    }
    env.db.expire_all()
    now_running = await env.db.get(ProjectRun, unrelated_run_id)
    terminal_after_drain = await env.db.get(ProjectRun, terminal_run_id)
    assert now_running is not None and now_running.status == "running"
    assert now_running.started_at is not None
    assert terminal_after_drain is not None
    assert terminal_after_drain.status == "succeeded"
    assert terminal_after_drain.finished_at == terminal_persisted_finished_at

    # Idempotent recovery keeps the original start timestamp and terminal fact.
    recovered_again = await subagent_runtime._load_or_start_input(child_id)
    assert recovered_again is not None and recovered_again[1] is True
    env.db.expire_all()
    running_again = await env.db.get(ProjectRun, project_run_id)
    terminal_again = await env.db.get(ProjectRun, terminal_run_id)
    assert running_again is not None and running_again.started_at == first_started_at
    assert terminal_again is not None and terminal_again.status == "succeeded"
    assert terminal_again.finished_at == terminal_persisted_finished_at
