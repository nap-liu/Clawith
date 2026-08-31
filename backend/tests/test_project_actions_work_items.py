from project_actions_support import *  # noqa: F401,F403

async def test_run_work_item_and_milestone_contracts_are_explicit(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.project import ProjectRun
    from app.services.project_runtime_tools import (
        execute_project_runtime_tool,
        execute_project_workspace_tool,
    )

    env = project_api
    project = await _create_project(env, name="Explicit trace contract")
    project_id = project["id"]
    stored_project = await env.db.get(Project, uuid.UUID(project_id))
    assert stored_project is not None
    stored_project.status = "running"
    await env.db.commit()

    item_response = await env.client.post(
        f"/api/projects/{project_id}/work-items",
        json={
            "title": "Ship traced artifact",
            "assignee_agent_id": str(env.leader_id),
            "acceptance_criteria": ["Artifact and milestone are linked"],
        },
    )
    assert item_response.status_code == 201, item_response.text
    item_id = item_response.json()["id"]
    run_response = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"work_item_id": item_id, "agent_id": str(env.leader_id)},
    )
    assert run_response.status_code == 201, run_response.text
    run = run_response.json()
    assert run["work_item_id"] == item_id
    assert run["agent_name"] == "Leader"
    assert run["project_member_id"]
    assert run["member_snapshot"]["name"] == "Leader"
    assert run["session_id"] == run["subagent_session_id"]
    child_id = uuid.UUID(run["subagent_session_id"])
    child_input = (
        await env.db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(child_id),
                ChatMessage.role == "user",
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    assert child_input.message_meta["project_run_id"] == run["id"]
    anchor_id = child_input.id

    await execute_project_runtime_tool(
        "project_update_work_item",
        {
            "work_item_id": item_id,
            "status": "in_progress",
            "progress_note": "Implementation is traceable",
            "evidence": ["deliverables/traced.md"],
        },
        agent_id=env.leader_id,
        execution_user_id=env.owner_id,
        session_id=str(child_id),
        tool_call_id="trace-update",
        turn_anchor_id=anchor_id,
    )
    write_result = json.loads(
        await execute_project_workspace_tool(
            "write_file",
            {
                "workspace": "project",
                "path": "deliverables/traced.md",
                "content": "# Durable evidence\n",
            },
            agent_id=env.leader_id,
            execution_user_id=env.owner_id,
            session_id=str(child_id),
            tool_call_id="trace-file",
            turn_anchor_id=anchor_id,
        )
    )
    stored_run = await env.db.get(ProjectRun, uuid.UUID(run["id"]))
    assert stored_run is not None
    stored_run.status = "succeeded"
    await env.db.commit()
    long_milestone_message = "里程碑完整说明：" + "六个Agent的交付证据、评审结论与回滚锚点均已核验。" * 40
    assert len(long_milestone_message) > 500
    milestone_result = json.loads(
        await execute_project_runtime_tool(
            "project_create_milestone",
            {
                "message": long_milestone_message,
                # Requested milestone paths are operation intent, not proof of
                # an actual change. Both files are clean here, so the empty
                # milestone commit must not make PROJECT.json appear in the
                # work item's code/file change list.
                "paths": ["deliverables/traced.md", "PROJECT.json"],
                "related_work_item_ids": [item_id],
            },
            agent_id=env.leader_id,
            execution_user_id=env.owner_id,
            session_id=str(child_id),
            tool_call_id="trace-milestone",
            turn_anchor_id=anchor_id,
        )
    )

    detail_response = await env.client.get(f"/api/projects/{project_id}/work-items/{item_id}")
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["work_item"]["id"] == item_id
    assert [row["id"] for row in detail["runs"]] == [run["id"]]
    assert detail["sessions"][0] == {
        "run_id": run["id"],
        "project_run_id": run["id"],
        "work_item_id": item_id,
        "agent_id": str(env.leader_id),
        "agent_name": "Leader",
        "status": detail["runs"][0]["status"],
        "source_channel": "subagent",
        "session_intent": "execution",
        "session_id": str(child_id),
        "subagent_session_id": str(child_id),
        "anchor_message_id": str(anchor_id),
    }
    assert {row["path"] for row in detail["files"]} == {"deliverables/traced.md"}
    assert detail["files"][0]["commit"] == write_result["commit"]
    assert {row["commit"] for row in detail["commits"]} >= {
        write_result["commit"],
        milestone_result["commit"],
    }
    milestone_trace = next(row for row in detail["commits"] if row["commit"] == milestone_result["commit"])
    assert milestone_trace["paths"] == []
    assert milestone_trace["diff_available"] is True
    assert any(row["kind"] == "progress_note" for row in detail["evidence"])
    assert any(row["kind"] == "evidence" for row in detail["evidence"])
    traced_events = [
        row
        for row in detail["events"]
        if row["event_type"] in {"work_item.updated", "project.file.committed", "git.milestone.created"}
    ]
    assert all(row["run_id"] == run["id"] for row in traced_events)
    assert all(row["work_item_id"] == item_id for row in traced_events)

    # A durable Leader child is reused across work items. Session equality is
    # only a conversation entry point and must never pull another work item's
    # Run, events, commits, files, or evidence into this detail contract.
    other_item_response = await env.client.post(
        f"/api/projects/{project_id}/work-items",
        json={"title": "Unrelated work", "assignee_agent_id": str(env.leader_id)},
    )
    assert other_item_response.status_code == 201
    other_item_id = other_item_response.json()["id"]
    unrelated_run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=uuid.UUID(project_id),
        work_item_id=uuid.UUID(other_item_id),
        agent_id=env.leader_id,
        initiated_by_user_id=env.owner_id,
        status="succeeded",
        trigger_type="manual",
        output={"subagent_session_id": str(child_id)},
    )
    env.db.add(unrelated_run)
    await env.db.flush()
    foreign_commit = "f" * 40
    env.db.add(
        ProjectEvent(
            tenant_id=env.tenant_id,
            project_id=uuid.UUID(project_id),
            work_item_id=uuid.UUID(other_item_id),
            run_id=unrelated_run.id,
            actor_agent_id=env.leader_id,
            event_type="foreign.work.evidence",
            summary="Must stay with the other work item",
            event_metadata={
                "project_run_id": str(unrelated_run.id),
                "session_id": str(child_id),
                "subagent_session_id": str(child_id),
                "commit": foreign_commit,
                "path": "foreign/only.md",
                "evidence": ["foreign-evidence"],
            },
        )
    )
    await env.db.commit()
    detail_again = (await env.client.get(f"/api/projects/{project_id}/work-items/{item_id}")).json()
    assert [row["id"] for row in detail_again["runs"]] == [run["id"]]
    assert all(row["event_type"] != "foreign.work.evidence" for row in detail_again["events"])
    assert all(row["commit"] != foreign_commit for row in detail_again["commits"])
    assert all(row["path"] != "foreign/only.md" for row in detail_again["files"])
    assert all(row["value"] != "foreign-evidence" for row in detail_again["evidence"])
    assert len(detail_again["sessions"]) == 1

    milestones_response = await env.client.get(f"/api/projects/{project_id}/milestones")
    assert milestones_response.status_code == 200, milestones_response.text
    milestone = milestones_response.json()[0]
    assert milestone["commit"] == milestone_result["commit"]
    assert milestone["run_id"] == run["id"]
    assert milestone["work_item_id"] == item_id
    assert milestone["session_id"] == str(child_id)
    assert milestone["subagent_session_id"] == str(child_id)
    assert milestone["agent_name"] == "Leader"
    assert milestone["related_run_ids"] == [run["id"]]
    assert milestone["related_work_item_ids"] == [item_id]
    assert milestone["message"] == long_milestone_message
    milestone_event = await env.db.get(ProjectEvent, uuid.UUID(milestone_result["event_id"]))
    assert milestone_event is not None
    assert milestone_event.event_type == "git.milestone.created"
    assert len(milestone_event.summary) <= 500
    assert milestone_event.event_metadata["milestone_message"] == long_milestone_message
    assert milestone_event.event_metadata["description"] == long_milestone_message

    # A repository commit can win immediately before the metadata transaction
    # fails.  The durable prepared event and Git operation trailer let a retry
    # finalize that same semantic operation without another empty commit.
    from app.services import project_runtime_tools

    commit_attempts = {"value": 0}

    class FailSecondCommitSession:
        def __init__(self):
            self._session = env.session_factory()

        async def __aenter__(self):
            await self._session.__aenter__()
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return await self._session.__aexit__(exc_type, exc, traceback)

        def __getattr__(self, name):
            return getattr(self._session, name)

        async def commit(self):
            commit_attempts["value"] += 1
            if commit_attempts["value"] == 2:
                await self._session.rollback()
                raise RuntimeError("injected metadata commit failure")
            await self._session.commit()

    recovery_arguments = {
        "message": "Recover this semantic milestone after DB failure",
        "related_work_item_ids": [item_id],
    }
    monkeypatch.setattr(project_runtime_tools, "async_session", FailSecondCommitSession)
    with pytest.raises(RuntimeError, match="injected metadata commit failure"):
        await execute_project_runtime_tool(
            "project_create_milestone",
            recovery_arguments,
            agent_id=env.leader_id,
            execution_user_id=env.owner_id,
            session_id=str(child_id),
            tool_call_id="recovery-first-call",
            turn_anchor_id=anchor_id,
        )

    repo = project_repo_path(env.tenant_id, uuid.UUID(project_id))
    operation_commits_after_failure = subprocess.check_output(
        ["git", "-C", str(repo), "log", "--format=%H", "--fixed-strings", "--grep=Project-Milestone-Operation:"],
        text=True,
    ).splitlines()
    assert len(operation_commits_after_failure) == 2

    late_success = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=uuid.UUID(project_id),
        work_item_id=uuid.UUID(item_id),
        agent_id=env.leader_id,
        initiated_by_user_id=env.owner_id,
        status="succeeded",
        trigger_type="retry",
        output={"subagent_session_id": str(child_id)},
    )
    env.db.add(late_success)
    await env.db.commit()

    monkeypatch.setattr(project_runtime_tools, "async_session", env.session_factory)
    recovered_result = json.loads(
        await execute_project_runtime_tool(
            "project_create_milestone",
            recovery_arguments,
            agent_id=env.leader_id,
            execution_user_id=env.owner_id,
            session_id=str(child_id),
            # A regenerated model tool call has another tool_call_id; semantic
            # milestone identity must still recover the original operation.
            tool_call_id="recovery-regenerated-call",
            turn_anchor_id=anchor_id,
        )
    )
    assert recovered_result["idempotent_replay"] is True
    assert recovered_result["commit"] == operation_commits_after_failure[0]
    operation_commits_after_retry = subprocess.check_output(
        ["git", "-C", str(repo), "log", "--format=%H", "--fixed-strings", "--grep=Project-Milestone-Operation:"],
        text=True,
    ).splitlines()
    assert operation_commits_after_retry == operation_commits_after_failure
    recovered_events = (
        (
            await env.db.execute(
                select(ProjectEvent).where(
                    ProjectEvent.project_id == uuid.UUID(project_id),
                    ProjectEvent.event_type.in_(["git.milestone.prepared", "git.milestone.created"]),
                )
            )
        )
        .scalars()
        .all()
    )
    operation_keys = [
        event.event_metadata.get("milestone_operation_key")
        for event in recovered_events
        if event.event_metadata.get("milestone_message") == recovery_arguments["message"]
    ]
    assert len(operation_keys) == 1
    assert (
        next(
            event
            for event in recovered_events
            if event.event_metadata.get("milestone_message") == recovery_arguments["message"]
        ).event_type
        == "git.milestone.created"
    )
    recovered_event = next(
        event
        for event in recovered_events
        if event.event_metadata.get("milestone_message") == recovery_arguments["message"]
    )
    assert set(recovered_event.event_metadata["related_run_ids"]) == {run["id"], str(late_success.id)}
    assert not subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip()

    with pytest.raises(ValueError, match=r"2 unfinished"):
        await execute_project_runtime_tool(
            "project_set_status",
            {"status": "completed", "reason": "Must not skip unfinished work"},
            agent_id=env.leader_id,
            execution_user_id=env.owner_id,
            session_id=str(child_id),
            tool_call_id="trace-premature-complete",
            turn_anchor_id=anchor_id,
        )

    work_items = (
        (
            await env.db.execute(
                select(ProjectWorkItem).where(ProjectWorkItem.project_id == uuid.UUID(project_id))
            )
        )
        .scalars()
        .all()
    )
    for work_item in work_items:
        work_item.status = "done"
    await env.db.commit()

    completed = json.loads(
        await execute_project_runtime_tool(
            "project_set_status",
            {"status": "completed", "reason": "Acceptance evidence is committed"},
            agent_id=env.leader_id,
            execution_user_id=env.owner_id,
            session_id=str(child_id),
            tool_call_id="trace-complete",
            turn_anchor_id=anchor_id,
        )
    )
    assert completed["status"] == "completed"
    env.db.expire(stored_project)
    refreshed_project = (await env.client.get(f"/api/projects/{project_id}")).json()
    assert refreshed_project["status"] == "completed"
    project_events = (await env.client.get(f"/api/projects/{project_id}/events?limit=200")).json()
    status_event = next(row for row in project_events if row["event_type"] == "project.status.updated")
    assert status_event["run_id"] == run["id"]
    assert status_event["event_metadata"]["after"] == "completed"
