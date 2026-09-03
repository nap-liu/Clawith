import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.services.org_sync_base import BaseOrgSyncAdapter
from app.services.org_sync_models import ExternalDepartment


class _FakeDB:
    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def commit(self):
        return None

    async def flush(self):
        return None


class _OrderedVendorAdapter(BaseOrgSyncAdapter):
    provider_type = "wecom"

    def __init__(self, *, skip_users: bool = False):
        super().__init__()
        self.provider = SimpleNamespace(
            id="provider-1",
            provider_type="wecom",
            config={},
        )
        self.applied_groups: list[str] = []
        self.reconciled = False
        self.skip_users = skip_users

    @property
    def api_base_url(self) -> str:
        return "https://provider.invalid"

    async def get_access_token(self) -> str:
        return "unused"

    async def _ensure_provider(self, db):
        return self.provider

    async def fetch_departments(self):
        return [
            ExternalDepartment(
                external_id="child",
                name="Child",
                parent_external_id="parent",
            ),
            ExternalDepartment(external_id="parent", name="Parent"),
        ]

    async def fetch_users(self, department_external_id: str):
        return []

    def _should_skip_department_user_fetch(self, dept):
        return self.skip_users

    async def _upsert_department(self, db, provider, dept):
        self.applied_groups.append(dept.external_id)

    async def _rebuild_department_paths(self, db, provider_id):
        return {}

    async def _refresh_member_department_paths(self, db, provider_id):
        return None

    async def _update_member_counts(self, db, provider_id):
        return None

    async def _reconcile(self, db, provider_id, sync_start):
        self.reconciled = True


def test_vendor_lifecycle_applies_validated_snapshot_in_topological_order():
    adapter = _OrderedVendorAdapter()

    result = asyncio.run(adapter.sync_org_structure(_FakeDB()))

    assert result["errors"] == []
    assert adapter.applied_groups == ["parent", "child"]
    assert adapter.reconciled is True


def test_vendor_lifecycle_does_not_write_when_any_group_user_fetch_is_skipped():
    adapter = _OrderedVendorAdapter(skip_users=True)

    result = asyncio.run(adapter.sync_org_structure(_FakeDB()))

    assert result["departments"] == 0
    assert result["members"] == 0
    assert adapter.applied_groups == []
    assert adapter.reconciled is False
    assert result["user_fetch_skipped_departments"] == 2
    assert any("snapshot fetch was incomplete" in error for error in result["errors"])
