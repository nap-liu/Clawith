"""Repository initialization, remotes, cloning, and reconciliation."""

from app.services.project_git_core import *  # noqa: F401,F403

def _initialize(
    project: Project,
    author_name: str | None,
    author_email: str | None,
) -> dict:
    repo = project_repo_path(project.tenant_id, project.id)
    with _repo_lock(repo):
        repo.mkdir(parents=True, exist_ok=True)
        if not (repo / ".git").exists():
            _git(repo, "init")
            _git(repo, "config", "user.name", _DEFAULT_PROJECT_AUTHOR_NAME)
            _git(repo, "config", "user.email", _DEFAULT_PROJECT_AUTHOR_EMAIL)
            (repo / "README.md").write_text(
                f"# {project.name}\n\n{project.description}\n\n## Goal\n\n{project.goal}\n",
                encoding="utf-8",
            )
            (repo / "PROJECT.json").write_text(
                json.dumps(
                    {
                        "project_id": str(project.id),
                        "name": project.name,
                        "goal": project.goal,
                        "success_criteria": project.success_criteria,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            _git(repo, "add", "--", "README.md", "PROJECT.json")
            _commit_with_author(
                repo,
                "-m",
                "创建项目初始版本",
                author_name=author_name,
                author_email=author_email,
            )
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        return {
            "mode": "managed",
            "head": head,
            "default_branch": _git(repo, "branch", "--show-current").stdout.strip(),
        }


async def initialize_project_repo(
    project: Project,
    *,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict:
    return await asyncio.to_thread(_initialize, project, author_name, author_email)


def _repo_for(project: Project) -> Path:
    git_settings = dict((project.settings or {}).get("git") or {})
    git_mode = git_settings.get("mode") or git_settings.get("repository_mode", "managed")
    if git_mode != "managed":
        raise HTTPException(status_code=501, detail="External Git repositories require a connector")
    repo = project_repo_path(project.tenant_id, project.id)
    if not (repo / ".git").is_dir():
        raise HTTPException(status_code=409, detail="Managed project repository is not initialized")
    return repo


def _restore(
    project: Project,
    commit: str,
    message: str | None,
    author_name: str | None,
    author_email: str | None,
) -> dict:
    if not _COMMIT_RE.fullmatch(commit):
        raise HTTPException(status_code=422, detail="Invalid commit identifier")
    repo = _repo_for(project)
    with _repo_lock(repo):
        if _git(repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode != 0:
            raise HTTPException(status_code=422, detail="Commit does not exist in this project repository")
        _git(repo, "restore", "--source", commit, "--", ".")
        _git(repo, "add", "-A")
        _commit_with_author(
            repo,
            "--allow-empty",
            "-m",
            message or "恢复项目版本",
            author_name=author_name,
            author_email=author_email,
        )
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        return {"status": "completed", "operation": "restore_commit", "source_commit": commit, "commit": head}


async def restore_as_new_commit(
    project: Project,
    commit: str,
    message: str | None = None,
    *,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict:
    return await asyncio.to_thread(
        _restore,
        project,
        commit,
        message,
        author_name,
        author_email,
    )


def _create_branch(project: Project, name: str, from_commit: str | None) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        if _git(repo, "check-ref-format", "--branch", name, check=False).returncode != 0:
            raise HTTPException(status_code=422, detail="Invalid Git branch name")
        start = from_commit or "HEAD"
        if from_commit and (
            not _COMMIT_RE.fullmatch(from_commit)
            or _git(repo, "cat-file", "-e", f"{from_commit}^{{commit}}", check=False).returncode != 0
        ):
            raise HTTPException(status_code=422, detail="Branch source commit does not exist")
        result = _git(repo, "branch", name, start, check=False)
        if result.returncode != 0:
            raise HTTPException(status_code=409, detail=(result.stderr or "Branch already exists").strip())
        return {
            "status": "completed",
            "operation": "create_branch",
            "branch": name,
            "from_commit": _git(repo, "rev-parse", start).stdout.strip(),
        }


async def create_branch(project: Project, name: str, from_commit: str | None = None) -> dict:
    return await asyncio.to_thread(_create_branch, project, name, from_commit)


def _list_remotes(project: Project) -> list[dict[str, str]]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        names = [line.strip() for line in _git(repo, "remote").stdout.splitlines() if line.strip()]
        return [
            {
                "name": name,
                "url": _git(repo, "remote", "get-url", "--", name).stdout.strip(),
            }
            for name in names
        ]


async def list_git_remotes(project: Project) -> list[dict[str, str]]:
    return await asyncio.to_thread(_list_remotes, project)


def _put_remote(project: Project, name: str, safe_url: str) -> dict[str, str | bool]:
    repo = _repo_for(project)
    safe_name = _safe_remote_name(name)
    with _repo_lock(repo):
        exists = _git(repo, "remote", "get-url", "--", safe_name, check=False).returncode == 0
        command = (
            ("remote", "set-url", "--", safe_name, safe_url)
            if exists
            else (
                "remote",
                "add",
                "--",
                safe_name,
                safe_url,
            )
        )
        _git(repo, *command)
        return {"name": safe_name, "url": safe_url, "created": not exists}


async def put_git_remote(project: Project, name: str, url: str) -> dict[str, str | bool]:
    safe_url = await validate_project_remote_url(url)
    return await asyncio.to_thread(_put_remote, project, name, safe_url)


def _delete_remote(project: Project, name: str) -> dict[str, str]:
    repo = _repo_for(project)
    safe_name = _safe_remote_name(name)
    with _repo_lock(repo):
        current = _git(repo, "remote", "get-url", "--", safe_name, check=False)
        if current.returncode != 0:
            raise HTTPException(status_code=404, detail="Git remote not found")
        _git(repo, "remote", "remove", "--", safe_name)
        return {"name": safe_name, "url": current.stdout.strip(), "status": "deleted"}


async def delete_git_remote(project: Project, name: str) -> dict[str, str]:
    return await asyncio.to_thread(_delete_remote, project, name)


def _assert_replaceable_baseline(project: Project, repo: Path) -> None:
    """Allow clone only over the generated pre-delivery project baseline.

    Project creation records the initialization commit in settings, then may
    add one system-owned member-directory commit so every selected member has
    a project-local identity.  Those generated assets are still part of the
    replaceable planning baseline; any other commit or path is user output and
    must make clone fail closed.
    """

    if _git(repo, "status", "--porcelain").stdout.strip():
        raise HTTPException(status_code=409, detail="Project repository has uncommitted changes")
    configured_head = str(dict((project.settings or {}).get("git") or {}).get("head") or "")
    baseline_exists = bool(
        _COMMIT_RE.fullmatch(configured_head)
        and _git(repo, "cat-file", "-e", f"{configured_head}^{{commit}}", check=False).returncode == 0
    )
    baseline_files = (
        {line for line in _git(repo, "ls-tree", "-r", "--name-only", configured_head).stdout.splitlines() if line}
        if baseline_exists
        else set()
    )
    baseline_subject = _git(repo, "log", "-1", "--format=%s", configured_head).stdout.strip() if baseline_exists else ""
    baseline_is_ancestor = bool(
        baseline_exists
        and _git(repo, "merge-base", "--is-ancestor", configured_head, "HEAD", check=False).returncode == 0
    )
    generated_subjects = (
        _git(repo, "log", "--format=%s", f"{configured_head}..HEAD").stdout.splitlines() if baseline_is_ancestor else []
    )
    generated_authors = (
        _git(repo, "log", "--format=%ae", f"{configured_head}..HEAD").stdout.splitlines()
        if baseline_is_ancestor
        else []
    )
    generated_paths = (
        {line for line in _git(repo, "diff", "--name-only", configured_head, "HEAD").stdout.splitlines() if line}
        if baseline_is_ancestor
        else set()
    )
    generated_subjects_only = all(
        subject in {"Ensure project member directories", "创建项目成员目录"}
        or subject.startswith(("Create project Agent: ", "创建项目数字员工："))
        for subject in generated_subjects
    )
    generated_authors_only = all(
        author in {_DEFAULT_PROJECT_AUTHOR_EMAIL, project_user_git_email(project.owner_user_id)}
        for author in generated_authors
    )
    generated_only = (
        generated_subjects_only
        and generated_authors_only
        and all(path.startswith(".agents/") for path in generated_paths)
    )
    if (
        baseline_files != {"PROJECT.json", "README.md"}
        or baseline_subject
        not in {"Initialize project", "Initialize AI-native project", "创建项目初始版本"}
        or not baseline_is_ancestor
        or not generated_only
    ):
        raise HTTPException(
            status_code=409,
            detail="Clone is only allowed before the project repository contains user output",
        )


def _copy_generated_member_baseline(repo: Path, candidate: Path) -> None:
    """Carry the current project's generated member identities into a clone."""

    source = repo / ".agents"
    if not source.is_dir():
        return
    if source.is_symlink() or any(path.is_symlink() for path in source.rglob("*")):
        raise HTTPException(status_code=409, detail="Generated project member assets contain a symbolic link")
    shutil.copytree(source, candidate / ".agents", dirs_exist_ok=True)
    _git(candidate, "add", "--", ".agents")
    if _git(candidate, "diff", "--cached", "--quiet", "--", ".agents", check=False).returncode == 0:
        return
    _commit_with_author(
        candidate,
        "-m",
        "创建项目成员目录",
        author_name=None,
        author_email=None,
    )


def _restore_clone_backup(operation: ProjectRepositoryCloneOperation) -> None:
    repo = operation.repo
    displaced = repo.parent / f".repo-rollback-displaced-{operation.id}"
    try:
        with _repo_lock(repo):
            if operation.backup.exists():
                if repo.exists():
                    if displaced.exists():
                        shutil.rmtree(displaced)
                    os.replace(repo, displaced)
                try:
                    os.replace(operation.backup, repo)
                except BaseException:
                    if displaced.exists() and not repo.exists():
                        os.replace(displaced, repo)
                    raise
            elif not repo.exists() or _git(repo, "rev-parse", "HEAD", check=False).stdout.strip() != operation.old_head:
                raise RuntimeError("Clone rollback lost its durable initialization backup")
            if displaced.exists():
                shutil.rmtree(displaced)
            if operation.staging_root.exists():
                shutil.rmtree(operation.staging_root)
            operation.active = False
    finally:
        _release_repository_operation_lock(operation)


def _stage_clone_repository(
    project: Project,
    safe_url: str,
    branch: str | None,
    operation_id: uuid.UUID,
) -> ProjectRepositoryCloneOperation:
    if project.status != "planning":
        raise HTTPException(status_code=409, detail="A remote can only be cloned while the project is planning")
    repo = _repo_for(project)
    lock_handle = _acquire_repository_operation_lock(repo, blocking=True)
    if lock_handle is None:  # Blocking acquisition only returns None defensively.
        raise HTTPException(status_code=409, detail="A repository operation is already in progress")
    staging_root = repo.parent / f".clawith-clone-{operation_id}"
    candidate = staging_root / "repo"
    backup = repo.parent / f".repo-backup-{operation_id}"
    operation: ProjectRepositoryCloneOperation | None = None
    try:
        if staging_root.exists() or backup.exists():
            raise RuntimeError("Clone operation paths already exist")
        _assert_replaceable_baseline(project, repo)
        if branch and _git(repo, "check-ref-format", "--branch", branch, check=False).returncode != 0:
            raise HTTPException(status_code=422, detail="Invalid Git branch name")
        old_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        staging_root.mkdir(mode=0o700)
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
        }
        args = ["git", "-c", "http.followRedirects=false", "clone", "--no-hardlinks"]
        if branch:
            args.extend(["--branch", branch])
        args.extend(["--", safe_url, str(candidate)])
        cloned = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
            env=env,
        )
        if cloned.returncode != 0:
            detail = (cloned.stderr or cloned.stdout or "git clone failed").strip()
            raise HTTPException(status_code=422, detail=f"Git clone failed: {detail[:500]}")
        if not (candidate / ".git").is_dir():
            raise HTTPException(status_code=422, detail="Cloned source is not a Git repository")
        _copy_generated_member_baseline(repo, candidate)
        if _git(candidate, "fsck", "--no-dangling", check=False).returncode != 0:
            raise HTTPException(status_code=422, detail="Cloned Git repository failed validation")
        verified_head = _git(candidate, "rev-parse", "--verify", "HEAD", check=False)
        if verified_head.returncode != 0:
            raise HTTPException(status_code=422, detail="Cloned Git repository has no valid HEAD")
        head = verified_head.stdout.strip()
        default_branch = _git(candidate, "branch", "--show-current").stdout.strip()
        _git(candidate, "config", "user.name", _DEFAULT_PROJECT_AUTHOR_NAME)
        _git(candidate, "config", "user.email", _DEFAULT_PROJECT_AUTHOR_EMAIL)
        remotes = [
            {
                "name": name,
                "url": _git(candidate, "remote", "get-url", "--", name).stdout.strip(),
            }
            for name in _git(candidate, "remote").stdout.splitlines()
            if name.strip()
        ]
        operation = ProjectRepositoryCloneOperation(
            id=operation_id,
            repo=repo,
            backup=backup,
            staging_root=staging_root,
            candidate=candidate,
            old_head=old_head,
            new_head=head,
            lock_handle=lock_handle,
            result={
                "status": "completed",
                "operation": "clone",
                "url": safe_url,
                "head": head,
                "default_branch": default_branch,
                "remotes": remotes,
            },
        )
        return operation
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Git clone timed out without prompting") from exc
    finally:
        if operation is None:
            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)
            placeholder = ProjectRepositoryCloneOperation(
                id=operation_id,
                repo=repo,
                backup=backup,
                staging_root=staging_root,
                candidate=candidate,
                old_head="",
                new_head="",
                result={},
                lock_handle=lock_handle,
            )
            _release_repository_operation_lock(placeholder)


async def begin_project_repository_clone(
    project: Project,
    url: str,
    branch: str | None = None,
) -> ProjectRepositoryCloneOperation:
    safe_url = await validate_project_remote_url(url)
    operation_id = uuid.uuid4()
    return await asyncio.to_thread(_stage_clone_repository, project, safe_url, branch, operation_id)


def _apply_clone_repository(operation: ProjectRepositoryCloneOperation) -> None:
    with _repo_lock(operation.repo):
        current_head = _git(operation.repo, "rev-parse", "HEAD", check=False).stdout.strip()
        candidate_head = _git(operation.candidate, "rev-parse", "HEAD", check=False).stdout.strip()
        if current_head != operation.old_head or candidate_head != operation.new_head:
            raise HTTPException(status_code=409, detail="Project repository changed while clone was staged")
        os.replace(operation.repo, operation.backup)
        try:
            os.replace(operation.candidate, operation.repo)
        except BaseException:
            os.replace(operation.backup, operation.repo)
            raise


async def apply_project_repository_clone(operation: ProjectRepositoryCloneOperation) -> None:
    await asyncio.to_thread(_apply_clone_repository, operation)


def _finalize_clone_repository(operation: ProjectRepositoryCloneOperation) -> None:
    try:
        with _repo_lock(operation.repo):
            current_head = _git(operation.repo, "rev-parse", "HEAD", check=False).stdout.strip()
            if current_head != operation.new_head:
                raise RuntimeError("Committed clone repository no longer matches its journal")
            if operation.backup.exists():
                shutil.rmtree(operation.backup)
            if operation.staging_root.exists():
                shutil.rmtree(operation.staging_root)
            operation.active = False
    finally:
        _release_repository_operation_lock(operation)


async def finalize_project_repository_clone(operation: ProjectRepositoryCloneOperation) -> None:
    """Discard the initialization backup after the database commit succeeds."""

    await asyncio.to_thread(_finalize_clone_repository, operation)


async def rollback_project_repository_clone(operation: ProjectRepositoryCloneOperation) -> None:
    """Restore the pre-clone repository after a database transaction failure."""

    await asyncio.to_thread(_restore_clone_backup, operation)


async def release_project_repository_clone_lock(operation: ProjectRepositoryCloneOperation) -> None:
    """Release the process lock when the durable DB state cannot be read.

    The journal and filesystem are intentionally left untouched. The next
    repository access can then decide between rollback and finalize from the
    durable state instead of guessing after an ambiguous commit result.
    """

    await asyncio.to_thread(_release_repository_operation_lock, operation)


def _operation_path(repo: Path, name: str, *, prefix: str) -> Path:
    if Path(name).name != name or not name.startswith(prefix):
        raise RuntimeError("Invalid repository operation journal path")
    path = (repo.parent / name).resolve(strict=False)
    if path.parent != repo.parent.resolve():
        raise RuntimeError("Repository operation journal escaped the managed root")
    return path


def _operation_from_journal(row: ProjectRepositoryOperation) -> ProjectRepositoryCloneOperation:
    repo = project_repo_path(row.tenant_id, row.project_id)
    staging_root = _operation_path(repo, row.staging_name, prefix=".clawith-clone-")
    backup = _operation_path(repo, row.backup_name, prefix=".repo-backup-")
    return ProjectRepositoryCloneOperation(
        id=row.id,
        repo=repo,
        backup=backup,
        staging_root=staging_root,
        candidate=staging_root / "repo",
        old_head=row.old_head,
        new_head=row.new_head,
        result={},
    )


def _reconcile_clone_journal(row: ProjectRepositoryOperation) -> bool:
    operation = _operation_from_journal(row)
    operation.lock_handle = _acquire_repository_operation_lock(operation.repo, blocking=False)
    if operation.lock_handle is None:
        return False
    if row.operation_type != "clone":
        _release_repository_operation_lock(operation)
        raise RuntimeError(f"Unsupported repository operation: {row.operation_type}")
    if row.state == "prepared":
        _restore_clone_backup(operation)
    elif row.state == "committed":
        _finalize_clone_repository(operation)
    else:
        _release_repository_operation_lock(operation)
        raise RuntimeError(f"Unsupported repository operation state: {row.state}")
    return True


async def _reconcile_project_repository_operations(
    db: AsyncSession,
    project_id: uuid.UUID | None,
) -> int:
    """Repair abandoned clone journals before the next repository access."""

    statement = select(ProjectRepositoryOperation).order_by(ProjectRepositoryOperation.created_at)
    if project_id is not None:
        statement = statement.where(ProjectRepositoryOperation.project_id == project_id)
    rows = (await db.execute(statement)).scalars().all()
    reconciled = 0
    for row in rows:
        completed = await asyncio.to_thread(_reconcile_clone_journal, row)
        if not completed:
            if project_id is not None:
                raise HTTPException(status_code=409, detail="A repository operation is already in progress")
            continue
        await db.delete(row)
        reconciled += 1
    if project_id is not None:
        await _ensure_project_member_directories(db, project_id)
    await db.flush()
    return reconciled


def _missing_project_member_identity_paths(repo: Path, agent_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    missing: dict[uuid.UUID, list[str]] = {}
    with _repo_lock(repo):
        tracked_paths = set(_git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", ".agents").stdout.splitlines())
        for agent_id in agent_ids:
            agent_dir = f".agents/{agent_id}"
            paths = [f"{agent_dir}/soul.md", f"{agent_dir}/memory.md"]
            absent = [path for path in paths if path not in tracked_paths]
            if absent:
                missing[agent_id] = absent
    return missing


def _commit_project_member_workspace_paths(project: Project, paths: list[str]) -> None:
    """Commit only files copied into member snapshots, never unrelated changes."""

    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized_paths = [_safe_relative_path(repo, path)[0] for path in paths]
        _git(repo, "add", "--", *normalized_paths)
        diff_args = ["diff", "--cached", "--quiet", "--", *normalized_paths]
        if _git(repo, *diff_args, check=False).returncode == 0:
            return
        _commit_with_author(
            repo,
            "--only",
            "-m",
            "创建项目成员目录",
            "--",
            *normalized_paths,
            author_name=None,
            author_email=None,
        )


async def _ensure_project_member_directories(db: AsyncSession, project_id: uuid.UUID) -> None:
    """Keep every member snapshot visible through the canonical project Git HEAD."""

    project = await db.get(Project, project_id)
    if project is None:
        return
    repo = project_repo_path(project.tenant_id, project.id)
    if not (repo / ".git").is_dir():
        return
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot)
                .where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                )
                .order_by(ProjectMemberSnapshot.created_at, ProjectMemberSnapshot.id)
            )
        )
        .scalars()
        .all()
    )
    missing = await asyncio.to_thread(
        _missing_project_member_identity_paths,
        repo,
        [member.agent_id for member in members],
    )
    if not missing:
        return

    agents = (
        (
            await db.execute(
                select(Agent).where(
                    Agent.tenant_id == project.tenant_id,
                    Agent.id.in_(missing),
                )
            )
        )
        .scalars()
        .all()
    )
    agent_by_id = {agent.id: agent for agent in agents}

    from app.services.project_agent_workspace import (
        build_project_agent_identity_defaults,
        create_project_agent_workspace,
    )

    changed_paths: set[str] = set()
    for member in members:
        if member.agent_id not in missing:
            continue
        agent = agent_by_id.get(member.agent_id)
        source_agent_id = None
        if agent is not None:
            source_agent_id = agent.source_agent_id if agent.scope == "project" else agent.id
        default_soul, default_memory = build_project_agent_identity_defaults(
            project_name=project.name,
            project_goal=project.goal or "",
            success_criteria=project.success_criteria or [],
            agent_name=member.name_snapshot,
            role_description=member.role_snapshot,
        )
        copy_result = await create_project_agent_workspace(
            repo,
            member.agent_id,
            source_agent_id=source_agent_id,
            default_soul=default_soul,
            default_memory=default_memory,
            copy_source_memory=False,
            copy_source_workspace=False,
        )
        changed_paths.update(missing[member.agent_id])
        changed_paths.update(f".agents/{member.agent_id}/{path}" for path in copy_result.copied)
    await asyncio.to_thread(
        _commit_project_member_workspace_paths,
        project,
        sorted(changed_paths),
    )


async def reconcile_project_repository_operations(
    project_id: uuid.UUID | None = None,
    *,
    db: AsyncSession | None = None,
) -> int:
    if db is not None:
        return await _reconcile_project_repository_operations(db, project_id)
    async with async_session() as owned_db:
        reconciled = await _reconcile_project_repository_operations(owned_db, project_id)
        await owned_db.commit()
        return reconciled




__all__ = [name for name in globals() if not name.startswith("__")]
