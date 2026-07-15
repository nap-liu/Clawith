import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User
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


class _RecordingExecuteDB:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)


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
            ExternalDepartment(external_id="2", name="Large Department", parent_external_id="1"),
            ExternalDepartment(external_id="3", name="Large Department Team", parent_external_id="2"),
            ExternalDepartment(external_id="4", name="Small Department", parent_external_id="1"),
        ]

    async def fetch_users(self, department_external_id: str):
        self.fetched_department_ids.append(department_external_id)
        return [
            ExternalUser(
                external_id=f"user-{department_external_id}",
                name=f"User {department_external_id}",
                unionid=f"union-{department_external_id}",
            )
        ]


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

    assert adapter.fetched_department_ids == ["4"]
    assert result["members"] == 1
    assert result["user_fetch_skipped_departments"] == 2
    assert adapter.reconcile_called is False
    assert "Reconcile skipped because department user fetch was intentionally skipped" in result["errors"]


def test_dingtalk_sync_skip_department_names_default_to_empty():
    adapter = DingTalkOrgSyncAdapter(config={"app_key": "app-key", "app_secret": "app-secret"})

    assert adapter._configured_user_fetch_skip_department_names() == set()


def test_reconcile_disables_session_synchronization_for_datetime_comparisons():
    adapter = _DummyAdapter()
    db = _RecordingExecuteDB()

    asyncio.run(adapter._reconcile(db, uuid.uuid4(), datetime.now(timezone.utc)))

    assert len(db.statements) == 2
    for statement in db.statements:
        assert statement.get_execution_options()["synchronize_session"] is False


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
        "1": "Root",
        "2": "Root/Large Department",
        "3": "Root/Large Department/Large Department Team",
        "4": "Root/Small Department",
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
        "1": "Root",
        "2": "Root/Large Department",
        "4": "Root/Small Department",
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


def test_build_department_path_map_treats_external_zero_root_as_empty_path():
    root_id = uuid.uuid4()
    child_id = uuid.uuid4()

    departments = [
        SimpleNamespace(id=child_id, external_id="200", name="研发部", parent_id=root_id),
        SimpleNamespace(id=root_id, external_id="0", name="Root", parent_id=None),
    ]

    path_map = build_department_path_map(departments)

    assert path_map[root_id] == ""
    assert path_map[child_id] == "研发部"


def test_normalize_contact_for_match_strips_common_mobile_formatting():
    assert normalize_contact_for_match("+86 138-0013-8000") == "8613800138000"
    assert normalize_contact_for_match(" 138 0013 8000 ") == "13800138000"


def test_normalize_contact_for_match_keeps_email_lowercase():
    assert normalize_contact_for_match(" Alice@Example.COM ") == "alice@example.com"


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


async def _seed_provider(tenant_id: uuid.UUID, provider_type: str) -> IdentityProvider:
    async with async_session() as db:
        provider = IdentityProvider(
            provider_type=provider_type,
            name=f"{provider_type} {uuid.uuid4().hex[:6]}",
            is_active=True,
            config={},
            tenant_id=tenant_id,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)
        return provider


async def _seed_user(tenant_id: uuid.UUID, *, email: str | None = None, phone: str | None = None) -> User:
    async with async_session() as db:
        identity = Identity(
            username=f"user_{uuid.uuid4().hex[:10]}",
            email=email,
            phone=phone,
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant_id,
            display_name=f"User {uuid.uuid4().hex[:6]}",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


@pytest.mark.asyncio
async def test_org_sync_auto_creates_user_from_dingtalk_member_mobile():
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    phone = f"8613{uuid.uuid4().int % 10**9:09d}"

    external_user = ExternalUser(
        external_id=f"dt_{uuid.uuid4().hex[:8]}",
        unionid=f"union_{uuid.uuid4().hex[:8]}",
        name="钉钉新成员",
        mobile=phone,
        email="",
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        assert stats["user_created"] is True
        assert stats["profile_synced"] is True
        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == external_user.external_id,
                )
            )
        ).scalar_one()
        assert member.user_id is not None
        created_user = await db.get(User, member.user_id)
        assert created_user is not None
        assert created_user.tenant_id == tenant.id
        assert created_user.registration_source == "dingtalk_org_sync"


@pytest.mark.asyncio
async def test_org_sync_links_existing_user_by_mobile_before_email():
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    phone = f"8613{uuid.uuid4().int % 10**9:09d}"
    shared_email = f"shared-{uuid.uuid4().hex[:8]}@example.com"
    email_user = await _seed_user(tenant.id, email=shared_email, phone=f"8613{uuid.uuid4().int % 10**9:09d}")
    mobile_user = await _seed_user(tenant.id, email=f"mobile-{uuid.uuid4().hex[:8]}@example.com", phone=phone)

    external_user = ExternalUser(
        external_id=f"dt_{uuid.uuid4().hex[:8]}",
        unionid=f"union_{uuid.uuid4().hex[:8]}",
        name="手机号优先",
        mobile=phone,
        email=shared_email,
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == external_user.external_id,
                )
            )
        ).scalar_one()
        assert stats["user_created"] is False
        assert stats["user_linked"] is True
        assert member.user_id == mobile_user.id
        assert member.user_id != email_user.id


@pytest.mark.asyncio
async def test_org_sync_missing_mobile_creates_external_only_canonical_user():
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)

    external_user = ExternalUser(
        external_id=f"dt_{uuid.uuid4().hex[:8]}",
        unionid=f"union_{uuid.uuid4().hex[:8]}",
        name="无手机号成员",
        mobile="",
        email=f"nomobile-{uuid.uuid4().hex[:8]}@example.com",
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == external_user.external_id,
                )
            )
        ).scalar_one()
        assert stats["user_created"] is True
        assert stats["user_skipped_no_phone"] is False
        assert member.user_id is not None
        canonical_user = await db.get(User, member.user_id)
        assert canonical_user.identity_id is None


@pytest.mark.asyncio
async def test_org_sync_non_dingtalk_still_links_existing_user_by_email():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id, "feishu")
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    email = f"feishu-{uuid.uuid4().hex[:8]}@example.com"
    existing_user = await _seed_user(tenant.id, email=email)

    external_user = ExternalUser(
        external_id=f"fs_{uuid.uuid4().hex[:8]}",
        unionid=f"union_{uuid.uuid4().hex[:8]}",
        name="飞书已有用户",
        mobile="",
        email=email,
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == external_user.external_id,
                )
            )
        ).scalar_one()
        assert stats["user_created"] is False
        assert stats["user_linked"] is True
        assert member.user_id == existing_user.id


@pytest.mark.asyncio
async def test_org_sync_dingtalk_missing_mobile_clears_phone_and_keeps_canonical_user():
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    old_phone = f"8613{uuid.uuid4().int % 10**9:09d}"
    external_id = f"dt_{uuid.uuid4().hex[:8]}"
    unionid = f"union_{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            external_id=external_id,
            unionid=unionid,
            name="历史手机号成员",
            phone=old_phone,
            status="active",
        )
        db.add(member)
        await db.commit()
        member_id = member.id

    external_user = ExternalUser(
        external_id=external_id,
        unionid=unionid,
        name="历史手机号成员",
        mobile="",
        email="",
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        member = await db.get(OrgMember, member_id)
        assert stats["user_created"] is True
        assert stats["user_skipped_no_phone"] is False
        assert member.phone is None
        assert member.user_id is not None
        canonical_user = await db.get(User, member.user_id)
        assert canonical_user.identity_id is None


@pytest.mark.asyncio
async def test_org_sync_dingtalk_backfills_legacy_member_tenant_before_provisioning():
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    phone = f"8613{uuid.uuid4().int % 10**9:09d}"
    external_id = f"dt_{uuid.uuid4().hex[:8]}"
    unionid = f"union_{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        member = OrgMember(
            tenant_id=None,
            provider_id=provider.id,
            external_id=external_id,
            unionid=unionid,
            name="历史空租户成员",
            phone=phone,
            status="active",
        )
        db.add(member)
        await db.commit()
        member_id = member.id

    external_user = ExternalUser(
        external_id=external_id,
        unionid=unionid,
        name="历史空租户成员",
        mobile=phone,
        email="",
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        member = await db.get(OrgMember, member_id)
        assert stats["user_created"] is True
        assert member.tenant_id == tenant.id
        assert member.user_id is not None


@pytest.mark.asyncio
async def test_org_sync_dingtalk_auto_create_disabled_does_not_link_by_email():
    tenant = await _seed_tenant()
    email = f"dingtalk-email-disabled-{uuid.uuid4().hex[:8]}@example.com"
    existing_user = await _seed_user(tenant.id, email=email)

    async with async_session() as db:
        provider = IdentityProvider(
            provider_type="dingtalk",
            name=f"DingTalk Disabled {uuid.uuid4().hex[:6]}",
            is_active=True,
            config={"auto_create_users_on_sync": False},
            tenant_id=tenant.id,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)

    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    external_user = ExternalUser(
        external_id=f"dt_{uuid.uuid4().hex[:8]}",
        unionid=f"union_{uuid.uuid4().hex[:8]}",
        name="禁用自动创建",
        mobile="",
        email=email,
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == external_user.external_id,
                )
            )
        ).scalar_one()
        assert stats["user_created"] is False
        assert stats["user_linked"] is False
        assert member.user_id is None
        assert member.user_id != existing_user.id
