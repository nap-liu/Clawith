from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.agent_memory import load_agent_memory_snapshot
from app.services.agent_runtime_workspace import (
    bind_agent_runtime_workspace,
    current_agent_runtime_workspace,
    project_agent_runtime_workspace,
    resolve_agent_runtime_workspace,
    standard_agent_runtime_workspace,
)
from app.services.agent_tools import _execute_workspace_mutation, _tool_storage_key
from app.services.storage_runtime.agent_files import agent_storage_key


class _MemoryStorage:
    def __init__(self, files: dict[str, str]):
        self.files = files

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def exists(self, key: str) -> bool:
        return key in self.files

    async def read_text(self, key: str, **_kwargs) -> str:
        return self.files[key]


def _project_workspace():
    return project_agent_runtime_workspace(
        agent_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
    )


def test_project_runtime_routes_agent_assets_under_project_repo():
    workspace = _project_workspace()

    assert workspace.local_root == workspace.project_repo_root / ".agents" / str(workspace.agent_id)
    assert workspace.storage_key("soul.md").endswith(f".agents/{workspace.agent_id}/soul.md")
    assert workspace.storage_key("memory/memory.md").endswith(f".agents/{workspace.agent_id}/memory.md")
    assert workspace.storage_key("workspace/output.md").endswith(f".agents/{workspace.agent_id}/workspace/output.md")


def test_project_runtime_requires_matching_project_and_validates_durable_hint():
    workspace = _project_workspace()
    resolved = resolve_agent_runtime_workspace(
        agent_id=workspace.agent_id,
        agent_scope="project",
        agent_project_id=workspace.project_id,
        tenant_id=workspace.project_repo_root.parents[1].name,
        session_project_id=workspace.project_id,
        session_config={"agent_runtime_workspace": workspace.as_session_config()},
    )
    assert resolved == workspace

    with pytest.raises(ValueError, match="outside its owning project"):
        resolve_agent_runtime_workspace(
            agent_id=workspace.agent_id,
            agent_scope="project",
            agent_project_id=workspace.project_id,
            tenant_id=workspace.project_repo_root.parents[1].name,
            session_project_id=uuid.uuid4(),
        )


def test_runtime_binding_is_agent_isolated_and_standard_behavior_is_unchanged():
    workspace = _project_workspace()
    other_agent_id = uuid.uuid4()

    with bind_agent_runtime_workspace(workspace):
        assert current_agent_runtime_workspace(workspace.agent_id) == workspace
        assert current_agent_runtime_workspace(other_agent_id) == standard_agent_runtime_workspace(other_agent_id)
        storage_key, _path, _enterprise = _tool_storage_key(workspace.agent_id, "workspace/a.md")
        assert storage_key == workspace.storage_key("workspace/a.md")
        assert agent_storage_key(workspace.agent_id, "workspace/a.md") == workspace.storage_key("workspace/a.md")

    assert current_agent_runtime_workspace(workspace.agent_id) == standard_agent_runtime_workspace(workspace.agent_id)


@pytest.mark.asyncio
async def test_project_core_memory_is_loaded_without_daily_memory():
    workspace = _project_workspace()
    storage = _MemoryStorage({workspace.storage_key("memory/memory.md"): "PROJECT CORE"})

    with (
        bind_agent_runtime_workspace(workspace),
        patch("app.services.agent_memory.get_storage_backend", return_value=storage),
    ):
        snapshot = await load_agent_memory_snapshot(workspace.agent_id, today=date(2026, 8, 22))

    assert snapshot.core_memory == "PROJECT CORE"
    assert snapshot.structure_guide == ""
    assert snapshot.daily_records == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("write_file", {"path": "soul.md", "content": "changed"}),
        ("edit_file", {"path": "memory.md", "old_string": "a", "new_string": "b"}),
        ("delete_file", {"path": "memory/memory.md"}),
        ("move_file", {"source_path": "workspace/a.md", "destination_path": "soul.md"}),
    ],
)
async def test_project_agent_tools_cannot_mutate_owner_managed_identity_files(
    tool_name: str,
    arguments: dict[str, str],
    tmp_path: Path,
):
    workspace = _project_workspace()

    with bind_agent_runtime_workspace(workspace):
        result = await _execute_workspace_mutation(
            tool_name,
            arguments,
            agent_id=workspace.agent_id,
            base_dir=tmp_path,
            session_id=None,
        )

    assert "read-only" in result or "cannot be moved" in result
