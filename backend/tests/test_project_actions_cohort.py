from project_actions_support import *  # noqa: F401,F403

async def test_project_group_timeline_page_does_not_materialize_unrelated_history(
    project_api: ProjectApiEnv,
):
    from sqlalchemy import event as sqlalchemy_event

    from app.models.audit import ChatMessage
    from app.models.project import ProjectRun
    from app.services.project_group_timeline import build_project_group_timeline

    env = project_api
    project = await _create_project(env, name="Bounded group timeline")
    project_id = uuid.UUID(project["id"])
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    child_id = uuid.uuid4()
    page_message = ChatMessage(
        agent_id=env.leader_id,
        user_id=env.owner_id,
        sender_user_id=env.owner_id,
        role="user",
        content="Current page anchor",
        conversation_id=group["id"],
        message_meta={"kind": "project_group_message", "visible_to_group": True},
    )
    old_message = ChatMessage(
        agent_id=env.leader_id,
        user_id=env.owner_id,
        sender_user_id=env.owner_id,
        role="user",
        content="Unrelated old anchor",
        conversation_id=group["id"],
        message_meta={"kind": "project_group_message", "visible_to_group": True},
    )
    env.db.add_all([page_message, old_message])
    await env.db.flush()
    relevant_run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.worker_id,
        initiated_by_user_id=env.owner_id,
        execution_user_id=env.owner_id,
        status="running",
        trigger_type="group_mention",
        input={"group_message_id": str(page_message.id)},
        output={"subagent_session_id": str(child_id)},
    )
    unrelated_run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.worker_id,
        initiated_by_user_id=env.owner_id,
        execution_user_id=env.owner_id,
        status="succeeded",
        trigger_type="group_mention",
        input={"group_message_id": str(old_message.id)},
        output={"subagent_session_id": str(child_id)},
    )
    env.db.add_all([relevant_run, unrelated_run])
    await env.db.flush()
    relevant_input_id = uuid.uuid4()
    unrelated_input_id = uuid.uuid4()
    relevant_input = ChatMessage(
        id=relevant_input_id,
        agent_id=env.worker_id,
        user_id=env.owner_id,
        role="user",
        content="Relevant child input",
        conversation_id=str(child_id),
        message_meta={
            "kind": "subagent_input",
            "project_run_id": str(relevant_run.id),
            "subagent_turn_anchor_id": str(relevant_input_id),
        },
    )
    unrelated_input = ChatMessage(
        id=unrelated_input_id,
        agent_id=env.worker_id,
        user_id=env.owner_id,
        role="user",
        content="Unrelated child input",
        conversation_id=str(child_id),
        message_meta={
            "kind": "subagent_input",
            "project_run_id": str(unrelated_run.id),
            "subagent_turn_anchor_id": str(unrelated_input_id),
        },
    )
    relevant_reply = ChatMessage(
        agent_id=env.worker_id,
        sender_agent_id=env.worker_id,
        role="assistant",
        content="Relevant child reply",
        conversation_id=str(child_id),
        message_meta={"turn_anchor_id": str(relevant_input_id)},
    )
    unrelated_reply = ChatMessage(
        agent_id=env.worker_id,
        sender_agent_id=env.worker_id,
        role="assistant",
        content="Unrelated child reply",
        conversation_id=str(child_id),
        message_meta={"turn_anchor_id": str(unrelated_input_id)},
    )
    env.db.add_all(
        [relevant_input, unrelated_input, relevant_reply, unrelated_reply]
    )
    await env.db.commit()

    page_message_id = page_message.id
    relevant_run_id = relevant_run.id
    unrelated_run_id = unrelated_run.id
    relevant_reply_id = relevant_reply.id
    unrelated_reply_id = unrelated_reply.id
    env.db.expunge_all()
    page_row = await env.db.get(ChatMessage, page_message_id)
    loaded_run_ids: set[uuid.UUID] = set()
    loaded_message_ids: set[uuid.UUID] = set()

    def capture_loaded(_session, instance):
        if isinstance(instance, ProjectRun):
            loaded_run_ids.add(instance.id)
        elif isinstance(instance, ChatMessage):
            loaded_message_ids.add(instance.id)

    sqlalchemy_event.listen(env.db.sync_session, "loaded_as_persistent", capture_loaded)
    try:
        timeline = await build_project_group_timeline(
            env.db,
            project_id=project_id,
            group_messages=[page_row],
        )
    finally:
        sqlalchemy_event.remove(
            env.db.sync_session,
            "loaded_as_persistent",
            capture_loaded,
        )

    assert relevant_run_id in loaded_run_ids
    assert unrelated_run_id not in loaded_run_ids
    assert relevant_input_id in loaded_message_ids
    assert unrelated_input_id not in loaded_message_ids
    assert relevant_reply_id in loaded_message_ids
    assert unrelated_reply_id not in loaded_message_ids
    assert any(item["content"] == "Relevant child reply" for item in timeline)
    assert all(item["content"] != "Unrelated child reply" for item in timeline)

async def test_project_participant_replies_coalesce_into_one_durable_leader_turn(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.audit import ChatMessage
    from app.models.project import ProjectEvent, ProjectRun, ProjectWorkItem
    from app.models.subagent_run import SubagentRun
    from app.services import subagent_runtime

    env = project_api
    project = await _create_project(env, name="Coalesced Leader inbox")
    project_id = uuid.UUID(project["id"])
    await _mark_project_running(env, project_id)
    dependency = ProjectWorkItem(
        tenant_id=env.tenant_id,
        project_id=project_id,
        assignee_agent_id=env.reviewer_id,
        created_by_agent_id=env.leader_id,
        title="Confirm the upstream evidence source",
        description="The source contract must be settled first",
        status="done",
        priority="medium",
        acceptance_criteria=["Source contract is recorded"],
        dependency_ids=[],
    )
    env.db.add(dependency)
    await env.db.flush()
    dependency_id = dependency.id
    work_item = ProjectWorkItem(
        tenant_id=env.tenant_id,
        project_id=project_id,
        assignee_agent_id=env.worker_id,
        created_by_agent_id=env.leader_id,
        title="Collect exact participant evidence",
        description="All replies in this batch belong to one explicit work item",
        status="todo",
        priority="medium",
        acceptance_criteria=["Both participant replies are consolidated with attributed evidence"],
        dependency_ids=[str(dependency_id)],
    )
    env.db.add(work_item)
    await env.db.flush()
    env.db.add(
        ProjectEvent(
            tenant_id=env.tenant_id,
            project_id=project_id,
            work_item_id=work_item.id,
            actor_agent_id=env.worker_id,
            event_type="work_item.updated",
            summary="Participant evidence recorded",
            event_metadata={"evidence": ["docs/participant-evidence.md", "commit-a1b2c3d4"]},
        )
    )
    await env.db.commit()
    work_item_id = work_item.id
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    wake = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Ask two participants and let Leader coordinate results",
            "mentions": [str(env.worker_id), str(env.reviewer_id)],
            "work_item_id": str(work_item_id),
        },
    )
    assert wake.status_code == 201, wake.text
    run_by_agent = {row["agent_id"]: row for row in wake.json()["subagent_runs"]}
    created_runs = (
        (
            await env.db.execute(
                select(ProjectRun).where(
                    ProjectRun.id.in_([uuid.UUID(row["project_run_id"]) for row in run_by_agent.values()])
                )
            )
        )
        .scalars()
        .all()
    )
    assert created_runs
    assert {row.work_item_id for row in created_runs} == {work_item_id}
    assert run_by_agent[str(env.leader_id)]["run_id"] is None
    assert run_by_agent[str(env.leader_id)]["status"] == "skipped"
    worker_child_id = uuid.UUID(run_by_agent[str(env.worker_id)]["session_id"])
    reviewer_child_id = uuid.UUID(run_by_agent[str(env.reviewer_id)]["session_id"])

    completion_rows = [
        ChatMessage(
            agent_id=agent_id,
            sender_agent_id=agent_id,
            role="assistant",
            content=content,
            conversation_id=str(child_id),
            message_meta={
                "kind": "subagent_completion",
                "subagent_wake": True,
                "project_run_ids": [project_run_id],
                "attachments": attachments,
            },
        )
        for agent_id, child_id, content, project_run_id, attachments in [
            (
                env.worker_id,
                worker_child_id,
                "Worker evidence A",
                run_by_agent[str(env.worker_id)]["project_run_id"],
                [{"name": "evidence-a.md", "path": "evidence-a.md"}],
            ),
            (
                env.reviewer_id,
                reviewer_child_id,
                "Reviewer finding B",
                run_by_agent[str(env.reviewer_id)]["project_run_id"],
                [],
            ),
            (
                env.worker_id,
                worker_child_id,
                "Worker follow-up C",
                run_by_agent[str(env.worker_id)]["project_run_id"],
                [],
            ),
        ]
    ]
    env.db.add_all(completion_rows)
    await env.db.commit()

    async def forbidden_resume(_anchor):
        raise AssertionError("participant replies must not resume the project group root")

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", forbidden_resume)
    for completion in completion_rows:
        assert await subagent_runtime._dispatch_parent_event(completion.id) is True

    assert (
        await subagent_runtime._dispatch_project_leader_batch(
            uuid.UUID(group["id"]),
            debounce_seconds=0,
        )
        is True
    )
    env.db.expire_all()
    leader_child_id = (
        await env.db.execute(
            select(ChatSession.id)
            .where(
                ChatSession.project_id == project_id,
                ChatSession.agent_id == env.leader_id,
                ChatSession.source_channel == "subagent",
            )
            .order_by(ChatSession.created_at, ChatSession.id)
        )
    ).scalar_one()
    leader_inputs = (
        (
            await env.db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(leader_child_id),
                    ChatMessage.message_meta["project_leader_batch"].as_boolean().is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(leader_inputs) == 1
    batch_input = leader_inputs[0]
    assert len(batch_input.message_meta["source_group_message_ids"]) == 3
    assert len(batch_input.message_meta["source_replies"]) == 3
    assert batch_input.message_meta["batch_limits"] == {
        "max_replies": subagent_runtime.PROJECT_LEADER_BATCH_MAX_REPLIES,
        "max_bytes": subagent_runtime.PROJECT_LEADER_BATCH_MAX_BYTES,
    }
    assert batch_input.message_meta["batch_input_bytes"] <= subagent_runtime.PROJECT_LEADER_BATCH_MAX_BYTES
    assert batch_input.message_meta["original_human_request"]["content"] == (
        "Ask two participants and let Leader coordinate results"
    )
    [work_item_snapshot] = batch_input.message_meta["work_item_snapshots"]
    assert work_item_snapshot["title"] == "Collect exact participant evidence"
    assert work_item_snapshot["acceptance_criteria"] == [
        "Both participant replies are consolidated with attributed evidence"
    ]
    assert work_item_snapshot["dependencies"] == [
        {
            "id": str(dependency_id),
            "title": "Confirm the upstream evidence source",
            "status": "done",
        }
    ]
    assert work_item_snapshot["evidence"] == ["docs/participant-evidence.md", "commit-a1b2c3d4"]
    assert "Ask two participants and let Leader coordinate results" in batch_input.content
    assert "Both participant replies are consolidated with attributed evidence" in batch_input.content
    assert "docs/participant-evidence.md" in batch_input.content
    assert {
        (reply["source_agent_name"], reply["source_role_snapshot"])
        for reply in batch_input.message_meta["source_replies"]
    } == {
        ("Worker", "Build deliverables"),
        ("Reviewer", "Review evidence"),
    }
    batch_attachments = [
        attachment for reply in batch_input.message_meta["source_replies"] for attachment in reply["attachments"]
    ]
    assert batch_attachments[0]["name"] == "evidence-a.md"

    materialized = (
        (
            await env.db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == group["id"],
                    ChatMessage.message_meta["kind"].as_string() == "project_subagent_reply",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(materialized) == 3
    assert {row.message_meta["leader_batch_state"] for row in materialized} == {"delivered"}
    assert len({row.message_meta["leader_batch_id"] for row in materialized}) == 1

    batch_runs = (
        (
            await env.db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project_id,
                    ProjectRun.trigger_type == "leader_reply_batch",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(batch_runs) == 1
    assert batch_runs[0].work_item_id == work_item_id
    assert batch_runs[0].input["group_message_id"] == wake.json()["message"]["id"]
    assert batch_runs[0].input["related_work_item_ids"] == [str(work_item_id)]
    assert batch_runs[0].input["original_human_request"] == batch_input.message_meta["original_human_request"]
    assert batch_runs[0].input["work_item_snapshots"] == batch_input.message_meta["work_item_snapshots"]
    assert batch_runs[0].output["subagent_session_id"] == str(leader_child_id)
    assert batch_runs[0].output["source_count"] == 3
    leader_run = await env.db.get(SubagentRun, leader_child_id)
    assert leader_run is not None and leader_run.parent_session_id == uuid.UUID(group["id"])

    assert (
        await subagent_runtime._dispatch_project_leader_batch(
            uuid.UUID(group["id"]),
            debounce_seconds=0,
        )
        is False
    )
    env.db.expire_all()
    replay_inputs = (
        (
            await env.db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(leader_child_id),
                    ChatMessage.message_meta["project_leader_batch"].as_boolean().is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(replay_inputs) == 1

async def test_leader_reply_batch_lineage_requires_exact_source_run_consensus(
    project_api: ProjectApiEnv,
):
    """Concurrent work-item replies are never guessed into one batch lineage."""
    from app.models.audit import ChatMessage
    from app.models.project import ProjectRun, ProjectWorkItem
    from app.services.subagent_runtime import _resolve_batch_work_item_lineage

    env = project_api
    project = await _create_project(env, name="Exact batch lineage")
    project_id = uuid.UUID(project["id"])
    first_item = ProjectWorkItem(
        tenant_id=env.tenant_id,
        project_id=project_id,
        assignee_agent_id=env.worker_id,
        created_by_agent_id=env.leader_id,
        title="Concurrent item A",
        status="in_progress",
        priority="medium",
        acceptance_criteria=[],
        dependency_ids=[],
    )
    second_item = ProjectWorkItem(
        tenant_id=env.tenant_id,
        project_id=project_id,
        assignee_agent_id=env.worker_id,
        created_by_agent_id=env.leader_id,
        title="Concurrent item B",
        status="in_progress",
        priority="medium",
        acceptance_criteria=[],
        dependency_ids=[],
    )
    env.db.add_all([first_item, second_item])
    await env.db.flush()
    runs = [
        ProjectRun(
            tenant_id=env.tenant_id,
            project_id=project_id,
            work_item_id=work_item_id,
            agent_id=env.worker_id,
            initiated_by_user_id=env.owner_id,
            status="succeeded",
            trigger_type="a2a",
            input={},
            output={},
        )
        for work_item_id in [first_item.id, first_item.id, second_item.id]
    ]
    env.db.add_all(runs)
    await env.db.flush()
    rows = [
        ChatMessage(
            agent_id=env.worker_id,
            sender_agent_id=env.worker_id,
            role="assistant",
            content=f"reply-{index}",
            conversation_id=str(uuid.uuid4()),
            message_meta={"source_project_run_ids": [str(run.id)]},
        )
        for index, run in enumerate(runs)
    ]
    missing = ChatMessage(
        agent_id=env.worker_id,
        sender_agent_id=env.worker_id,
        role="assistant",
        content="reply-without-source",
        conversation_id=str(uuid.uuid4()),
        message_meta={},
    )

    exact, related = await _resolve_batch_work_item_lineage(env.db, project_id, rows[:2])
    assert exact == first_item.id
    assert related == [first_item.id]

    exact, related = await _resolve_batch_work_item_lineage(env.db, project_id, [rows[0], rows[2]])
    assert exact is None
    assert set(related) == {first_item.id, second_item.id}

    exact, related = await _resolve_batch_work_item_lineage(env.db, project_id, [rows[0], missing])
    assert exact is None
    assert related == [first_item.id]

async def test_peer_a2a_completion_only_escalates_decisions_failures_and_owner_requests() -> None:
    from app.services.subagent_runtime import _a2a_completion_leader_policy

    leader_id = uuid.uuid4()
    peer_id = uuid.uuid4()
    informational = SimpleNamespace(
        input={"dispatch": {"a2a": {"from_agent_id": str(peer_id), "mode": "task_delegate"}}},
        work_item_id=None,
    )
    assert await _a2a_completion_leader_policy(
        None,
        project_run=informational,
        leader_agent_id=leader_id,
        failed=False,
    ) == (False, "peer_completion_recorded")

    consult = SimpleNamespace(
        input={"dispatch": {"a2a": {"from_agent_id": str(peer_id), "mode": "consult"}}},
        work_item_id=None,
    )
    assert await _a2a_completion_leader_policy(
        None,
        project_run=consult,
        leader_agent_id=leader_id,
        failed=False,
    ) == (True, "decision_consult")

    owner_requested = SimpleNamespace(
        input={"dispatch": {"a2a": {"from_agent_id": str(leader_id), "mode": "task_delegate"}}},
        work_item_id=None,
    )
    assert (
        await _a2a_completion_leader_policy(
            None,
            project_run=owner_requested,
            leader_agent_id=leader_id,
            failed=False,
        )
    )[0] is True
    assert (
        await _a2a_completion_leader_policy(
            None,
            project_run=informational,
            leader_agent_id=leader_id,
            failed=True,
        )
    )[0] is True

async def test_leader_reply_batch_is_bounded_and_causally_isolated() -> None:
    from app.services.subagent_runtime import (
        PROJECT_LEADER_BATCH_MAX_BYTES,
        PROJECT_LEADER_BATCH_MAX_REPLIES,
        _select_leader_batch_rows,
        _truncate_batch_content,
    )

    first_cause = [
        ChatMessage(id=uuid.uuid4(), content="x" * 100, role="assistant", conversation_id="group")
        for _ in range(PROJECT_LEADER_BATCH_MAX_REPLIES + 2)
    ]
    unrelated = ChatMessage(
        id=uuid.uuid4(),
        content="other",
        role="assistant",
        conversation_id="group",
    )
    rows = [first_cause[0], unrelated, *first_cause[1:]]
    causal_keys = {row.id: "work-item:a" for row in first_cause}
    causal_keys[unrelated.id] = "work-item:b"
    selected = _select_leader_batch_rows(rows, causal_keys)
    assert len(selected) == PROJECT_LEADER_BATCH_MAX_REPLIES
    assert unrelated not in selected

    large_rows = [
        ChatMessage(id=uuid.uuid4(), content="字" * 4_000, role="assistant", conversation_id="group") for _ in range(3)
    ]
    selected_large = _select_leader_batch_rows(
        large_rows,
        {row.id: "cause:one" for row in large_rows},
    )
    assert len(selected_large) == 1
    truncated = _truncate_batch_content("字" * PROJECT_LEADER_BATCH_MAX_BYTES)
    assert len(truncated.encode("utf-8")) <= PROJECT_LEADER_BATCH_MAX_BYTES
