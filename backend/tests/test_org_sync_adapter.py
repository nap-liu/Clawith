import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.org_sync_adapter import (
    BaseOrgSyncAdapter,
    DingTalkOrgSyncAdapter,
    ExternalUser,
    GoogleWorkspaceOrgSyncAdapter,
    SYNC_ADAPTER_CLASSES,
    build_department_path_map,
    normalize_contact_for_match,
)


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
