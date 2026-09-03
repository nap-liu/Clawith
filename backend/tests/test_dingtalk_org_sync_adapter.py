"""DingTalk adapter boundary behavior for canonical directory snapshots."""

import asyncio

import pytest

from app.services.org_sync_adapter import DingTalkOrgSyncAdapter
from app.services.org_sync_feishu import FeishuOrgSyncAdapter
from app.services.scim_directory import ScimSnapshotError
from app.services.vendor_directory_snapshot import build_vendor_scim_snapshot
from test_org_sync_adapter import _FakeDingTalkClient, _FakeDingTalkResponse


class _FakeDingTalkMissingInternalParentClient(_FakeDingTalkClient):
    async def post(self, url, params=None, json=None):
        if url == DingTalkOrgSyncAdapter.DINGTALK_DEPT_LIST_URL and json == {
            "dept_id": 42
        }:
            self.department_list_requests.append(42)
            return _FakeDingTalkResponse(
                {
                    "errcode": 0,
                    "result": [
                        {
                            "dept_id": 43,
                            "name": "平台组",
                            "parent_id": 999,
                        }
                    ],
                }
            )
        return await super().post(url, params=params, json=json)


class _FakeDingTalkDepartmentListFailureClient(_FakeDingTalkClient):
    async def post(self, url, params=None, json=None):
        if url == DingTalkOrgSyncAdapter.DINGTALK_DEPT_LIST_URL:
            self.department_list_requests.append(json["dept_id"])
            return _FakeDingTalkResponse(
                {"errcode": 50004, "errmsg": "请求的部门id不在授权范围内"}
            )
        return await super().post(url, params=params, json=json)


class _FakeDingTalkDepartmentDetailFailureClient(_FakeDingTalkClient):
    async def post(self, url, params=None, json=None):
        if url == DingTalkOrgSyncAdapter.DINGTALK_DEPT_GET_URL:
            return _FakeDingTalkResponse({"errcode": 0, "result": {}})
        return await super().post(url, params=params, json=json)


class _FakeFeishuDepartmentFailureClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, headers=None):
        return _FakeDingTalkResponse({"code": 999, "msg": "directory unavailable"})


def _disable_token_fetch(adapter):
    async def fake_get_access_token():
        return "access-token"

    adapter.get_access_token = fake_get_access_token


def test_limited_scope_normalizes_only_the_authorized_root(monkeypatch):
    fake_client = _FakeDingTalkMissingInternalParentClient()
    monkeypatch.setattr(
        "app.services.org_sync_adapter.httpx.AsyncClient",
        lambda *args, **kwargs: fake_client,
    )
    adapter = DingTalkOrgSyncAdapter(
        config={"app_key": "app-key", "app_secret": "app-secret"}
    )

    _disable_token_fetch(adapter)

    departments = asyncio.run(adapter.fetch_departments())

    assert departments[0].parent_external_id is None
    assert departments[1].parent_external_id == "999"
    with pytest.raises(ScimSnapshotError, match="references missing Group 999"):
        build_vendor_scim_snapshot(departments, [])


def test_dingtalk_department_list_scope_error_aborts_snapshot(monkeypatch):
    fake_client = _FakeDingTalkDepartmentListFailureClient()
    monkeypatch.setattr(
        "app.services.org_sync_adapter.httpx.AsyncClient",
        lambda *args, **kwargs: fake_client,
    )
    adapter = DingTalkOrgSyncAdapter(
        config={"app_key": "app-key", "app_secret": "app-secret"}
    )
    _disable_token_fetch(adapter)

    with pytest.raises(RuntimeError, match="department list error"):
        asyncio.run(adapter.fetch_departments())


def test_dingtalk_empty_authorized_department_detail_aborts_snapshot(monkeypatch):
    fake_client = _FakeDingTalkDepartmentDetailFailureClient()
    monkeypatch.setattr(
        "app.services.org_sync_adapter.httpx.AsyncClient",
        lambda *args, **kwargs: fake_client,
    )
    adapter = DingTalkOrgSyncAdapter(
        config={"app_key": "app-key", "app_secret": "app-secret"}
    )
    _disable_token_fetch(adapter)

    with pytest.raises(RuntimeError, match="authorized department 42 detail"):
        asyncio.run(adapter.fetch_departments())


def test_feishu_department_api_error_aborts_recursive_snapshot(monkeypatch):
    monkeypatch.setattr(
        "app.services.org_sync_feishu.httpx.AsyncClient",
        lambda *args, **kwargs: _FakeFeishuDepartmentFailureClient(),
    )
    adapter = FeishuOrgSyncAdapter(
        config={"app_id": "app-id", "app_secret": "app-secret"}
    )
    _disable_token_fetch(adapter)

    with pytest.raises(RuntimeError, match="department list error.*directory unavailable"):
        asyncio.run(adapter.fetch_departments())
