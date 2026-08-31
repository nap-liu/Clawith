from project_actions_support import *  # noqa: F401,F403

async def test_project_a2a_delivery_returns_scoped_session_identifiers(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.chat_session import ChatSession
    from app.models.project import ProjectRun
    from app.services import agent_tools, project_service

    env = project_api
    project = await _create_project(env, name="A2A session identity")
    await _mark_project_running(env, project["id"])
    work_item = await _create_work_item(
        env,
        project["id"],
        title="Resolve exact project thread",
        assignee_agent_id=env.reviewer_id,
    )
    queued = await env.client.post(
        f"/api/projects/{project['id']}/a2a",
        json={
            "from_agent_id": str(env.worker_id),
            "to_agent_id": str(env.reviewer_id),
            "title": "Resolve exact project thread",
            "message": "Return the exact project thread",
            "mode": "consult",
            "expected_output": "The exact scoped session identifier",
            "work_item_id": work_item["id"],
        },
    )
    assert queued.status_code == 202, queued.text
    queued_body = queued.json()

    async def scoped_sender(from_agent_id, args, **_kwargs):
        project_id = uuid.UUID(args["_project_id"])
        target_id = uuid.UUID(args["agent_id"])
        access_id = min(from_agent_id, target_id, key=str)
        peer_id = max(from_agent_id, target_id, key=str)
        async with env.session_factory() as delivery_db:
            delivery_db.add(
                ChatSession(
                    project_id=project_id,
                    agent_id=access_id,
                    peer_agent_id=peer_id,
                    source_channel="agent",
                    title="Worker ↔ Reviewer",
                    external_conv_id=f"project-a2a:{project_id}:{peer_id}",
                )
            )
            await delivery_db.commit()
        return "✅ Notification sent"

    monkeypatch.setattr(project_service, "async_session", env.session_factory)
    monkeypatch.setattr(agent_tools, "_send_message_to_agent", scoped_sender)
    await project_service.deliver_project_a2a(uuid.UUID(queued_body["run_id"]))

    env.db.expire_all()
    run = await env.db.get(ProjectRun, uuid.UUID(queued_body["run_id"]))
    assert run is not None and run.status == "succeeded"
    assert run.output["group_session_id"] == queued_body["group_session_id"]
    assert run.output["session_id"]
    assert run.output["session_agent_id"] == run.output["session_access_agent_id"]
    assert run.output["session_title"] == "Worker ↔ Reviewer"
    delivered = (
        await env.db.execute(
            select(ProjectEvent).where(
                ProjectEvent.run_id == run.id,
                ProjectEvent.event_type == "a2a.delivered",
            )
        )
    ).scalar_one()
    assert delivered.event_metadata["session_id"] == run.output["session_id"]
    assert delivered.event_metadata["group_session_id"] == queued_body["group_session_id"]

async def test_project_a2a_native_receipt_keeps_exact_session_when_pair_has_newer_thread(
    project_api: ProjectApiEnv,
):
    from app.models.chat_session import ChatSession
    from app.models.subagent_run import SubagentRun
    from app.services import project_service

    env = project_api
    project = await _create_project(env, name="Exact concurrent A2A receipt")
    project_id = uuid.UUID(project["id"])
    project_run_id = uuid.uuid4()
    access_id = min(env.worker_id, env.reviewer_id, key=str)
    peer_id = max(env.worker_id, env.reviewer_id, key=str)
    exact_session = ChatSession(
        project_id=project_id,
        agent_id=access_id,
        peer_agent_id=peer_id,
        source_channel="agent",
        title="Exact earlier thread",
        external_conv_id=f"a2a-{uuid.uuid4().hex}",
    )
    newer_session = ChatSession(
        project_id=project_id,
        agent_id=access_id,
        peer_agent_id=peer_id,
        source_channel="agent",
        title="Unrelated newer thread",
        external_conv_id=f"a2a-{uuid.uuid4().hex}",
    )
    child_id = uuid.uuid4()
    child_session = ChatSession(
        id=child_id,
        project_id=project_id,
        agent_id=env.reviewer_id,
        source_channel="subagent",
        title="Exact child execution",
    )
    env.db.add_all([exact_session, newer_session, child_session])
    await env.db.flush()
    env.db.add(
        SubagentRun(
            id=child_id,
            parent_session_id=exact_session.id,
            project_id=project_id,
            execution_user_id=env.owner_id,
            origin_tool_call_id=f"test-exact-{uuid.uuid4()}",
            mode="run",
            status="queued",
        )
    )
    await env.db.commit()

    receipt = json.dumps(
        {
            "status": "queued",
            "session_id": str(exact_session.id),
            "a2a_session_id": str(exact_session.id),
            "project_run_id": str(project_run_id),
            "subagent_run_id": str(child_id),
            "subagent_session_id": str(child_id),
        }
    )
    session_info, error = await project_service._resolve_project_a2a_session_info(
        env.db,
        project_id=project_id,
        project_run_id=project_run_id,
        source_agent_id=env.worker_id,
        target_agent_id=env.reviewer_id,
        result=receipt,
    )

    assert error is None
    assert session_info["session_id"] == str(exact_session.id)
    assert session_info["session_id"] != str(newer_session.id)
    assert session_info["subagent_session_id"] == str(child_id)

async def test_project_a2a_native_receipt_rejects_forged_scope_without_latest_fallback(
    project_api: ProjectApiEnv,
):
    from app.models.chat_session import ChatSession
    from app.models.subagent_run import SubagentRun
    from app.services import project_service

    env = project_api
    project = await _create_project(env, name="Reject forged A2A receipt")
    project_id = uuid.UUID(project["id"])
    project_run_id = uuid.uuid4()
    access_id = min(env.worker_id, env.reviewer_id, key=str)
    peer_id = max(env.worker_id, env.reviewer_id, key=str)
    valid_latest = ChatSession(
        project_id=project_id,
        agent_id=access_id,
        peer_agent_id=peer_id,
        source_channel="agent",
        title="Valid latest thread",
        external_conv_id=f"a2a-{uuid.uuid4().hex}",
    )
    forged_parent = ChatSession(
        project_id=project_id,
        agent_id=min(env.leader_id, env.reviewer_id, key=str),
        peer_agent_id=max(env.leader_id, env.reviewer_id, key=str),
        source_channel="agent",
        title="Wrong project member pair",
        external_conv_id=f"a2a-{uuid.uuid4().hex}",
    )
    forged_child_id = uuid.uuid4()
    forged_child = ChatSession(
        id=forged_child_id,
        project_id=project_id,
        agent_id=env.reviewer_id,
        source_channel="subagent",
        title="Wrong parent child",
    )
    env.db.add_all([valid_latest, forged_parent, forged_child])
    await env.db.flush()
    env.db.add(
        SubagentRun(
            id=forged_child_id,
            parent_session_id=forged_parent.id,
            project_id=project_id,
            execution_user_id=env.owner_id,
            origin_tool_call_id=f"test-forged-{uuid.uuid4()}",
            mode="run",
            status="queued",
        )
    )
    await env.db.commit()

    forged_receipt = json.dumps(
        {
            "status": "queued",
            "session_id": str(forged_parent.id),
            "a2a_session_id": str(forged_parent.id),
            "project_run_id": str(project_run_id),
            "subagent_run_id": str(forged_child_id),
            "subagent_session_id": str(forged_child_id),
        }
    )
    session_info, error = await project_service._resolve_project_a2a_session_info(
        env.db,
        project_id=project_id,
        project_run_id=project_run_id,
        source_agent_id=env.worker_id,
        target_agent_id=env.reviewer_id,
        result=forged_receipt,
    )

    assert session_info == {}
    assert error == "Project A2A transport receipt references an invalid collaboration session"

    legacy_info, legacy_error = await project_service._resolve_project_a2a_session_info(
        env.db,
        project_id=project_id,
        project_run_id=project_run_id,
        source_agent_id=env.worker_id,
        target_agent_id=env.reviewer_id,
        result="✅ Legacy transport delivered",
    )
    assert legacy_error is None
    assert legacy_info["session_id"] == str(valid_latest.id)
