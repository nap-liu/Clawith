from __future__ import annotations

import socket
import subprocess
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models.agent  # noqa: F401
import app.models.chat_session  # noqa: F401
import app.models.llm  # noqa: F401
import app.models.org  # noqa: F401
import app.models.participant  # noqa: F401
import app.models.project  # noqa: F401
import app.models.tenant  # noqa: F401
import app.models.user  # noqa: F401
from app.database import Base
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.project_git_service import (
    _git,
    _mime_and_kind,
    commit_project_changes,
    create_branch,
    initialize_project_repo,
    inspect_project_directory,
    list_project_files,
    project_commit_diff,
    restore_as_new_commit,
    validate_project_remote_url,
    write_project_file,
)
from app.services.project_service import (
    PROJECT_EVENT_SUMMARY_MAX_LENGTH,
    bounded_project_event_summary,
)

TABLES = [
    "llm_models",
    "identities",
    "tenants",
    "users",
    "agent_templates",
    "agents",
    "agent_permissions",
    "tools",
    "agent_tools",
    "agent_agent_relationships",
    "org_departments",
    "org_members",
    "participants",
    "project_templates",
    "projects",
    "project_repository_operations",
    "project_access_grants",
    "project_member_snapshots",
    "project_capability_bindings",
    "project_work_items",
    "project_runs",
    "project_run_member_snapshots",
    "project_events",
    "chat_sessions",
]


def test_project_event_summary_boundary_keeps_full_unicode_detail_in_metadata():
    original = "完整中文事件说明" * 100
    summary, metadata = bounded_project_event_summary("project.tool.updated", original)

    assert len(summary) <= PROJECT_EVENT_SUMMARY_MAX_LENGTH
    assert summary.startswith("project.tool.updated · full details stored in event metadata")
    assert metadata["full_summary"] == original
    assert metadata["summary_compacted"] is True
    assert len(metadata["summary_sha256"]) == 64


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[Base.metadata.tables[name] for name in TABLES],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _tenant(db, name: str) -> Tenant:
    tenant = Tenant(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}")
    db.add(tenant)
    await db.flush()
    return tenant


async def _user(db, tenant: Tenant, name: str) -> User:
    identity = Identity(username=f"{name}-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex}@local.test")
    db.add(identity)
    await db.flush()
    user = User(identity_id=identity.id, tenant_id=tenant.id, display_name=name, role="member", is_active=True)
    db.add(user)
    await db.flush()
    return user


async def test_git_remote_validation_blocks_non_public_dns(monkeypatch: pytest.MonkeyPatch):
    addresses = {
        "loopback.example": "127.0.0.1",
        "linklocal.example": "169.254.7.8",
        "metadata.example": "169.254.169.254",
        "private.example": "10.23.4.5",
        "public.example": "93.184.216.34",
    }

    def fake_getaddrinfo(host: str, port: int, *_args):
        address = addresses[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    for remote in (
        "http://loopback.example/org/repo.git",
        "http://linklocal.example/org/repo.git",
        "http://metadata.example/latest/meta-data",
        "https://private.example/org/repo.git",
        "ssh://git@private.example/org/repo.git",
        "git://private.example/org/repo.git",
        "git@private.example:org/repo.git",
    ):
        with pytest.raises(HTTPException) as denied:
            await validate_project_remote_url(remote)
        assert denied.value.status_code == 422

    for public_remote in (
        "http://public.example/org/repo.git",
        "https://public.example/org/repo.git",
        "ssh://git@public.example/org/repo.git",
        "git://public.example/org/repo.git",
        "git@public.example:org/repo.git",
    ):
        assert await validate_project_remote_url(public_remote) == public_remote


@pytest.mark.parametrize("trailing_bytes", [1, 2])
def test_mime_detection_accepts_utf8_split_at_blob_prefix_boundary(trailing_bytes: int):
    encoded = "中".encode()
    sample = b"a" * (8192 - trailing_bytes) + encoded[:trailing_bytes]

    assert len(sample) == 8192
    assert _mime_and_kind("notes.txt", sample) == ("text/plain", "text", True)


def test_mime_detection_still_rejects_invalid_utf8_inside_blob_prefix():
    sample = b"valid-prefix\n" + b"\xff" + b"valid-suffix\n"

    assert _mime_and_kind("notes.txt", sample) == ("text/plain", "binary", False)


def test_git_commands_trust_only_the_exact_managed_repository(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr("app.services.project_git_service.subprocess.run", fake_run)

    result = _git(tmp_path, "status", "--short")

    assert result.stdout == "ok"
    assert captured["command"][:5] == [
        "git",
        "-c",
        f"safe.directory={tmp_path}",
        "-C",
        str(tmp_path),
    ]


async def test_managed_git_restore_creates_new_commit_without_rewriting(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.project_git_service.get_settings",
        lambda: SimpleNamespace(STORAGE_LOCAL_ROOT=str(tmp_path)),
    )
    project = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="Git Project",
        description="Traceable",
        goal="Never rewrite history",
        success_criteria=["restore commit"],
        settings={"git": {"mode": "managed"}},
    )
    initial = await initialize_project_repo(
        project,
        author_name="Project Owner",
        author_email="user-owner@project.local",
    )
    repo = tmp_path / "_projects" / str(project.tenant_id) / str(project.id) / "repo"
    written = await write_project_file(
        project,
        "deliverables/result.md",
        "# Result\n\nTraceable.\n",
        author_name="Research Agent",
        author_email="agent-research@project.local",
    )
    assert written["commit"] != initial["head"]
    files = await list_project_files(project)
    assert next(item for item in files if item["path"] == "deliverables/result.md")["preview"].startswith("# Result")
    with pytest.raises(HTTPException) as traversal:
        await write_project_file(project, "../outside.md", "escaped")
    assert traversal.value.status_code == 422
    with pytest.raises(HTTPException) as git_metadata:
        await write_project_file(project, ".git/config", "escaped")
    assert git_metadata.value.status_code == 422

    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    changed = await commit_project_changes(
        project,
        "Change README",
        ["README.md"],
        author_name="Architecture Agent",
        author_email="agent-architecture@project.local",
    )
    changed_head = changed["commit"]

    milestone = await commit_project_changes(
        project,
        "Delivery milestone",
        milestone=True,
        operation_key="delivery-v1",
        author_name="QA Agent",
        author_email="agent-qa@project.local",
    )
    assert milestone["commit"] != changed_head
    assert milestone["changed"] is False

    subprocess.check_call(
        [
            "git",
            "-C",
            str(repo),
            "commit",
            "--allow-empty",
            "-m",
            "Legacy milestone",
            "-m",
            "Clawith-Milestone-Operation: legacy-delivery-v1",
        ]
    )
    legacy_commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    legacy_replay = await commit_project_changes(
        project,
        "Legacy milestone replay",
        milestone=True,
        operation_key="legacy-delivery-v1",
        author_name="QA Agent",
        author_email="agent-qa@project.local",
    )
    assert legacy_replay["idempotent_replay"] is True
    assert legacy_replay["commit"] == legacy_commit

    restored = await restore_as_new_commit(
        project,
        initial["head"],
        author_name="Project Owner",
        author_email="user-owner@project.local",
    )
    assert restored["commit"] not in {initial["head"], changed_head, milestone["commit"]}
    assert (repo / "README.md").read_text(encoding="utf-8").startswith("# Git Project")
    branch = await create_branch(project, "review/restore", initial["head"])
    assert branch["status"] == "completed"
    assert (
        subprocess.check_output(["git", "-C", str(repo), "rev-parse", "review/restore"], text=True).strip()
        == initial["head"]
    )
    expected_authors = {
        initial["head"]: "Project Owner <user-owner@project.local>",
        written["commit"]: "Research Agent <agent-research@project.local>",
        changed["commit"]: "Architecture Agent <agent-architecture@project.local>",
        milestone["commit"]: "QA Agent <agent-qa@project.local>",
        restored["commit"]: "Project Owner <user-owner@project.local>",
    }
    for commit, expected_author in expected_authors.items():
        assert (
            subprocess.check_output(
                ["git", "-C", str(repo), "show", "-s", "--format=%an <%ae>", commit],
                text=True,
            ).strip()
            == expected_author
        )
    assert (
        subprocess.check_output(
            ["git", "-C", str(repo), "config", "user.name"],
            text=True,
        ).strip()
        == "项目负责人"
    )
    assert (
        subprocess.check_output(
            ["git", "-C", str(repo), "config", "user.email"],
            text=True,
        ).strip()
        == "project@project.local"
    )
    milestone_body = subprocess.check_output(
        ["git", "-C", str(repo), "show", "-s", "--format=%B", milestone["commit"]],
        text=True,
    )
    assert "Project-Milestone-Operation: delivery-v1" in milestone_body
    assert "Clawith-Milestone-Operation" not in milestone_body


async def test_project_git_diff_is_bounded_path_safe_and_handles_root_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.project_git_service.get_settings",
        lambda: SimpleNamespace(STORAGE_LOCAL_ROOT=str(tmp_path)),
    )
    project = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="Diff Project",
        description="Provider-neutral diff",
        goal="Inspect evidence safely",
        success_criteria=["bounded patch"],
        settings={"git": {"mode": "managed"}},
    )
    initial = await initialize_project_repo(project)
    repo = tmp_path / "_projects" / str(project.tenant_id) / str(project.id) / "repo"
    root_diff = await project_commit_diff(project, initial["head"], path="README.md")
    assert root_diff["is_root"] is True
    assert root_diff["parent_commit"] is None
    assert [entry["path"] for entry in root_diff["files"]] == ["README.md"]
    assert root_diff["files"][0]["status"] == "added"
    assert root_diff["files"][0]["original_content"] == ""
    assert root_diff["files"][0]["modified_content"].startswith("# Diff Project")

    first = await write_project_file(project, "src/result.py", "print('first')\n")
    second = await write_project_file(
        project,
        "src/result.py",
        "# 中文证据\n" + "print('traceable change')\n" * 200,
    )
    bounded = await project_commit_diff(project, second["commit"], path="src/result.py", max_patch_bytes=128)
    assert bounded["parent_commit"] == first["commit"]
    assert bounded["patch_truncated"] is True
    assert bounded["patch_bytes"] <= 128
    assert len(bounded["patch"].encode("utf-8")) <= 128
    assert bounded["files"][0]["original_content"] == "print('first')\n"
    assert bounded["files"][0]["modified_content"].startswith("# 中文证据")
    commit_wide = await project_commit_diff(project, second["commit"])
    assert commit_wide["files"][0]["content_included"] is False
    assert commit_wide["files"][0]["original_content"] is None
    assert commit_wide["files"][0]["modified_content"] is None

    with pytest.raises(HTTPException) as traversal:
        await project_commit_diff(project, second["commit"], path="../secret")
    assert traversal.value.status_code == 422
    with pytest.raises(HTTPException) as invalid_parent:
        await project_commit_diff(project, second["commit"], parent=initial["head"])
    assert invalid_parent.value.status_code == 422

    (repo / "asset.bin").write_bytes(b"\x00private-binary-payload\xff")
    _git(repo, "add", "--", "asset.bin")
    _git(repo, "commit", "-m", "Add binary evidence")
    binary_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
    binary_diff = await project_commit_diff(project, binary_commit, path="asset.bin")
    assert binary_diff["files"][0]["binary"] is True
    assert binary_diff["files"][0]["modified_content"] is None
    assert "private-binary-payload" not in binary_diff["patch"]

    tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    dangling = _git(repo, "commit-tree", tree, "-p", binary_commit, "-m", "Dangling secret").stdout.strip()
    with pytest.raises(HTTPException) as unreachable:
        await project_commit_diff(project, dangling)
    assert unreachable.value.status_code == 422


async def test_project_directory_archive_rejects_limits_traversal_and_symlinks(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.project_git_service.get_settings",
        lambda: SimpleNamespace(STORAGE_LOCAL_ROOT=str(tmp_path)),
    )
    project = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="Archive Project",
        description="Safe ZIP",
        goal="Download tracked evidence",
        success_criteria=[],
        settings={"git": {"mode": "managed"}},
    )
    await initialize_project_repo(project)
    repo = tmp_path / "_projects" / str(project.tenant_id) / str(project.id) / "repo"
    (repo / "deliverables").mkdir()
    (repo / "deliverables" / "report.txt").write_text("tracked evidence", encoding="utf-8")
    _git(repo, "add", "--", "deliverables/report.txt")
    _git(repo, "commit", "-m", "Add archive evidence")
    snapshot = await inspect_project_directory(project, "deliverables")
    assert snapshot["file_count"] == 1
    assert snapshot["total_size"] == len("tracked evidence")

    with pytest.raises(HTTPException) as traversal:
        await inspect_project_directory(project, "../deliverables")
    assert traversal.value.status_code == 422
    monkeypatch.setattr("app.services.project_git_service.PROJECT_DIRECTORY_ARCHIVE_MAX_BYTES", 4)
    with pytest.raises(HTTPException) as too_large:
        await inspect_project_directory(project, "deliverables")
    assert too_large.value.status_code == 413
    monkeypatch.setattr("app.services.project_git_service.PROJECT_DIRECTORY_ARCHIVE_MAX_BYTES", 100 * 1024 * 1024)

    (repo / "deliverables" / "linked-secret").symlink_to("../README.md")
    _git(repo, "add", "--", "deliverables/linked-secret")
    _git(repo, "commit", "-m", "Add unsafe symlink fixture")
    with pytest.raises(HTTPException) as symlink:
        await inspect_project_directory(project, "deliverables")
    assert symlink.value.status_code == 422


def test_project_router_exposes_closed_loop_contract():
    from app.api.projects import router

    paths = {route.path for route in router.routes}
    assert {
        "/projects",
        "/projects/templates",
        "/projects/bootstrap-options",
        "/projects/{project_id}/members",
        "/projects/{project_id}/capabilities",
        "/projects/{project_id}/work-items",
        "/projects/{project_id}/runs",
        "/projects/{project_id}/events",
        "/projects/{project_id}/a2a",
        "/projects/{project_id}/settings",
        "/projects/{project_id}/files",
        "/projects/{project_id}/files/archive",
        "/projects/{project_id}/files/archive/raw",
        "/projects/{project_id}/files/preview/{ticket}/{path:path}",
        "/projects/{project_id}/git/commit",
        "/projects/{project_id}/git/diff",
        "/projects/{project_id}/git/restore",
    } <= paths
