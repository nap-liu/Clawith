"""Project-scoped Agent workspace isolation and promotion tests."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.services import agent_manager as agent_manager_module
from app.services import project_agent_workspace as workspace_service
from app.services.project_agent_workspace import (
    build_project_agent_identity_defaults,
    create_project_agent_workspace,
    deactivate_project_agent_workspace,
    project_agent_workspace,
    promote_project_agent_workspace,
    resolve_project_agent_path,
)
from app.services.storage import LocalStorageBackend
from app.services.workspace_paths import WorkspacePathError


def test_project_agent_paths_are_fixed_and_reject_traversal_and_symlinks(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        project_agent_workspace(tmp_path / "missing-project", uuid.uuid4())

    project_root = tmp_path / "project"
    project_root.mkdir()
    agent_id = uuid.uuid4()

    layout = project_agent_workspace(project_root, agent_id)

    assert layout.root == project_root / ".agents" / str(agent_id)
    assert layout.soul == layout.root / "soul.md"
    assert layout.memory == layout.root / "memory.md"
    assert layout.workspace == layout.root / "workspace"
    assert resolve_project_agent_path(project_root, agent_id, "workspace/report.md") == (layout.workspace / "report.md")
    with pytest.raises(WorkspacePathError):
        resolve_project_agent_path(project_root, agent_id, "../other-agent/soul.md")
    with pytest.raises(WorkspacePathError):
        resolve_project_agent_path(project_root, agent_id, str(tmp_path / "outside.md"))

    layout.workspace.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (layout.workspace / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspacePathError):
        resolve_project_agent_path(project_root, agent_id, "workspace/linked/secret.txt")

    other_agent_root = project_root / ".agents" / str(uuid.uuid4())
    other_agent_root.mkdir(parents=True)
    linked_agent_id = uuid.uuid4()
    (project_root / ".agents" / str(linked_agent_id)).symlink_to(other_agent_root, target_is_directory=True)
    with pytest.raises(WorkspacePathError):
        project_agent_workspace(project_root, linked_agent_id)


@pytest.mark.asyncio
async def test_create_from_agentdir_copies_only_allowlisted_assets_and_retains_on_deactivation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_agent_id = uuid.uuid4()
    project_agent_id = uuid.uuid4()
    source = tmp_path / "source-agent"
    (source / "memory").mkdir(parents=True)
    (source / "workspace" / "docs").mkdir(parents=True)
    (source / "workspace" / "sessions").mkdir()
    (source / "workspace" / "cache").mkdir()
    (source / "soul.md").write_text("# Source soul\n", encoding="utf-8")
    (source / "memory" / "memory.md").write_text("# Source memory\n", encoding="utf-8")
    (source / "workspace" / "docs" / "brief.md").write_text("brief", encoding="utf-8")
    (source / "workspace" / ".env.production").write_text("TOKEN=secret", encoding="utf-8")
    (source / "workspace" / "credentials.json").write_text("{}", encoding="utf-8")
    (source / "workspace" / "sessions" / "active.json").write_text("{}", encoding="utf-8")
    (source / "workspace" / "cache" / "index.bin").write_bytes(b"cache")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (source / "workspace" / "outside-link").symlink_to(outside)

    async def materialize_source(agent_id: uuid.UUID) -> Path:
        assert agent_id == source_agent_id
        return source

    monkeypatch.setattr(workspace_service.agent_manager, "_materialize_agent_dir", materialize_source)
    project_root = tmp_path / "project"
    project_root.mkdir()

    result = await create_project_agent_workspace(
        project_root,
        project_agent_id,
        source_agent_id=source_agent_id,
    )

    assert result.workspace.soul.read_text(encoding="utf-8") == "# Source soul\n"
    assert result.workspace.memory.read_text(encoding="utf-8") == "# Source memory\n"
    assert (result.workspace.workspace / "docs" / "brief.md").read_text(encoding="utf-8") == "brief"
    assert not (result.workspace.workspace / ".env.production").exists()
    assert not (result.workspace.workspace / "credentials.json").exists()
    assert not (result.workspace.workspace / "sessions").exists()
    assert not (result.workspace.workspace / "cache").exists()
    assert not (result.workspace.workspace / "outside-link").exists()
    assert set(result.skipped_sensitive) == {
        ".env.production",
        "cache/index.bin",
        "credentials.json",
        "outside-link",
        "sessions/active.json",
    }

    (source / "soul.md").write_text("# Changed source\n", encoding="utf-8")
    repeated = await create_project_agent_workspace(
        project_root,
        project_agent_id,
        source_agent_id=source_agent_id,
    )
    assert repeated.workspace.soul.read_text(encoding="utf-8") == "# Source soul\n"
    assert "soul.md" in repeated.skipped_existing

    retained = deactivate_project_agent_workspace(project_root, project_agent_id)
    assert retained.root == result.workspace.root
    assert retained.soul.exists()
    assert retained.memory.exists()


@pytest.mark.asyncio
async def test_create_without_source_seeds_required_project_assets(tmp_path: Path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    result = await create_project_agent_workspace(project_root, uuid.uuid4())

    assert result.workspace.soul.read_text(encoding="utf-8") == "# Project Agent\n"
    assert result.workspace.memory.read_text(encoding="utf-8") == "# Project Memory\n"
    assert result.workspace.workspace.is_dir()


@pytest.mark.asyncio
async def test_project_identity_defaults_are_role_specific_and_do_not_replace_source_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    soul, memory = build_project_agent_identity_defaults(
        project_name="客户续费健康度治理",
        project_goal="识别高风险客户并形成可执行的续费干预方案。",
        success_criteria=["高风险客户均有责任人与下一步动作", "续费预测误差低于 10%"],
        agent_name="客户成功分析师",
        role_description="负责健康度建模、风险分层与干预效果复盘。",
    )
    assert "客户成功分析师" in soul
    assert "健康度建模、风险分层与干预效果复盘" in soul
    assert "客户续费健康度治理" in memory
    assert "续费预测误差低于 10%" in memory

    source_agent_id = uuid.uuid4()
    source = tmp_path / "source-agent"
    (source / "memory").mkdir(parents=True)
    (source / "soul.md").write_text("# 来源 Agent 专属身份\n", encoding="utf-8")
    (source / "memory" / "memory.md").write_text("# 来源 Agent 专属记忆\n", encoding="utf-8")

    async def materialize_source(agent_id: uuid.UUID) -> Path:
        assert agent_id == source_agent_id
        return source

    monkeypatch.setattr(workspace_service.agent_manager, "_materialize_agent_dir", materialize_source)
    project_root = tmp_path / "project-with-source"
    project_root.mkdir()
    result = await create_project_agent_workspace(
        project_root,
        uuid.uuid4(),
        source_agent_id=source_agent_id,
        default_soul=soul,
        default_memory=memory,
    )

    assert result.workspace.soul.read_text(encoding="utf-8") == "# 来源 Agent 专属身份\n"
    assert result.workspace.memory.read_text(encoding="utf-8") == "# 来源 Agent 专属记忆\n"


@pytest.mark.asyncio
async def test_promote_writes_standard_agentdir_assets_without_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    project_agent_id = uuid.uuid4()
    target_agent_id = uuid.uuid4()
    project_result = await create_project_agent_workspace(project_root, project_agent_id)
    project_result.workspace.soul.write_text("# Promoted soul\n", encoding="utf-8")
    project_result.workspace.memory.write_text("# Promoted memory\n", encoding="utf-8")
    (project_result.workspace.workspace / "deliverables").mkdir()
    (project_result.workspace.workspace / "deliverables" / "result.md").write_text(
        "result",
        encoding="utf-8",
    )
    (project_result.workspace.workspace / "token.json").write_text("secret", encoding="utf-8")
    (project_result.workspace.root / ".openclaw").mkdir()
    (project_result.workspace.root / ".openclaw" / "openclaw.json").write_text(
        "secret",
        encoding="utf-8",
    )

    storage_root = tmp_path / "standard-agentdirs"
    storage = LocalStorageBackend(str(storage_root))
    monkeypatch.setattr(workspace_service, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_manager_module, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(
        workspace_service.agent_manager,
        "_agent_dir",
        lambda agent_id: storage_root / str(agent_id),
    )

    promoted = await promote_project_agent_workspace(project_root, project_agent_id, target_agent_id)

    assert promoted.target_agent_dir == storage_root / str(target_agent_id)
    assert (promoted.target_agent_dir / "soul.md").read_text(encoding="utf-8") == "# Promoted soul\n"
    assert (promoted.target_agent_dir / "memory" / "memory.md").read_text(encoding="utf-8") == ("# Promoted memory\n")
    assert (promoted.target_agent_dir / "workspace" / "deliverables" / "result.md").read_text(
        encoding="utf-8"
    ) == "result"
    assert not (promoted.target_agent_dir / "workspace" / "token.json").exists()
    assert not (promoted.target_agent_dir / ".openclaw").exists()
    assert promoted.skipped_sensitive == ("token.json",)

    project_result.workspace.soul.write_text("# New soul\n", encoding="utf-8")
    preserved = await promote_project_agent_workspace(project_root, project_agent_id, target_agent_id)
    assert "soul.md" in preserved.skipped_existing
    assert (promoted.target_agent_dir / "soul.md").read_text(encoding="utf-8") == "# Promoted soul\n"

    await promote_project_agent_workspace(
        project_root,
        project_agent_id,
        target_agent_id,
        overwrite=True,
    )
    assert (promoted.target_agent_dir / "soul.md").read_text(encoding="utf-8") == "# New soul\n"
