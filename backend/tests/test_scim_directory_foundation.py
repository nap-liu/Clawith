"""Observable contracts for the provider-neutral SCIM directory foundation."""

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.services.directory_sync_policy import next_sync_time, validate_sync_policy
from app.services.org_sync_adapter import get_org_sync_adapter
from app.services.provider_field_discovery import discover_oidc_claim_paths
from app.services.scim_client import ScimClient
from app.services.scim_directory import ScimDirectorySnapshot, ScimSnapshotError
from app.services.scim_org_sync_adapter import ScimOrgSyncAdapter
from app.schemas.oauth2 import OAuth2Config

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
ENTERPRISE_USER_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"


def _user(**values):
    return {"schemas": [USER_SCHEMA], **values}


def _group(**values):
    return {"schemas": [GROUP_SCHEMA], **values}


def test_fixed_schedule_units_and_calendar_month_are_stable():
    start = datetime(2025, 1, 31, 8, 30, tzinfo=timezone.utc)
    assert next_sync_time(start, 2, "hour") == datetime(2025, 1, 31, 10, 30, tzinfo=timezone.utc)
    assert next_sync_time(start, 1, "month") == datetime(2025, 2, 28, 8, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="hour, day, week, or month"):
        validate_sync_policy(True, 1, "minute")


@pytest.mark.asyncio
async def test_native_scim_provider_uses_scim_adapter_without_extra_capability_flag():
    provider = SimpleNamespace(provider_type="scim", tenant_id="tenant", config={})

    adapter = await get_org_sync_adapter(None, "scim", provider=provider)

    assert isinstance(adapter, ScimOrgSyncAdapter)


def test_scim_snapshot_preserves_multi_group_nested_and_ungrouped_users():
    snapshot = ScimDirectorySnapshot.from_resources(
        [
            _user(id="u-1", userName="one", active=True),
            _user(id="u-2", userName="two", active=False),
        ],
        [
            _group(**{
                "id": "parent-a",
                "displayName": "Parent A",
                "members": [
                    {"value": "child", "type": "Group"},
                    {"value": "u-1", "type": "User"},
                ],
            }),
            _group(**{
                "id": "parent-b",
                "displayName": "Parent B",
                "members": [{"value": "child", "type": "Group"}],
            }),
            _group(**{
                "id": "child",
                "displayName": "Child",
                "members": [{"value": "u-1", "type": "User"}],
            }),
        ],
    )

    assert snapshot.group_edges() == (("parent-a", "child"), ("parent-b", "child"))
    assert snapshot.user_groups() == {
        "u-1": ("child", "parent-a"),
        "u-2": (),
    }
    assert [group.id for group in snapshot.topological_groups()] == [
        "parent-a",
        "parent-b",
        "child",
    ]


def test_scim_user_preserves_standard_title_and_enterprise_organization_fields():
    snapshot = ScimDirectorySnapshot.from_resources(
        [
            {
                "schemas": [USER_SCHEMA, ENTERPRISE_USER_SCHEMA],
                "id": "u-standard",
                "userName": "standard",
                "displayName": "Standard User",
                "photos": [{"value": "https://cdn.example/avatar.png", "primary": True}],
                "title": "Frontend Engineer",
                ENTERPRISE_USER_SCHEMA: {
                    "employeeNumber": "E-100",
                    "organization": "Yeyecha",
                    "division": "Technology",
                    "department": "Frontend",
                },
            }
        ],
        [],
    )

    user = snapshot.users[0]
    assert user.title == "Frontend Engineer"
    assert user.photos == ("https://cdn.example/avatar.png",)
    assert user.employee_number == "E-100"
    assert user.organizational_path == "Yeyecha/Technology/Frontend"


def test_scim_user_applies_configured_platform_field_paths():
    snapshot = ScimDirectorySnapshot.from_resources(
        [
            _user(
                id="u-custom",
                userName="custom",
                active=True,
                profile={
                    "fullName": "Mapped User",
                    "workEmail": "mapped@example.com",
                    "workPhone": "13800000000",
                    "photoUrl": "https://cdn.example/mapped.png",
                    "jobTitle": "Mapped Title",
                    "org": "Mapped Organization",
                },
            )
        ],
        [],
        user_field_mapping={
            "name": "/profile/fullName",
            "email": "/profile/workEmail",
            "mobile": "/profile/workPhone",
            "avatar": "/profile/photoUrl",
            "title": "/profile/jobTitle",
            "organization": "/profile/org",
        },
    )

    user = snapshot.users[0]
    assert user.display_name == "Mapped User"
    assert user.emails == ("mapped@example.com",)
    assert user.phone_numbers == ("13800000000",)
    assert user.photos == ("https://cdn.example/mapped.png",)
    assert user.title == "Mapped Title"
    assert user.organizational_path == "Mapped Organization"


def test_oauth_config_preserves_scim_directory_field_mapping():
    config = OAuth2Config(
        app_id="client",
        authorize_url="https://example.com/oauth2/authorize",
        directory={"field_mapping": {"title": "/profile/jobTitle"}},
    )

    assert config.model_dump()["directory"] == {
        "field_mapping": {"title": "/profile/jobTitle"}
    }


def test_scim_mapping_rejects_unknown_targets_and_non_pointer_paths():
    with pytest.raises(ScimSnapshotError, match="unsupported fields"):
        ScimDirectorySnapshot.from_resources(
            [_user(id="u-1")],
            [],
            user_field_mapping={"password": "/password"},
        )
    with pytest.raises(ScimSnapshotError, match="JSON Pointers"):
        ScimDirectorySnapshot.from_resources(
            [_user(id="u-1")],
            [],
            user_field_mapping={"title": "profile.jobTitle"},
        )


def test_scim_snapshot_rejects_dangling_refs_and_group_cycles():
    with pytest.raises(ScimSnapshotError, match="missing User"):
        ScimDirectorySnapshot.from_resources(
            [],
            [_group(id="g", members=[{"value": "missing", "type": "User"}])],
        )

    with pytest.raises(ScimSnapshotError, match="cycle"):
        ScimDirectorySnapshot.from_resources(
            [],
            [
                _group(id="a", members=[{"value": "b", "type": "Group"}]),
                _group(id="b", members=[{"value": "a", "type": "Group"}]),
            ],
        )


@pytest.mark.asyncio
async def test_scim_client_uses_basic_auth_and_complete_pagination():
    seen_authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("authorization", ""))
        path = request.url.path
        if path.endswith("ServiceProviderConfig"):
            return httpx.Response(200, json={"patch": {"supported": True}})
        if path.endswith("ResourceTypes"):
            return httpx.Response(200, json={"Resources": [{"name": "User"}, {"name": "Group"}]})
        if path.endswith("Schemas"):
            return httpx.Response(
                200,
                json={
                    "Resources": [
                        {
                            "id": USER_SCHEMA,
                            "attributes": [
                                {"name": "userName"},
                                {"name": "title"},
                            ],
                        },
                        {"id": GROUP_SCHEMA},
                        {
                            "id": ENTERPRISE_USER_SCHEMA,
                            "attributes": [{"name": "department"}],
                        },
                    ]
                },
            )
        if path.endswith("Users"):
            start = int(request.url.params["startIndex"])
            resources = (
                [_user(id="u1", userName="one")]
                if start == 1
                else [_user(id="u2", userName="two")]
            )
            return httpx.Response(200, json={"totalResults": 2, "Resources": resources})
        if path.endswith("Groups"):
            return httpx.Response(
                200,
                json={
                    "totalResults": 1,
                    "Resources": [
                        _group(id="g1", members=[{"value": "u1", "type": "User"}])
                    ],
                },
            )
        return httpx.Response(404)

    client = ScimClient(
        base_url="https://directory.example/scim/v2",
        client_id="client",
        client_secret="secret",
        page_size=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        fields = await client.discover_user_fields()
        snapshot = await client.fetch_snapshot()
    finally:
        await client.close()

    assert [user.id for user in snapshot.users] == ["u1", "u2"]
    assert [group.id for group in snapshot.groups] == ["g1"]
    fields_by_path = {field["path"]: field["sample_value"] for field in fields}
    assert "/title" in fields_by_path
    assert fields_by_path["/userName"] == "one"
    assert f"/{ENTERPRISE_USER_SCHEMA}/department" in fields_by_path
    assert seen_authorization and all(value.startswith("Basic ") for value in seen_authorization)


@pytest.mark.asyncio
async def test_scim_field_discovery_targets_an_exact_account():
    observed_filters: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("Schemas"):
            return httpx.Response(200, json={
                "Resources": [{
                    "id": USER_SCHEMA,
                    "attributes": [{"name": "userName"}, {"name": "emails"}],
                }],
            })
        if request.url.path.endswith("Users"):
            observed_filters.append(request.url.params.get("filter", ""))
            return httpx.Response(200, json={
                "totalResults": 1,
                "Resources": [_user(
                    id="u-target",
                    userName="target",
                    emails=[{"value": "target@example.com"}],
                )],
            })
        return httpx.Response(404)

    client = ScimClient(
        base_url="https://directory.example/scim/v2",
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        fields = await client.discover_user_fields("target@example.com")
    finally:
        await client.close()

    assert observed_filters == ['emails.value eq "target@example.com"']
    samples = {field["path"]: field["sample_value"] for field in fields}
    assert samples["/userName"] == "target"
    assert "target@example.com" in samples["/emails"]


@pytest.mark.asyncio
async def test_scim_field_discovery_scans_until_exact_match_without_filters():
    unfiltered_starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("Schemas"):
            return httpx.Response(200, json={
                "Resources": [{
                    "id": USER_SCHEMA,
                    "attributes": [{"name": "userName"}],
                }],
            })
        if request.url.path.endswith("Users") and "filter" in request.url.params:
            return httpx.Response(400, json={"detail": "filter unsupported"})
        if request.url.path.endswith("Users"):
            start = int(request.url.params["startIndex"])
            unfiltered_starts.append(start)
            resources = (
                [_user(id="u-other", userName="other")]
                if start == 1
                else [_user(id="u-target", userName="target")]
            )
            return httpx.Response(200, json={
                "totalResults": 2,
                "Resources": resources,
            })
        return httpx.Response(404)

    client = ScimClient(
        base_url="https://directory.example/scim/v2",
        client_id="client",
        client_secret="secret",
        page_size=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        fields = await client.discover_user_fields("target")
    finally:
        await client.close()

    assert unfiltered_starts == [1, 2]
    samples = {field["path"]: field["sample_value"] for field in fields}
    assert samples["/userName"] == "target"


@pytest.mark.asyncio
async def test_oidc_field_discovery_does_not_offer_unobserved_metadata_claims():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"metadata-only discovery is not a real sample: {request.url}")

    paths = await discover_oidc_claim_paths(
        {"authorize_url": "https://login.example/user-center/oauth2/authorize"},
        transport=httpx.MockTransport(handler),
    )

    assert paths == ()


def test_scim_snapshot_rejects_string_boolean_and_invalid_multivalue():
    with pytest.raises(ScimSnapshotError, match="not a boolean"):
        ScimDirectorySnapshot.from_resources(
            [_user(id="u1", userName="one", active="false")], []
        )
    with pytest.raises(ScimSnapshotError, match="multi-valued"):
        ScimDirectorySnapshot.from_resources(
            [_user(id="u1", userName="one", emails="not-an-array")], []
        )


@pytest.mark.asyncio
async def test_scim_client_retries_bounded_throttling_response(monkeypatch):
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.services.scim_client.asyncio.sleep", no_sleep)
    client = ScimClient(
        base_url="https://directory.example/scim/v2",
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client._get("/ServiceProviderConfig") == {"ok": True}
    finally:
        await client.close()
    assert attempts == 2
