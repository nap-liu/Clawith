import uuid
from types import SimpleNamespace

import pytest

from app.api import enterprise as enterprise_api


class DummyResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class DummyDB:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return DummyResult(self.rows)


@pytest.mark.asyncio
async def test_list_org_members_handles_user_display_name_column(monkeypatch):
    member = SimpleNamespace(
        id=uuid.uuid4(),
        name="刘喜",
        email="liuxi@example.com",
        phone=None,
        title="",
        department_path="研发部",
        avatar_url=None,
        external_id="ou_123",
        provider_id=uuid.uuid4(),
    )
    db = DummyDB([(member, "Feishu", "feishu", "刘喜")])

    async def fake_derive_member_department_paths(_db, members):
        return {members[0].id: "总部/研发部"}

    monkeypatch.setattr(enterprise_api, "derive_member_department_paths", fake_derive_member_department_paths)

    response = await enterprise_api.list_org_members(
        search="刘喜",
        current_user=SimpleNamespace(role="platform_admin", tenant_id=None),
        db=db,
    )

    assert response == [
        {
            "id": str(member.id),
            "name": "刘喜",
            "email": "liuxi@example.com",
            "phone": None,
            "title": "",
            "department_path": "总部/研发部",
            "avatar_url": None,
            "external_id": "ou_123",
            "provider_id": str(member.provider_id),
            "provider_name": "Feishu",
            "provider_type": "feishu",
            "user_display_name": "刘喜",
        }
    ]
