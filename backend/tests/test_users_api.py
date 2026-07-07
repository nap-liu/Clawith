from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api import users as users_api
from app.models.user import Identity, User

pytestmark = pytest.mark.asyncio


class ScalarResult:
    def __init__(self, value: int):
        self.value = value

    def scalar(self):
        return self.value


class RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class RecordingDB:
    def __init__(self, responses):
        self.responses = list(responses)
        self.executed = []

    async def execute(self, statement):
        compiled = statement.compile()
        self.executed.append(
            {
                "sql": str(statement),
                "params": dict(compiled.params),
            }
        )
        if not self.responses:
            raise AssertionError("Unexpected DB execute call")
        return self.responses.pop(0)


def _fake_user(
    *,
    display_name: str,
    created_at: datetime,
    tenant_id: uuid.UUID | None = None,
    email: str | None = None,
    phone: str | None = None,
    username: str | None = None,
    role: str = "member",
) -> User:
    identity = Identity(
        id=uuid.uuid4(),
        username=username or f"user_{uuid.uuid4().hex[:8]}",
        email=email or f"{uuid.uuid4().hex[:8]}@example.local",
        phone=phone,
        password_hash="x",
    )
    return User(
        id=uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        identity=identity,
        display_name=display_name,
        role=role,
        is_active=True,
        quota_message_limit=50,
        quota_message_period="permanent",
        quota_messages_used=0,
        quota_max_agents=2,
        quota_agent_ttl_hours=0,
        created_at=created_at,
        registration_source="web",
    )


async def test_list_users_returns_paginated_page_with_total_and_agent_counts():
    tenant_id = uuid.uuid4()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    page_users = [
        _fake_user(
            tenant_id=tenant_id,
            display_name=f"User {i:02d}",
            created_at=base + timedelta(minutes=i),
        )
        for i in [3, 2, 1, 0]
    ]
    db = RecordingDB(
        [
            ScalarResult(12),
            RowsResult([(page_users[0], 2), *[(u, 0) for u in page_users[1:]]]),
        ]
    )
    current_user = SimpleNamespace(role="org_admin", tenant_id=tenant_id)

    result = await users_api.list_users(
        tenant_id=None,
        page=3,
        page_size=4,
        search="",
        sort_order="desc",
        current_user=current_user,
        db=db,
    )

    assert result["total"] == 12
    assert result["page"] == 3
    assert result["page_size"] == 4
    assert [item.display_name for item in result["items"]] == [
        "User 03",
        "User 02",
        "User 01",
        "User 00",
    ]
    assert result["items"][0].agents_count == 2

    list_query = db.executed[1]
    assert "LIMIT" in list_query["sql"]
    assert "OFFSET" in list_query["sql"]
    assert any(value == 4 for value in list_query["params"].values())
    assert any(value == 8 for value in list_query["params"].values())


async def test_list_users_searches_identity_fields_and_org_admin_stays_inside_tenant():
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    matched = _fake_user(
        tenant_id=tenant_a,
        display_name="Alice Zhang",
        email="alice.zhang@example.local",
        phone="13800000001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db = RecordingDB([ScalarResult(1), RowsResult([(matched, 0)])])
    current_user = SimpleNamespace(role="org_admin", tenant_id=tenant_a)

    result = await users_api.list_users(
        tenant_id=str(tenant_b),
        page=1,
        page_size=15,
        search="13800000001",
        sort_order="desc",
        current_user=current_user,
        db=db,
    )

    assert result["total"] == 1
    assert [item.display_name for item in result["items"]] == ["Alice Zhang"]

    count_query = db.executed[0]
    assert "identities.username" in count_query["sql"]
    assert "identities.email" in count_query["sql"]
    assert "identities.phone" in count_query["sql"]
    assert tenant_a in count_query["params"].values()
    assert tenant_b not in count_query["params"].values()


async def test_platform_admin_can_page_requested_tenant():
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    matched = _fake_user(
        tenant_id=tenant_b,
        display_name="Tenant B User",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db = RecordingDB([ScalarResult(1), RowsResult([(matched, 0)])])
    current_user = SimpleNamespace(role="platform_admin", tenant_id=tenant_a)

    result = await users_api.list_users(
        tenant_id=str(tenant_b),
        page=1,
        page_size=15,
        search="",
        sort_order="desc",
        current_user=current_user,
        db=db,
    )

    assert result["total"] == 1
    assert [item.display_name for item in result["items"]] == ["Tenant B User"]
    assert tenant_b in db.executed[0]["params"].values()
