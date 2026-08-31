from project_actions_support import *  # noqa: F401,F403

async def test_project_a2a_uses_durable_project_child_and_exact_standard_timeline(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    """A→B stays single-target while B retains project tools and exact trace."""
    from datetime import timedelta

    from app.models.chat_session import ChatSession
    from app.models.project import ProjectEvent, ProjectMemberSnapshot, ProjectRun, ProjectWorkItem
    from app.models.subagent_run import SubagentRun
    from app.services import agent_tools, project_runtime_tools, project_service, subagent_runtime
    from app.services.project_service import freeze_run_members

    env = project_api
    monkeypatch.setattr(agent_tools, "async_session", env.session_factory)
    monkeypatch.setattr(project_service, "async_session", env.session_factory)
    project = await _create_project(env, name="Durable exact project A2A")
    project_id = uuid.UUID(project["id"])
    await _mark_project_running(env, project_id)

    dependency = ProjectWorkItem(
        tenant_id=env.tenant_id,
        project_id=project_id,
        assignee_agent_id=env.leader_id,
        created_by_agent_id=env.leader_id,
        title="Approve the evidence contract",
        description="Define the required evidence before implementation",
        status="done",
        priority="medium",
        acceptance_criteria=["Evidence contract approved"],
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
        title="Write A2A evidence",
        description="B must update this item inside its project runtime",
        status="todo",
        priority="medium",
        acceptance_criteria=["Committed evidence exists", "Evidence cites its source"],
        dependency_ids=[str(dependency_id)],
    )
    parallel_work_item = ProjectWorkItem(
        tenant_id=env.tenant_id,
        project_id=project_id,
        assignee_agent_id=env.reviewer_id,
        created_by_agent_id=env.leader_id,
        title="Review parallel A2A evidence",
        description="A concurrent delegation must preserve this exact work-item lineage",
        status="todo",
        priority="medium",
        acceptance_criteria=["Review evidence exists"],
        dependency_ids=[],
    )
    env.db.add_all([work_item, parallel_work_item])
    await env.db.flush()
    env.db.add(
        ProjectEvent(
            tenant_id=env.tenant_id,
            project_id=project_id,
            work_item_id=work_item.id,
            actor_agent_id=env.leader_id,
            event_type="work_item.updated",
            summary="Initial evidence attached",
            event_metadata={"evidence": ["docs/evidence-contract.md", "commit-contract-1234"]},
        )
    )
    await env.db.commit()
    work_item_id = work_item.id
    parallel_work_item_id = parallel_work_item.id

    parent_run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        work_item_id=work_item_id,
        agent_id=env.leader_id,
        initiated_by_user_id=env.owner_id,
        status="running",
        trigger_type="manual",
        input={"title": "Coordinate A2A evidence"},
        output={},
    )
    env.db.add(parent_run)
    await env.db.flush()
    project_row = await env.db.get(Project, project_id)
    assert project_row is not None
    await freeze_run_members(env.db, project_row, parent_run)
    await env.db.commit()

    raw_result = await agent_tools._send_message_to_agent(
        env.leader_id,
        {
            "agent_id": str(env.worker_id),
            "message": "Write docs/a2a-evidence.md and mark the assigned item done.",
            "msg_type": "task_delegate",
            "force_async": True,
            "_project_id": str(project_id),
            "_parent_project_run_id": str(parent_run.id),
        },
        user_id=env.owner_id,
        origin_session_id=None,
        tool_call_id="leader-a2a-worker-1",
    )
    result = json.loads(raw_result)
    assert result["status"] == "queued"
    assert result["awakened_agent_ids"] == [str(env.worker_id)]
    a2a_session_id = uuid.UUID(result["a2a_session_id"])
    child_id = uuid.UUID(result["subagent_session_id"])
    project_run_id = uuid.UUID(result["project_run_id"])

    a2a_session = await env.db.get(ChatSession, a2a_session_id)
    child = await env.db.get(ChatSession, child_id)
    durable = await env.db.get(SubagentRun, child_id)
    run = await env.db.get(ProjectRun, project_run_id)
    assert a2a_session is not None and a2a_session.source_channel == "agent"
    assert a2a_session.project_id == project_id
    assert child is not None and child.source_channel == "subagent"
    assert child.project_id == project_id and child.agent_id == env.worker_id
    assert durable is not None and durable.parent_session_id == a2a_session_id
    assert durable.project_member_id is not None
    assert run is not None and run.status in {"queued", "running"}
    assert run.work_item_id == work_item_id
    assert run.input["parent_project_run_id"] == str(parent_run.id)
    assert run.input["title"] == "Coordinate A2A evidence"
    assert run.input["work_item_snapshot"] == {
        "id": str(work_item_id),
        "title": "Write A2A evidence",
        "description": "B must update this item inside its project runtime",
        "status": "todo",
        "acceptance_criteria": ["Committed evidence exists", "Evidence cites its source"],
        "dependencies": [
            {
                "id": str(dependency_id),
                "title": "Approve the evidence contract",
                "status": "done",
            }
        ],
        "evidence": ["docs/evidence-contract.md", "commit-contract-1234"],
    }

    # Later edits must not rewrite the exact context frozen for this A2A turn.
    current_work_item = await env.db.get(ProjectWorkItem, work_item_id)
    assert current_work_item is not None
    current_work_item.dependency_ids = []
    await env.db.commit()
    env.db.expire_all()
    persisted_run = await env.db.get(ProjectRun, project_run_id)
    assert persisted_run is not None
    assert persisted_run.input["work_item_snapshot"]["dependencies"][0]["id"] == str(dependency_id)
    run = persisted_run

    assert run.output["session_id"] == str(a2a_session_id)
    assert run.output["subagent_session_id"] == str(child_id)

    serialized_runs = (await env.client.get(f"/api/projects/{project_id}/runs")).json()
    serialized_run = next(row for row in serialized_runs if row["id"] == str(run.id))
    assert serialized_run["work_item_id"] == str(work_item_id)
    assert serialized_run["title"] == "Coordinate A2A evidence"

    tool_names = {
        item["function"]["name"]
        for item in await subagent_runtime.prepare_subagent_tools(
            env.worker_id,
            child_id,
            execution_user_id=env.owner_id,
        )
    }
    assert {"write_file", "project_update_work_item", "project_message_agent"} <= tool_names

    write_result = json.loads(
        await project_runtime_tools.execute_project_workspace_tool(
            "write_file",
            {
                "workspace": "project",
                "path": "docs/a2a-evidence.md",
                "content": "# Exact A2A evidence\n",
            },
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
            session_id=str(child_id),
            tool_call_id="worker-write-a2a-evidence",
            turn_anchor_id=None,
        )
    )
    assert write_result["commit"]
    await project_runtime_tools.execute_project_runtime_tool(
        "project_update_work_item",
        {
            "work_item_id": str(work_item_id),
            "status": "done",
            "progress_note": "Evidence committed through project A2A child",
            "evidence": ["docs/a2a-evidence.md", write_result["commit"]],
        },
        agent_id=env.worker_id,
        execution_user_id=env.owner_id,
        session_id=str(child_id),
        tool_call_id="worker-finish-a2a-item",
        turn_anchor_id=None,
    )

    child_input = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(child_id),
                ChatMessage.message_meta["project_run_id"].as_string() == str(project_run_id),
            )
        )
    ).scalar_one()
    assert "Immutable work-item snapshot" in child_input.content
    assert "Evidence cites its source" in child_input.content
    assert "Approve the evidence contract" in child_input.content
    assert "docs/evidence-contract.md" in child_input.content

    message_schema = project_runtime_tools.PROJECT_TOOL_REGISTRY["project_message_agent"]["function"]
    assert message_schema["parameters"]["properties"]["mode"]["enum"] == ["task_delegate", "consult"]
    assert set(message_schema["parameters"]["required"]) == {
        "agent_id",
        "title",
        "message",
        "mode",
        "expected_output",
    }
    assert message_schema["description"] == (
        "Send one active project member a review request or assigned task. Include the relevant context, requested "
        "work, expected result, and related work item when one exists."
    )
    assert message_schema["parameters"]["properties"]["mode"]["description"] == (
        "Choose task_delegate for assigned work and consult for a review or decision."
    )
    with pytest.raises(ValueError, match="成员协作请求需要明确任务或咨询内容。"):
        await project_runtime_tools.execute_project_runtime_tool(
            "project_message_agent",
            {
                "agent_id": str(env.reviewer_id),
                "message": "FYI: evidence is ready; acknowledge receipt and wait.",
                "mode": "notify",
            },
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
            session_id=str(child_id),
            tool_call_id="worker-passive-notify-blocked",
            turn_anchor_id=child_input.id,
        )
    with pytest.raises(ValueError, match="成员协作请求需要填写标题。"):
        await project_runtime_tools.execute_project_runtime_tool(
            "project_message_agent",
            {
                "agent_id": str(env.reviewer_id),
                "message": "Evaluate the committed evidence and return a decision with cited findings.",
                "mode": "task_delegate",
                "expected_output": "A cited pass/fail decision.",
            },
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
            session_id=str(child_id),
            tool_call_id="worker-untitled-delegation-blocked",
            turn_anchor_id=child_input.id,
        )
    with pytest.raises(ValueError, match="成员协作请求需要包含可执行的工作内容和预期结果。"):
        await project_runtime_tools.execute_project_runtime_tool(
            "project_message_agent",
            {
                "agent_id": str(env.reviewer_id),
                "title": "Progress notification",
                "message": "已完成，已更新工作项，请收到后等待。",
                "mode": "consult",
                "expected_output": "Acknowledge receipt.",
            },
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
            session_id=str(child_id),
            tool_call_id="worker-mechanical-handoff-blocked",
            turn_anchor_id=child_input.id,
        )

    delegated_result = json.loads(
        await project_runtime_tools.execute_project_runtime_tool(
            "project_message_agent",
            {
                "agent_id": str(env.reviewer_id),
                "title": "Review A2A evidence",
                "message": "Review the committed A2A evidence against acceptance criteria.",
                "mode": "task_delegate",
                "expected_output": "A pass/fail decision with exact evidence references.",
            },
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
            session_id=str(child_id),
            tool_call_id="worker-delegate-a2a-review",
            turn_anchor_id=child_input.id,
        )
    )
    delegated_run = await env.db.get(
        ProjectRun,
        uuid.UUID(delegated_result["project_run_id"]),
    )
    assert delegated_run is not None
    assert delegated_run.work_item_id == work_item_id
    assert delegated_run.input["parent_project_run_id"] == str(run.id)
    assert delegated_run.input["title"] == "Review A2A evidence"

    explicit_result = json.loads(
        await project_runtime_tools.execute_project_runtime_tool(
            "project_message_agent",
            {
                "agent_id": str(env.reviewer_id),
                "work_item_id": str(parallel_work_item_id),
                "title": "Review the parallel evidence stream",
                "message": "Review only the evidence for the explicitly referenced parallel work item.",
                "mode": "task_delegate",
                "expected_output": "An independent review decision for the parallel work item.",
                "new_conversation": True,
            },
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
            session_id=str(child_id),
            tool_call_id="worker-delegate-explicit-parallel-item",
            turn_anchor_id=child_input.id,
        )
    )
    explicit_run = await env.db.get(
        ProjectRun,
        uuid.UUID(explicit_result["project_run_id"]),
    )
    assert explicit_run is not None
    assert explicit_run.work_item_id == parallel_work_item_id
    assert explicit_run.input["parent_project_run_id"] == str(run.id)
    assert explicit_run.input["work_item_id"] == str(parallel_work_item_id)

    child_input.message_meta = {
        **dict(child_input.message_meta or {}),
        "subagent_input_state": "processing",
        "subagent_turn_anchor_id": str(child_input.id),
        "turn_status": "running",
    }
    durable.status = "running"
    durable.lease_owner = subagent_runtime.settings.INSTANCE_ID
    base_time = child_input.created_at + timedelta(seconds=1)
    env.db.add_all(
        [
            ChatMessage(
                agent_id=env.worker_id,
                sender_agent_id=env.worker_id,
                role="assistant",
                content="",
                thinking="I should write the evidence before completing the item.",
                conversation_id=str(child_id),
                message_meta={"turn_anchor_id": str(child_input.id)},
                created_at=base_time,
            ),
            ChatMessage(
                agent_id=env.worker_id,
                sender_agent_id=env.worker_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "write_file",
                        "call_id": "worker-write-a2a-evidence",
                        "args": {"workspace": "project", "path": "docs/a2a-evidence.md"},
                        "status": "done",
                        "result": write_result,
                    }
                ),
                conversation_id=str(child_id),
                message_meta={"turn_anchor_id": str(child_input.id)},
                created_at=base_time + timedelta(seconds=1),
            ),
        ]
    )
    await env.db.commit()

    assert (
        await subagent_runtime._finish_subagent_turn(
            run_id=child_id,
            anchor_id=child_input.id,
            reply="Evidence committed and assigned work item completed.",
            failed=False,
        )
        is True
    )
    completion = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(child_id),
                ChatMessage.message_meta["kind"].as_string() == "subagent_completion",
            )
        )
    ).scalar_one()
    assert completion.message_meta["subagent_wake"] is True
    assert await subagent_runtime._dispatch_parent_event(completion.id) is True

    visible_rows = (
        (
            await env.db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == str(a2a_session_id))
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        )
        .scalars()
        .all()
    )
    assert visible_rows[0].sender_agent_id == env.leader_id
    assert visible_rows[0].message_meta["target_agent_id"] == str(env.worker_id)
    assert any(row.thinking for row in visible_rows)
    assert any(row.role == "tool_call" for row in visible_rows)
    final_reply = next(row for row in visible_rows if row.content.startswith("Evidence committed"))
    assert final_reply.role == "assistant"
    assert final_reply.sender_agent_id == env.worker_id
    assert final_reply.message_meta["a2a_session_id"] == str(a2a_session_id)
    assert final_reply.message_meta["subagent_session_id"] == str(child_id)

    # An exact peer-to-peer A2A reply remains visible in its own standard Chat
    # Session and also enters the durable project coordination queue.  It must
    # not directly resume/broadcast; the existing batch dispatcher gives the
    # Leader one coalesced follow-up turn.
    group_session = (
        await env.db.execute(
            select(ChatSession).where(
                ChatSession.project_id == project_id,
                ChatSession.source_channel == "project",
            )
        )
    ).scalar_one()
    group_reply = (
        await env.db.execute(
            select(ChatMessage).where(ChatMessage.external_event_key == f"project-a2a-group-reply:{completion.id}")
        )
    ).scalar_one()
    assert group_reply.conversation_id == str(group_session.id)
    assert group_reply.sender_agent_id == env.worker_id
    assert group_reply.message_meta["source_a2a_session_id"] == str(a2a_session_id)
    assert group_reply.message_meta["leader_batch_state"] == "pending"
    group_reply_id = group_reply.id
    assert (
        await subagent_runtime._dispatch_project_leader_batch(
            group_session.id,
            debounce_seconds=0,
        )
        is True
    )
    env.db.expire_all()
    delivered_group_reply = await env.db.get(ChatMessage, group_reply_id)
    assert delivered_group_reply is not None
    assert delivered_group_reply.message_meta["leader_batch_state"] == "delivered"

    completed_run = await env.db.get(ProjectRun, project_run_id)
    completed_item = await env.db.get(ProjectWorkItem, work_item_id)
    assert completed_run is not None and completed_run.status == "succeeded"
    assert completed_run.output["session_id"] == str(a2a_session_id)
    assert completed_run.output["subagent_session_id"] == str(child_id)
    assert completed_item is not None and completed_item.status == "done"
    terminal_event = (
        await env.db.execute(
            select(ProjectEvent).where(
                ProjectEvent.run_id == project_run_id,
                ProjectEvent.event_type == "run.succeeded",
            )
        )
    ).scalar_one()
    assert terminal_event.event_metadata["session_id"] == str(a2a_session_id)
    assert terminal_event.event_metadata["subagent_session_id"] == str(child_id)

    failure_run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.worker_id,
        initiated_by_user_id=env.owner_id,
        status="queued",
        trigger_type="a2a",
        input={"message": "Exercise the failed terminal audit contract"},
        output={"session_id": str(a2a_session_id)},
    )
    env.db.add(failure_run)
    await env.db.flush()
    project_row = await env.db.get(Project, project_id)
    assert project_row is not None
    await freeze_run_members(env.db, project_row, failure_run)
    await env.db.commit()
    failure_run_id = failure_run.id
    await subagent_runtime.append_subagent_message(
        agent_id=env.worker_id,
        parent_session_id=str(a2a_session_id),
        subagent_id=str(child_id),
        message="This test turn intentionally fails.",
        execution_user_id=env.owner_id,
        origin_tool_call_id="worker-a2a-failure-audit",
        project_run_id=failure_run_id,
        input_metadata={"project_a2a": True, "a2a_session_id": str(a2a_session_id)},
    )
    failed_input = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(child_id),
                ChatMessage.message_meta["project_run_id"].as_string() == str(failure_run_id),
            )
        )
    ).scalar_one()
    failed_input.message_meta = {
        **dict(failed_input.message_meta or {}),
        "subagent_input_state": "processing",
        "subagent_turn_anchor_id": str(failed_input.id),
        "turn_status": "running",
    }
    durable = await env.db.get(SubagentRun, child_id)
    assert durable is not None
    durable.status = "running"
    durable.lease_owner = subagent_runtime.settings.INSTANCE_ID
    await env.db.commit()
    assert (
        await subagent_runtime._finish_subagent_turn(
            run_id=child_id,
            anchor_id=failed_input.id,
            reply="Intentional project A2A failure",
            failed=True,
        )
        is True
    )
    failed_terminal_event = (
        await env.db.execute(
            select(ProjectEvent).where(
                ProjectEvent.run_id == failure_run_id,
                ProjectEvent.event_type == "run.failed",
            )
        )
    ).scalar_one()
    assert failed_terminal_event.event_metadata["session_id"] == str(a2a_session_id)
    assert failed_terminal_event.event_metadata["subagent_session_id"] == str(child_id)

    worker_member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project_id,
                ProjectMemberSnapshot.agent_id == env.worker_id,
            )
        )
    ).scalar_one()
    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    await project_service.deactivate_project_member(
        env.db,
        stored_project,
        worker_member,
        actor_user_id=env.owner_id,
        reason="A2A membership regression",
    )
    await env.db.commit()
    rejected = await agent_tools._send_message_to_agent(
        env.leader_id,
        {
            "agent_id": str(env.worker_id),
            "message": "This departed member must not wake.",
            "msg_type": "notify",
            "force_async": True,
            "_project_id": str(project_id),
        },
        user_id=env.owner_id,
        tool_call_id="leader-a2a-departed-worker",
    )
    rejection_payload = json.loads(rejected)
    assert rejection_payload["status"] == "error"
    assert rejection_payload["code"] == "project_member_inactive"
