import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.database import async_session, engine
from app.models.identity import IdentityProvider
from app.models.tenant import Tenant
from app.services.org_sync_adapter import (
    BaseOrgSyncAdapter,
    DingTalkOrgSyncAdapter,
    ExternalDepartment,
    ExternalUser,
    GoogleWorkspaceOrgSyncAdapter,
    SYNC_ADAPTER_CLASSES,
    build_department_path_map,
    normalize_contact_for_match,
)
from app.services.dingtalk_identity_reconciliation import (
    dingtalk_legacy_identity_reconciler,
)


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


class _DummyAdapter(BaseOrgSyncAdapter):
    provider_type = "feishu"

    @property
    def api_base_url(self) -> str:
        return "https://example.com"

    async def get_access_token(self) -> str:
        return "token"

    async def fetch_departments(self):
        return []

    async def fetch_users(self, department_external_id: str):
        return []


class _FakeDB:
    def __init__(self):
        self.flush_calls = 0

    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def flush(self):
        self.flush_calls += 1

    async def commit(self):
        return None


class _FakeDingTalkResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeDingTalkClient:
    def __init__(self):
        self.department_list_requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, params=None, json=None):
        if url == DingTalkOrgSyncAdapter.DINGTALK_AUTH_SCOPES_URL:
            return _FakeDingTalkResponse({
                "errcode": 0,
                "auth_org_scopes": {
                    "authed_dept": [42],
                    "authed_user": [],
                },
            })

        if url == DingTalkOrgSyncAdapter.DINGTALK_DEPT_GET_URL:
            assert json == {"dept_id": 42}
            return _FakeDingTalkResponse({
                "errcode": 0,
                "result": {
                    "dept_id": 42,
                    "name": "研发部",
                    "parent_id": 1,
                    "member_count": 2,
                },
            })

        if url == DingTalkOrgSyncAdapter.DINGTALK_DEPT_LIST_URL:
            dept_id = json["dept_id"]
            self.department_list_requests.append(dept_id)
            if dept_id == 1:
                return _FakeDingTalkResponse({
                    "errcode": 50004,
                    "errmsg": "请求的部门id不在授权范围内",
                })
            if dept_id == 42:
                return _FakeDingTalkResponse({
                    "errcode": 0,
                    "result": [
                        {
                            "dept_id": 43,
                            "name": "平台组",
                            "parent_id": 42,
                            "member_count": 1,
                        }
                    ],
                })
            if dept_id == 43:
                return _FakeDingTalkResponse({"errcode": 0, "result": []})

        raise AssertionError(f"Unexpected DingTalk request: {url} {json}")


class _FakeDingTalkScopeFallbackClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, params=None, json=None):
        self.calls.append(("POST", url))
        assert url == DingTalkOrgSyncAdapter.DINGTALK_AUTH_SCOPES_URL
        return _FakeDingTalkResponse({
            "errcode": 15,
            "errmsg": "Remote service error[submsg=远程服务不存在]",
        })

    async def get(self, url, params=None):
        self.calls.append(("GET", url))
        assert url == "https://oapi.dingtalk.com/auth/scopes"
        return _FakeDingTalkResponse({
            "errcode": 0,
            "errmsg": "ok",
            "auth_org_scopes": {
                "authed_dept": [42],
                "authed_user": [],
            },
        })


class _FakeDingTalkScopeFailureClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, params=None, json=None):
        self.calls.append(("POST", url))
        return _FakeDingTalkResponse({"errcode": 15, "errmsg": "topapi unavailable"})

    async def get(self, url, params=None):
        self.calls.append(("GET", url))
        return _FakeDingTalkResponse({"errcode": 16, "errmsg": "legacy unavailable"})


class _FakeDingTalkLargeDepartmentClient:
    def __init__(self):
        self.department_list_requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, params=None, json=None):
        if url == DingTalkOrgSyncAdapter.DINGTALK_AUTH_SCOPES_URL:
            return _FakeDingTalkResponse({
                "errcode": 0,
                "auth_org_scopes": {
                    "authed_dept": [1],
                    "authed_user": [],
                },
            })

        if url == DingTalkOrgSyncAdapter.DINGTALK_DEPT_GET_URL:
            assert json == {"dept_id": 1}
            return _FakeDingTalkResponse({
                "errcode": 0,
                "result": {"dept_id": 1, "name": "Example Corp"},
            })

        if url == DingTalkOrgSyncAdapter.DINGTALK_DEPT_LIST_URL:
            dept_id = json["dept_id"]
            self.department_list_requests.append(dept_id)
            if dept_id == 1:
                return _FakeDingTalkResponse({
                    "errcode": 0,
                    "result": [
                        {"dept_id": 2, "name": "Large Department", "parent_id": 1},
                        {"dept_id": 4, "name": "Small Department", "parent_id": 1},
                    ],
                })
            if dept_id == 2:
                return _FakeDingTalkResponse({
                    "errcode": 0,
                    "result": [{"dept_id": 3, "name": "Large Department Team", "parent_id": 2}],
                })
            if dept_id == 3:
                return _FakeDingTalkResponse({"errcode": 0, "result": []})
            if dept_id == 4:
                return _FakeDingTalkResponse({"errcode": 0, "result": []})

        raise AssertionError(f"Unexpected DingTalk request: {url} {json}")


class _FakeDingTalkRateLimitClient:
    def __init__(self):
        self.department_list_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, params=None, json=None):
        if url == DingTalkOrgSyncAdapter.DINGTALK_AUTH_SCOPES_URL:
            return _FakeDingTalkResponse({
                "errcode": 0,
                "auth_org_scopes": {
                    "authed_dept": [1],
                    "authed_user": [],
                },
            })

        if url == DingTalkOrgSyncAdapter.DINGTALK_DEPT_GET_URL:
            assert json == {"dept_id": 1}
            return _FakeDingTalkResponse({
                "errcode": 0,
                "result": {"dept_id": 1, "name": "Example Corp"},
            })

        if url == DingTalkOrgSyncAdapter.DINGTALK_DEPT_LIST_URL:
            self.department_list_calls += 1
            if self.department_list_calls == 1:
                return _FakeDingTalkResponse({
                    "errcode": 90002,
                    "errmsg": "当前所有钉钉应用调用该接口次数过多",
                })
            return _FakeDingTalkResponse({"errcode": 0, "result": []})

        raise AssertionError(f"Unexpected DingTalk request: {url} {json}")


class _SyncAdapterWithFailure(_DummyAdapter):
    def __init__(self):
        super().__init__()
        self.reconcile_called = False
        self.member_counts_updated = False
        self.provider = SimpleNamespace(id="provider-1", config={})

    async def _ensure_provider(self, db):
        return self.provider

    async def _upsert_department(self, db, provider, dept):
        return None

    async def _upsert_member(self, db, provider, user, department_external_id):
        raise ValueError("unionid is required")

    async def _reconcile(self, db, provider_id, sync_start):
        self.reconcile_called = True

    async def _update_member_counts(self, db, provider_id):
        self.member_counts_updated = True

    async def _rebuild_department_paths(self, db, provider_id):
        return {}

    async def _refresh_member_department_paths(self, db, provider_id):
        return None

    async def fetch_departments(self):
        return [SimpleNamespace(external_id="dept-1", name="Dept 1")]

    async def fetch_users(self, department_external_id: str):
        return [ExternalUser(external_id="user-1", name="Alice", unionid="")]


class _DingTalkSyncAdapterWithSkippedDepartments(DingTalkOrgSyncAdapter):
    def __init__(self):
        super().__init__(config={
            "app_key": "app-key",
            "app_secret": "app-secret",
            "skip_user_fetch_department_names": ["Large Department"],
        })
        self.provider = SimpleNamespace(id="provider-1", config={})
        self.fetched_department_ids: list[str] = []
        self.member_counts_updated = False
        self.reconcile_called = False
        self._dept_path_map = {
            "1": "Root",
            "2": "Large Department",
            "3": "Large Department/Large Department Team",
            "4": "Small Department",
        }

    async def _ensure_provider(self, db):
        return self.provider

    async def _upsert_department(self, db, provider, dept):
        return None

    async def _upsert_member(self, db, provider, user, department_external_id):
        return {"profile_synced": True}

    async def _reconcile(self, db, provider_id, sync_start):
        self.reconcile_called = True

    async def _update_member_counts(self, db, provider_id):
        self.member_counts_updated = True

    async def _rebuild_department_paths(self, db, provider_id):
        return {}

    async def _refresh_member_department_paths(self, db, provider_id):
        return None

    async def fetch_departments(self):
        return [
            ExternalDepartment(external_id="1", name="Root"),
            ExternalDepartment(external_id="2", name="Large Department", parent_external_id="1"),
            ExternalDepartment(external_id="3", name="Large Department Team", parent_external_id="2"),
            ExternalDepartment(external_id="4", name="Small Department", parent_external_id="1"),
        ]

    async def fetch_users(self, department_external_id: str):
        self.fetched_department_ids.append(department_external_id)
        if department_external_id == "1":
            return []
        return [
            ExternalUser(
                external_id=f"user-{department_external_id}",
                name=f"User {department_external_id}",
                unionid=f"union-{department_external_id}",
            )
        ]


class _MultiGroupSnapshotAdapter(_SyncAdapterWithFailure):
    provider_type = "wecom"

    def __init__(self):
        super().__init__()
        self.provider.provider_type = "wecom"
        self.applied_user = None

    async def fetch_departments(self):
        return [
            ExternalDepartment(external_id="dept-a", name="A"),
            ExternalDepartment(external_id="dept-b", name="B"),
        ]

    async def fetch_users(self, department_external_id: str):
        return [ExternalUser(external_id="same-user", name="Same User")]

    async def _upsert_member(self, db, provider, user, department_external_id):
        self.applied_user = user
        return {}


def test_validate_member_identifiers_requires_unionid_for_feishu():
    adapter = _DummyAdapter()
    provider = SimpleNamespace(provider_type="feishu")
    user = ExternalUser(external_id="ou_123", name="Alice", unionid="")

    with pytest.raises(ValueError, match="unionid is required"):
        adapter._validate_member_identifiers(provider, user)


def test_validate_member_identifiers_rejects_unionid_equal_to_external_id():
    adapter = _DummyAdapter()
    provider = SimpleNamespace(provider_type="dingtalk")
    user = ExternalUser(external_id="same-id", name="Bob", unionid="same-id")

    with pytest.raises(ValueError, match="must not equal external_id"):
        adapter._validate_member_identifiers(provider, user)


def test_validate_member_identifiers_allows_wecom_without_unionid():
    adapter = _DummyAdapter()
    provider = SimpleNamespace(provider_type="wecom")
    user = ExternalUser(external_id="zhangsan", name="Zhang San", unionid="")

    adapter._validate_member_identifiers(provider, user)


def test_sync_org_structure_skips_reconcile_after_member_failure():
    adapter = _SyncAdapterWithFailure()
    db = _FakeDB()

    result = asyncio.run(adapter.sync_org_structure(db))

    assert adapter.reconcile_called is False
    assert adapter.member_counts_updated is True
    assert "Reconcile skipped due to partial sync failures" in result["errors"]


def test_dingtalk_sync_skips_configured_department_user_fetch():
    adapter = _DingTalkSyncAdapterWithSkippedDepartments()
    db = _FakeDB()

    result = asyncio.run(adapter.sync_org_structure(db))

    assert adapter.fetched_department_ids == ["1", "4"]
    assert result["departments"] == 0
    assert result["members"] == 0
    assert result["user_fetch_skipped_departments"] == 2
    assert adapter.member_counts_updated is False
    assert adapter.reconcile_called is False
    assert any("snapshot fetch was incomplete" in error for error in result["errors"])


def test_provider_snapshot_merges_repeated_user_group_memberships():
    adapter = _MultiGroupSnapshotAdapter()

    result = asyncio.run(adapter.sync_org_structure(_FakeDB()))

    assert result["members"] == 1
    assert adapter.applied_user.department_ids == ["dept-a", "dept-b"]


def test_dingtalk_sync_skip_department_names_default_to_empty():
    adapter = DingTalkOrgSyncAdapter(config={"app_key": "app-key", "app_secret": "app-secret"})

    assert adapter._configured_user_fetch_skip_department_names() == set()


def test_google_workspace_adapter_parses_legacy_service_account_json_string():
    adapter = GoogleWorkspaceOrgSyncAdapter(
        config={
            "customer_id": "my_customer",
            "client_secret": '{"client_email":"svc@example.iam.gserviceaccount.com","private_key":"-----BEGIN PRIVATE KEY-----\\\\nabc\\\\n-----END PRIVATE KEY-----\\\\n"}',
            "delegated_admin_email": "admin@example.com",
        }
    )

    assert adapter.customer_id == "my_customer"
    assert adapter.delegated_admin_email == "admin@example.com"
    assert adapter.service_account["client_email"] == "svc@example.iam.gserviceaccount.com"


def test_google_workspace_adapter_uses_admin_authorization_email_as_primary_identity():
    adapter = GoogleWorkspaceOrgSyncAdapter(
        config={
            "client_id": "oauth-client-id.apps.googleusercontent.com",
            "client_secret": "oauth-client-secret",
            "google_admin_authorized_email": "admin@example.com",
        }
    )

    assert adapter.client_id == "oauth-client-id.apps.googleusercontent.com"
    assert adapter.client_secret == "oauth-client-secret"
    assert adapter.delegated_admin_email == "admin@example.com"
    assert adapter.service_account == {}


def test_google_workspace_adapter_registered():
    assert SYNC_ADAPTER_CLASSES["google_workspace"] is GoogleWorkspaceOrgSyncAdapter


def test_dingtalk_fetch_departments_starts_from_authorized_scope(monkeypatch):
    fake_client = _FakeDingTalkClient()
    monkeypatch.setattr(
        "app.services.org_sync_adapter.httpx.AsyncClient",
        lambda *args, **kwargs: fake_client,
    )

    adapter = DingTalkOrgSyncAdapter(config={"app_key": "app-key", "app_secret": "app-secret"})

    async def fake_get_access_token():
        return "access-token"

    adapter.get_access_token = fake_get_access_token

    departments = asyncio.run(adapter.fetch_departments())

    assert [dept.external_id for dept in departments] == ["42", "43"]
    assert departments[0].parent_external_id is None
    assert departments[1].parent_external_id == "42"
    assert fake_client.department_list_requests == [42, 43]
    assert adapter._dept_path_map == {"42": "研发部", "43": "研发部/平台组"}


def test_dingtalk_authorized_scope_falls_back_to_legacy_auth_scopes():
    fake_client = _FakeDingTalkScopeFallbackClient()
    adapter = DingTalkOrgSyncAdapter(config={"app_key": "app-key", "app_secret": "app-secret"})

    dept_ids = asyncio.run(adapter._fetch_authorized_department_ids(fake_client, "access-token"))

    assert dept_ids == [42]
    assert fake_client.calls == [
        ("POST", DingTalkOrgSyncAdapter.DINGTALK_AUTH_SCOPES_URL),
        ("GET", "https://oapi.dingtalk.com/auth/scopes"),
    ]


def test_dingtalk_authorized_scope_failure_never_expands_to_root():
    fake_client = _FakeDingTalkScopeFailureClient()
    adapter = DingTalkOrgSyncAdapter(config={"app_key": "app-key", "app_secret": "app-secret"})

    with pytest.raises(RuntimeError, match="authorization scope could not be determined"):
        asyncio.run(
            adapter._fetch_authorized_department_ids(fake_client, "access-token")
        )

    assert fake_client.calls == [
        ("POST", DingTalkOrgSyncAdapter.DINGTALK_AUTH_SCOPES_URL),
        ("GET", DingTalkOrgSyncAdapter.DINGTALK_AUTH_SCOPES_LEGACY_URL),
    ]


def test_dingtalk_fetch_departments_expands_departments_by_default(monkeypatch):
    fake_client = _FakeDingTalkLargeDepartmentClient()
    monkeypatch.setattr(
        "app.services.org_sync_adapter.httpx.AsyncClient",
        lambda *args, **kwargs: fake_client,
    )

    adapter = DingTalkOrgSyncAdapter(config={"app_key": "app-key", "app_secret": "app-secret"})

    async def fake_get_access_token():
        return "access-token"

    adapter.get_access_token = fake_get_access_token

    departments = asyncio.run(adapter.fetch_departments())

    assert [dept.external_id for dept in departments] == ["1", "2", "4", "3"]
    assert fake_client.department_list_requests == [1, 2, 4, 3]
    assert adapter._dept_path_map == {
        "1": "Example Corp",
        "2": "Example Corp/Large Department",
        "3": "Example Corp/Large Department/Large Department Team",
        "4": "Example Corp/Small Department",
    }


def test_dingtalk_fetch_departments_does_not_expand_configured_skipped_departments(monkeypatch):
    fake_client = _FakeDingTalkLargeDepartmentClient()
    monkeypatch.setattr(
        "app.services.org_sync_adapter.httpx.AsyncClient",
        lambda *args, **kwargs: fake_client,
    )

    adapter = DingTalkOrgSyncAdapter(config={
        "app_key": "app-key",
        "app_secret": "app-secret",
        "skip_user_fetch_department_names": ["Large Department"],
    })

    async def fake_get_access_token():
        return "access-token"

    adapter.get_access_token = fake_get_access_token

    departments = asyncio.run(adapter.fetch_departments())

    assert [dept.external_id for dept in departments] == ["1", "2", "4"]
    assert fake_client.department_list_requests == [1, 4]
    assert adapter._dept_path_map == {
        "1": "Example Corp",
        "2": "Example Corp/Large Department",
        "4": "Example Corp/Small Department",
    }


def test_dingtalk_fetch_departments_retries_rate_limited_department_requests(monkeypatch):
    fake_client = _FakeDingTalkRateLimitClient()
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(
        "app.services.org_sync_adapter.httpx.AsyncClient",
        lambda *args, **kwargs: fake_client,
    )
    monkeypatch.setattr("app.services.org_sync_adapter.asyncio.sleep", fake_sleep)

    adapter = DingTalkOrgSyncAdapter(config={"app_key": "app-key", "app_secret": "app-secret"})

    async def fake_get_access_token():
        return "access-token"

    adapter.get_access_token = fake_get_access_token

    departments = asyncio.run(adapter.fetch_departments())

    assert [dept.external_id for dept in departments] == ["1"]
    assert fake_client.department_list_calls == 2
    assert DingTalkOrgSyncAdapter.DINGTALK_RATE_LIMIT_RETRY_SECONDS in sleeps


def test_build_department_path_map_reconstructs_name_chain_from_internal_tree():
    root_id = uuid.uuid4()
    child_id = uuid.uuid4()
    leaf_id = uuid.uuid4()

    departments = [
        SimpleNamespace(id=leaf_id, external_id="leaf", name="平台组", parent_id=child_id),
        SimpleNamespace(id=child_id, external_id="child", name="研发部", parent_id=root_id),
        SimpleNamespace(id=root_id, external_id="root", name="总部", parent_id=None),
    ]

    path_map = build_department_path_map(departments)

    assert path_map[root_id] == "总部"
    assert path_map[child_id] == "总部/研发部"
    assert path_map[leaf_id] == "总部/研发部/平台组"


@pytest.mark.parametrize("external_id", ["0", "1", "root"])
def test_build_department_path_map_treats_transport_root_as_empty_path(external_id):
    root_id = uuid.uuid4()
    child_id = uuid.uuid4()

    departments = [
        SimpleNamespace(id=child_id, external_id="200", name="研发部", parent_id=root_id),
        SimpleNamespace(id=root_id, external_id=external_id, name="Root", parent_id=None),
    ]

    path_map = build_department_path_map(departments)

    assert path_map[root_id] == ""
    assert path_map[child_id] == "研发部"


def test_normalize_contact_for_match_strips_common_mobile_formatting():
    assert normalize_contact_for_match("+86 138-0013-8000") == "8613800138000"
    assert normalize_contact_for_match(" 138 0013 8000 ") == "13800138000"


def test_normalize_contact_for_match_keeps_email_lowercase():
    assert normalize_contact_for_match(" Alice@Example.COM ") == "alice@example.com"


def test_dingtalk_fresh_claims_reject_different_email_variants():
    from app.services.directory_identity_claims import VerifiedDirectoryClaims

    claims = VerifiedDirectoryClaims(
        tenant_id=uuid.uuid4(),
        provider_id=uuid.uuid4(),
        external_id="staff-conflict",
        observed_at=datetime.now(timezone.utc),
        raw_email="personal@example.com",
        raw_org_email="employee@example.com",
        raw_mobile="13800138000",
        source="test",
    )

    assert claims.has_alternate_email_conflict is True
    assert claims.email is None
    assert claims.phone == "13800138000"


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        tenant = Tenant(name="Org Sync Tenant", slug=f"org-sync-{uuid.uuid4().hex[:10]}")
        db.add(tenant)
        await db.commit()
        await db.refresh(tenant)
        return tenant


async def _seed_dingtalk_provider(tenant_id: uuid.UUID) -> IdentityProvider:
    async with async_session() as db:
        provider = IdentityProvider(
            provider_type="dingtalk",
            name=f"DingTalk {uuid.uuid4().hex[:6]}",
            is_active=True,
            config={},
            tenant_id=tenant_id,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)
        return provider


@pytest.mark.asyncio
async def test_dingtalk_sync_releases_each_subject_lock_before_outer_commit():
    tenant = await _seed_tenant()
    seeded_provider = await _seed_dingtalk_provider(tenant.id)
    external_ids = [
        f"staff-lock-{uuid.uuid4().hex[:8]}",
        f"staff-lock-{uuid.uuid4().hex[:8]}",
    ]
    async with async_session() as sync_db:
        provider = await sync_db.get(IdentityProvider, seeded_provider.id)
        adapter = DingTalkOrgSyncAdapter(
            provider=provider,
            tenant_id=tenant.id,
        )
        for index, external_id in enumerate(external_ids):
            await adapter._upsert_member(
                sync_db,
                provider,
                ExternalUser(
                    external_id=external_id,
                    unionid=f"union-lock-{index}-{uuid.uuid4().hex[:8]}",
                    name=f"Lock User {index}",
                    email=f"lock-{uuid.uuid4().hex[:8]}@example.com",
                    mobile="",
                    status="active",
                    raw_data={
                        "userid": external_id,
                        "email": "",
                        "org_email": f"lock-{uuid.uuid4().hex[:8]}@example.com",
                        "mobile": "",
                    },
                ),
                "",
            )

        # The sync transaction is deliberately still open. A different
        # connection must nevertheless acquire the first employee's subject.
        async with async_session() as other_db:
            await asyncio.wait_for(
                dingtalk_legacy_identity_reconciler.acquire_subject_lock(
                    other_db,
                    tenant_id=tenant.id,
                    provider_id=provider.id,
                    external_id=external_ids[0],
                ),
                timeout=1,
            )
            await other_db.rollback()
        await sync_db.rollback()
@pytest.mark.asyncio
async def test_dingtalk_sync_releases_subject_lock_after_failed_savepoint():
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    external_id = f"staff-lock-error-{uuid.uuid4().hex[:8]}"
    async with async_session() as sync_db:
        with pytest.raises(DBAPIError):
            async with sync_db.begin_nested():
                async with dingtalk_legacy_identity_reconciler.session_subject_lock(
                    sync_db,
                    tenant_id=tenant.id,
                    provider_id=provider.id,
                    external_id=external_id,
                ):
                    await sync_db.execute(text("SELECT 1 / 0"))

        async with async_session() as other_db:
            await asyncio.wait_for(
                dingtalk_legacy_identity_reconciler.acquire_subject_lock(
                    other_db,
                    tenant_id=tenant.id,
                    provider_id=provider.id,
                    external_id=external_id,
                ),
                timeout=1,
            )
            await other_db.rollback()
        await sync_db.rollback()
