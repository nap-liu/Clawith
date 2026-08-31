from project_actions_support import *  # noqa: F401,F403

async def test_project_standard_file_tools_use_shared_git_without_project_specific_size_limits(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.services.agent_runtime_workspace import (
        bind_agent_runtime_workspace,
        project_agent_runtime_workspace,
    )
    from app.services.agent_tools import execute_tool

    env = project_api
    project = await _create_project(env, name="Standard project files")
    project_id = project["id"]
    await _mark_project_running(env, project_id)
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    wake = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Prepare project files", "mentions": [str(env.worker_id)]},
    )
    assert wake.status_code == 201, wake.text
    child_id = next(
        row["session_id"]
        for row in wake.json()["subagent_runs"]
        if row["agent_id"] == str(env.worker_id)
    )
    runtime_workspace = project_agent_runtime_workspace(
        agent_id=env.worker_id,
        tenant_id=env.tenant_id,
        project_id=uuid.UUID(project_id),
    )

    with bind_agent_runtime_workspace(runtime_workspace):
        missing_workspace = await execute_tool(
            "list_files",
            {"path": ""},
            env.worker_id,
            env.owner_id,
            session_id=child_id,
            tool_call_id="project-file-missing-workspace",
            skip_autonomy=True,
        )
    assert "require workspace='agent' or workspace='project'" in missing_workspace

    async def run(tool_name: str, arguments: dict[str, Any], *, workspace: str = "project") -> str:
        with bind_agent_runtime_workspace(runtime_workspace):
            return await execute_tool(
                tool_name,
                {"workspace": workspace, **arguments},
                env.worker_id,
                env.owner_id,
                session_id=child_id,
                tool_call_id=f"project-file-{tool_name}-{uuid.uuid4().hex[:8]}",
                skip_autonomy=True,
            )

    repo = project_repo_path(env.tenant_id, uuid.UUID(project_id))
    head_before_workspace_reads = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    async def deny_project_write(*_args, **_kwargs):
        return {"allowed": False, "level": "L2", "message": "project write denied"}

    from app.services.autonomy_service import autonomy_service

    monkeypatch.setattr(autonomy_service, "check_and_enforce", deny_project_write)
    with bind_agent_runtime_workspace(runtime_workspace):
        denied_by_autonomy = await execute_tool(
            "write_file",
            {"workspace": "project", "path": "blocked.txt", "content": "must not exist"},
            env.worker_id,
            env.owner_id,
            session_id=child_id,
            tool_call_id="project-file-autonomy-denied",
        )
    assert "project write denied" in denied_by_autonomy
    assert not (repo / "blocked.txt").exists()

    private_listing = await run("list_files", {"path": ""}, workspace="agent")
    assert "PROJECT.json" not in private_listing
    project_root = await run("list_files", {"path": ""})
    assert "PROJECT.json" in project_root
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == head_before_workspace_reads

    large_content = "project file content\n" + ("x" * (1024 * 1024 + 1))
    written = json.loads(
        await run("write_file", {"path": "docs/result.txt", "content": large_content})
    )
    assert written["operation"] == "write_file"
    assert written["path"] == "docs/result.txt"

    listed = await run("list_files", {"path": "docs"})
    assert "docs/result.txt" in listed
    read = await run("read_file", {"path": "docs/result.txt", "offset": 0, "limit": 1})
    assert "project file content" in read
    searched = await run(
        "search_files",
        {"pattern": "project file content", "path": "docs", "file_pattern": "*.txt"},
    )
    assert "docs/result.txt:1" in searched
    found = await run("find_files", {"pattern": "**/*.txt", "path": "."})
    assert "docs/result.txt" in found

    edited = json.loads(
        await run(
            "edit_file",
            {
                "path": "docs/result.txt",
                "old_string": "project file content",
                "new_string": "project file updated",
            },
        )
    )
    assert edited["replacements"] == 1
    moved = json.loads(
        await run(
            "move_file",
            {"source_path": "docs/result.txt", "destination_path": "deliverables/result.txt"},
        )
    )
    assert moved["destination_path"] == "deliverables/result.txt"
    deleted = json.loads(await run("delete_file", {"path": "deliverables"}))
    assert deleted["operation"] == "delete_file"

    denied = await run("write_file", {"path": ".agents/forbidden.txt", "content": "blocked"})
    assert "project deliverables" in denied

    stored = await env.db.get(Project, uuid.UUID(project_id))
    assert stored.settings["git"]["head"] == subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    committed_events = (
        await env.db.execute(
            select(ProjectEvent).where(
                ProjectEvent.project_id == uuid.UUID(project_id),
                ProjectEvent.event_type == "project.file.committed",
            )
        )
    ).scalars().all()
    assert len(committed_events) == 4

    stored.status = "paused"
    await env.db.commit()
    paused_write = await run("write_file", {"path": "paused.txt", "content": "blocked"})
    assert "only while the project is running" in paused_write
    assert not (repo / "paused.txt").exists()

async def test_approved_project_tool_resumes_with_the_original_project_workspace(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.database as database
    from app.services import agent_tools
    from app.services.agent_runtime_workspace import current_agent_runtime_workspace
    from app.services.autonomy_service import autonomy_service

    env = project_api
    project = await _create_project(env, name="Approved project workspace")
    project_id = uuid.UUID(project["id"])
    session = ChatSession(
        project_id=project_id,
        agent_id=env.worker_id,
        user_id=env.owner_id,
        title="Approved project action",
        source_channel="project",
        external_conv_id=f"approval-{uuid.uuid4()}",
    )
    env.db.add(session)
    await env.db.commit()

    observed: dict[str, Any] = {}

    async def observe_execute_tool(
        _tool_name: str,
        _arguments: dict[str, Any],
        agent_id: uuid.UUID,
        **_kwargs: Any,
    ) -> str:
        workspace = current_agent_runtime_workspace(agent_id)
        observed["agent_id"] = workspace.agent_id
        observed["project_id"] = workspace.project_id
        observed["is_project"] = workspace.is_project
        return "approved project tool executed"

    monkeypatch.setattr(database, "async_session", env.session_factory)
    monkeypatch.setattr(agent_tools, "execute_tool", observe_execute_tool)
    result = await autonomy_service._execute_approved_action(
        env.worker_id,
        "write_workspace_files",
        {
            "tool": "write_file",
            "args": {"workspace": "project", "path": "approved.txt", "content": "ok"},
            "requested_by": str(env.owner_id),
            "session_id": str(session.id),
            "tool_call_id": "approved-project-write",
        },
        env.owner_id,
    )

    assert result == "approved project tool executed"
    assert observed == {
        "agent_id": env.worker_id,
        "project_id": project_id,
        "is_project": True,
    }
    assert await autonomy_service._execute_approved_action(
        env.worker_id,
        "write_workspace_files",
        {
            "tool": "write_file",
            "args": {"workspace": "project", "path": "invalid.txt", "content": "blocked"},
            "session_id": str(session.id),
        },
        env.owner_id,
    ) == "Execution failed: approval has no valid original requester"

async def test_project_structured_readers_and_isolated_sandbox_share_the_project_repository(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from docx import Document

    from app.services import agent_tools
    from app.services.agent_runtime_workspace import (
        bind_agent_runtime_workspace,
        project_agent_runtime_workspace,
    )
    from app.services.tools import read_image as read_image_tool

    env = project_api
    project = await _create_project(env, name="Structured project workspace")
    project_id = uuid.UUID(project["id"])
    await _mark_project_running(env, project_id)
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    wake = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Inspect and update project files", "mentions": [str(env.worker_id)]},
    )
    assert wake.status_code == 201, wake.text
    child_id = next(
        row["session_id"]
        for row in wake.json()["subagent_runs"]
        if row["agent_id"] == str(env.worker_id)
    )
    runtime_workspace = project_agent_runtime_workspace(
        agent_id=env.worker_id,
        tenant_id=env.tenant_id,
        project_id=project_id,
    )
    repo = project_repo_path(env.tenant_id, project_id)
    (repo / "docs").mkdir(exist_ok=True)
    document = Document()
    document.add_paragraph("Project document content")
    document.save(repo / "docs" / "brief.docx")
    (repo / "assets").mkdir(exist_ok=True)
    (repo / "assets" / "diagram.png").write_bytes(b"project-image-marker")
    (repo / "sandbox-delete.txt").write_text("remove me\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "add",
            "--",
            "docs/brief.docx",
            "assets/diagram.png",
            "sandbox-delete.txt",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Project test",
            "-c",
            "user.email=project@test.invalid",
            "commit",
            "-m",
            "Add structured project inputs",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    async def run(tool_name: str, arguments: dict[str, Any]) -> str:
        with bind_agent_runtime_workspace(runtime_workspace):
            return await agent_tools.execute_tool(
                tool_name,
                {"workspace": "project", **arguments},
                env.worker_id,
                env.owner_id,
                session_id=child_id,
                tool_call_id=f"project-{tool_name}-{uuid.uuid4().hex[:8]}",
                skip_autonomy=True,
            )

    document_result = await run("read_document", {"path": "docs/brief.docx"})
    assert "Project document content" in document_result

    async def fake_read_image(agent_id, arguments, *, workspace_root=None):
        assert agent_id == env.worker_id
        assert workspace_root is not None
        assert (workspace_root / "assets" / "diagram.png").read_bytes() == b"project-image-marker"
        assert not (workspace_root / ".agents").exists()
        assert not (workspace_root / ".git").exists()
        return "project image read"

    monkeypatch.setattr(read_image_tool, "handle_read_image", fake_read_image)
    assert await run("read_image", {"image_paths": ["assets/diagram.png"]}) == "project image read"

    async def fake_execute_code(_agent_id, _ws, _arguments, **kwargs):
        sandbox_root = kwargs["work_dir_override"]
        runtime_temp_root = kwargs["runtime_temp_path_override"]
        assert runtime_temp_root.parent == sandbox_root.parent
        assert runtime_temp_root != sandbox_root / ".tmp"
        runtime_temp_root.mkdir(parents=True, exist_ok=True)
        (runtime_temp_root / "runtime-only.txt").write_text("not a project file")
        assert (sandbox_root / "PROJECT.json").is_file()
        assert not (sandbox_root / ".agents").exists()
        assert not (sandbox_root / ".git").exists()
        if _arguments.get("code") == "create oversized output":
            with (sandbox_root / "oversized.bin").open("wb") as oversized:
                oversized.truncate(10 * 1024 * 1024 + 1)
            return "oversized output created"
        (sandbox_root / "sandbox").mkdir(exist_ok=True)
        (sandbox_root / "sandbox" / "result.txt").write_text(
            "sandbox project output\n",
            encoding="utf-8",
        )
        (sandbox_root / "sandbox-delete.txt").unlink()
        return "sandbox ok"

    monkeypatch.setattr(agent_tools, "_execute_code", fake_execute_code)
    sandbox_result = await run(
        "execute_code",
        {
            "action": "execute",
            "language": "python",
            "code": "print('ok')",
            "execution_mode": "foreground",
        },
    )
    assert "sandbox ok" in sandbox_result
    assert "sandbox_commit" in sandbox_result
    assert (repo / "sandbox" / "result.txt").read_text(encoding="utf-8") == "sandbox project output\n"
    assert not (repo / "sandbox-delete.txt").exists()
    assert not (repo / ".tmp").exists()
    assert not (repo / ".runtime-tmp").exists()
    assert subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
    sandbox_parent = Path(tempfile.gettempdir()) / "clawith-project-sandboxes"
    assert not list(sandbox_parent.glob("project-code-*"))
    assert not list(sandbox_parent.glob("project-read-*"))

    oversized_result = await run(
        "execute_code",
        {
            "action": "execute",
            "language": "python",
            "code": "create oversized output",
            "execution_mode": "foreground",
        },
    )
    assert "项目文件提交未完成" in oversized_result
    assert not (repo / "oversized.bin").exists()

    stored = await env.db.get(Project, project_id)
    assert stored.settings["git"]["head"] == subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    sandbox_events = (
        await env.db.execute(
            select(ProjectEvent).where(
                ProjectEvent.project_id == project_id,
                ProjectEvent.event_type == "project.file.committed",
                ProjectEvent.event_metadata["operation"].astext == "sandbox_commit",
            )
        )
    ).scalars().all()
    assert len(sandbox_events) == 1

async def test_project_uncertain_audit_resolution_preserves_only_reachable_commits(
    project_api: ProjectApiEnv,
):
    from app.services.project_git_service import (
        repository_state,
        write_project_workspace_file,
    )
    from app.services.project_runtime_tools import _resolve_uncertain_project_file_audit
    from app.services.project_service import add_event

    env = project_api
    created = await _create_project(env, name="Uncertain file audit")
    project = await env.db.get(Project, uuid.UUID(created["id"]))
    member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(ProjectMemberSnapshot.project_id == project.id)
        )
    ).scalars().first()
    assert project is not None and member is not None

    initial_head = (await repository_state(project))["head"]
    exact = await write_project_workspace_file(project, "exact.txt", "exact\n")
    assert await _resolve_uncertain_project_file_audit(
        project,
        previous_head=initial_head,
        result=exact,
        tool_call_id="exact-compensation",
        member=member,
        agent_id=member.agent_id,
        project_run=None,
        summary="exact compensation",
        metadata={**exact, "tool_call_id": "exact-compensation"},
    ) is False
    assert (await repository_state(project))["head"] == initial_head

    committed = await write_project_workspace_file(project, "committed.txt", "committed\n")
    add_event(
        env.db,
        project,
        "project.file.committed",
        "already committed",
        actor_agent_id=member.agent_id,
        metadata={**committed, "tool_call_id": "ambiguous-success"},
    )
    await env.db.commit()
    assert await _resolve_uncertain_project_file_audit(
        project,
        previous_head=initial_head,
        result=committed,
        tool_call_id="ambiguous-success",
        member=member,
        agent_id=member.agent_id,
        project_run=None,
        summary="already committed",
        metadata={**committed, "tool_call_id": "ambiguous-success"},
    ) is True
    assert (await repository_state(project))["head"] == committed["commit"]

    descendant_base = committed["commit"]
    ancestor = await write_project_workspace_file(project, "ancestor.txt", "ancestor\n")
    descendant = await write_project_workspace_file(project, "descendant.txt", "descendant\n")
    assert await _resolve_uncertain_project_file_audit(
        project,
        previous_head=descendant_base,
        result=ancestor,
        tool_call_id="descendant-recovery",
        member=member,
        agent_id=member.agent_id,
        project_run=None,
        summary="descendant recovery",
        metadata={**ancestor, "tool_call_id": "descendant-recovery"},
    ) is True
    assert (await repository_state(project))["head"] == descendant["commit"]
    recovered = (
        await env.db.execute(
            select(ProjectEvent).where(
                ProjectEvent.project_id == project.id,
                ProjectEvent.event_metadata["tool_call_id"].astext == "descendant-recovery",
            )
        )
    ).scalar_one()
    assert recovered.event_metadata["audit_recovered"] is True
    assert recovered.event_metadata["current_head"] == descendant["commit"]
