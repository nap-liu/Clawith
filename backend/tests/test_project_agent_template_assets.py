"""Project-template round-trip tests for project-owned Agent assets."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.services import project_agent_template_assets as template_assets
from app.services.project_agent_template_assets import (
    ProjectAgentTemplateAssetError,
    ProjectAgentTemplateSource,
    export_project_agent_template_assets,
    instantiate_project_agent_template_assets,
)
from app.services.project_agent_workspace import create_project_agent_workspace, project_agent_workspace


@pytest.mark.asyncio
async def test_project_agent_template_round_trip_uses_fresh_ids_and_sanitized_assets(tmp_path: Path):
    source_project_id = uuid.uuid4()
    source_tenant_id = uuid.uuid4()
    source_user_id = uuid.uuid4()
    source_agent_id = uuid.uuid4()
    source_project = tmp_path / "source-project"
    source_project.mkdir()
    source = await create_project_agent_workspace(source_project, source_agent_id)
    source.workspace.soul.write_text(
        f"# Research lead\nowner={source_user_id}\napi_key=super-secret\n",
        encoding="utf-8",
    )
    source.workspace.memory.write_text(
        f"Project {source_project_id}; tenant {source_tenant_id}; contact dev@example.com\n",
        encoding="utf-8",
    )
    (source.workspace.workspace / "references").mkdir()
    (source.workspace.workspace / "references" / "brief.md").write_text(
        f"Bearer abcdefghijklmnop for Agent {source_agent_id}",
        encoding="utf-8",
    )

    definition_agents = export_project_agent_template_assets(
        source_project,
        [
            ProjectAgentTemplateSource(
                agent_id=source_agent_id,
                name="Research lead",
                role_description="Owns research synthesis",
                is_leader=True,
            )
        ],
        redact_values=(source_project_id, source_tenant_id, source_user_id),
    )

    serialized = json.dumps(definition_agents, ensure_ascii=False)
    assert set(definition_agents[0]) == {
        "name",
        "role_description",
        "is_leader",
        "is_enabled",
        "soul",
        "core_memory",
        "workspace_files",
    }
    assert str(source_agent_id) not in serialized
    assert str(source_project_id) not in serialized
    assert str(source_tenant_id) not in serialized
    assert str(source_user_id) not in serialized
    assert "super-secret" not in serialized
    assert "abcdefghijklmnop" not in serialized
    assert "dev@example.com" not in serialized
    assert "source_agent_id" not in serialized
    assert "project_id" not in serialized
    assert "tenant_id" not in serialized
    assert "session" not in serialized.casefold()
    assert "run_id" not in serialized

    target_project = tmp_path / "target-project"
    target_project.mkdir()
    new_agent_id = uuid.uuid4()
    instances = await instantiate_project_agent_template_assets(
        target_project,
        definition_agents,
        agent_ids=[new_agent_id],
    )

    assert instances[0].agent_id == new_agent_id
    assert instances[0].agent_id != source_agent_id
    assert instances[0].agent_dir == f".agents/{new_agent_id}"
    assert instances[0].is_leader is True
    assert instances[0].is_enabled is True
    target = project_agent_workspace(target_project, new_agent_id)
    assert "[redacted]" in target.soul.read_text(encoding="utf-8")
    assert "[redacted-id]" in target.memory.read_text(encoding="utf-8")
    assert "[redacted-email]" in target.memory.read_text(encoding="utf-8")
    assert (target.workspace / "references" / "brief.md").read_text(encoding="utf-8") == (
        "Bearer [redacted] for Agent [redacted-id]"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workspace_file",
    [
        {"path": "../escape.md", "content": "no"},
        {"path": "/absolute.md", "content": "no"},
        {"path": "secrets/token.txt", "content": "no"},
        {"path": ".env.production", "content": "no"},
        {"path": "credentials.json", "content": "no"},
    ],
)
async def test_template_import_rejects_traversal_and_omits_sensitive_names(
    tmp_path: Path,
    workspace_file: dict,
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    agent_id = uuid.uuid4()

    payload = {
        "name": "Agent",
        "role_description": "Role",
        "soul": "Soul",
        "core_memory": "Memory",
        "workspace_files": [workspace_file],
    }
    if workspace_file["path"] in {"../escape.md", "/absolute.md"}:
        with pytest.raises(ProjectAgentTemplateAssetError):
            await instantiate_project_agent_template_assets(
                project_root,
                [payload],
                agent_ids=[agent_id],
            )
        assert not (project_root / ".agents" / str(agent_id)).exists()
        return

    instances = await instantiate_project_agent_template_assets(
        project_root,
        [payload],
        agent_ids=[agent_id],
    )
    assert instances[0].agent_id == agent_id
    assert list((project_root / ".agents" / str(agent_id) / "workspace").rglob("*")) == []


@pytest.mark.asyncio
async def test_template_import_rejects_identity_and_runtime_fields(tmp_path: Path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    for forbidden_field in (
        "id",
        "agent_id",
        "source_agent_id",
        "project_id",
        "tenant_id",
        "user_id",
        "session_id",
        "run_id",
        "token_usage",
        "accounting",
    ):
        payload = {
            "name": "Agent",
            "soul": "Soul",
            "core_memory": "Memory",
            "workspace_files": [],
            forbidden_field: "forbidden",
        }
        with pytest.raises(ProjectAgentTemplateAssetError, match="Unsupported project Agent template fields"):
            await instantiate_project_agent_template_assets(project_root, [payload])


@pytest.mark.asyncio
async def test_export_rejects_symlinks_sensitive_names_binary_and_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    agent_id = uuid.uuid4()
    result = await create_project_agent_workspace(project_root, agent_id)
    source = ProjectAgentTemplateSource(agent_id=agent_id, name="Agent")

    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (result.workspace.workspace / "linked.md").symlink_to(outside)
    with pytest.raises(ProjectAgentTemplateAssetError, match="Symbolic links"):
        export_project_agent_template_assets(project_root, [source])
    (result.workspace.workspace / "linked.md").unlink()

    (result.workspace.workspace / "token.json").write_text("{}", encoding="utf-8")
    exported = export_project_agent_template_assets(project_root, [source])
    assert exported[0]["workspace_files"] == []
    (result.workspace.workspace / "token.json").unlink()

    (result.workspace.workspace / "binary.dat").write_bytes(b"\xff\xfe")
    with pytest.raises(ProjectAgentTemplateAssetError, match="UTF-8"):
        export_project_agent_template_assets(project_root, [source])
    (result.workspace.workspace / "binary.dat").unlink()

    monkeypatch.setattr(template_assets, "MAX_TEMPLATE_ASSET_BYTES", 10)
    result.workspace.soul.write_text("more than ten bytes", encoding="utf-8")
    with pytest.raises(ProjectAgentTemplateAssetError, match="total limit"):
        export_project_agent_template_assets(project_root, [source])


@pytest.mark.asyncio
async def test_import_rolls_back_only_new_agent_directories_on_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    original = template_assets._write_agent_assets
    calls = 0

    def fail_on_second(project: Path, agent_id: uuid.UUID, agent):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        original(project, agent_id, agent)

    monkeypatch.setattr(template_assets, "_write_agent_assets", fail_on_second)
    payload = {
        "name": "Agent",
        "soul": "Soul",
        "core_memory": "Memory",
        "workspace_files": [],
    }

    with pytest.raises(OSError, match="disk full"):
        await instantiate_project_agent_template_assets(
            project_root,
            [payload, {**payload, "name": "Agent 2"}],
            agent_ids=[first_id, second_id],
        )

    assert not (project_root / ".agents" / str(first_id)).exists()
    assert not (project_root / ".agents" / str(second_id)).exists()
