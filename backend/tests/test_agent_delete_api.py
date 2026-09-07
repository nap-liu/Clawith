import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock

from app.api import agents as agents_api


class DummyResult:
    def __init__(self, values=None):
        self._values = list(values or [])

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


@pytest.mark.asyncio
async def test_archive_agent_task_history_writes_json_snapshot(tmp_path):
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    created_at = datetime.now(UTC)

    task = SimpleNamespace(
        id=task_id,
        title="Review PR",
        description="Check lore trailers",
        type="todo",
        status="done",
        priority="high",
        assignee="self",
        created_by=uuid.uuid4(),
        due_date=None,
        supervision_target_user_id=None,
        supervision_target_name=None,
        supervision_channel=None,
        remind_schedule=None,
        created_at=created_at,
        updated_at=created_at,
        completed_at=created_at,
    )
    log = SimpleNamespace(
        id=uuid.uuid4(),
        content="Completed review and left comments",
        created_at=created_at,
    )

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[
            DummyResult([task]),
            DummyResult([log]),
        ]),
    )

    archive_dir = tmp_path / "_archived" / f"{agent_id}_20260325_120000"
    archive_path = await agents_api._archive_agent_task_history(db, agent_id, archive_dir)

    assert archive_path == archive_dir / "task_history.json"
    payload = json.loads(archive_path.read_text(encoding="utf-8"))
    assert payload["agent_id"] == str(agent_id)
    assert payload["tasks"][0]["id"] == str(task_id)
    assert payload["tasks"][0]["logs"][0]["content"] == "Completed review and left comments"
