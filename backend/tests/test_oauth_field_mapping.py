"""Observable OAuth userinfo mapping behavior for nested provider payloads."""

from types import SimpleNamespace

import httpx
import pytest

from app.services.auth_provider import OAuth2AuthProvider
from app.services import provider_field_discovery


@pytest.mark.asyncio
async def test_oauth_user_mapping_reads_complete_nested_response_paths(monkeypatch):
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer access-token"
        return httpx.Response(
            200,
            json={
                "data": {
                    "userId": "user-42",
                    "profile": {"displayName": "Mapped User"},
                    "workEmail": "mapped@example.com",
                    "mobile": "13800000000",
                    "portrait": {"original": "https://cdn.example/avatar.png"},
                }
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(
            transport=httpx.MockTransport(handler),
        ),
    )
    provider = OAuth2AuthProvider(config={
        "user_info_url": "https://identity.example/userinfo",
        "field_mapping": {
            "user_id": "data.userId",
            "name": "data.profile.displayName",
            "email": "data.workEmail",
            "mobile": "data.mobile",
            "avatar": "data.portrait.original",
        },
    })

    info = await provider.get_user_info("access-token")

    assert info.provider_user_id == "user-42"
    assert info.name == "Mapped User"
    assert info.email == "mapped@example.com"
    assert info.mobile == "13800000000"
    assert info.avatar_url == "https://cdn.example/avatar.png"


@pytest.mark.asyncio
async def test_successful_oauth_login_caches_unflattened_userinfo(monkeypatch):
    observed: dict[str, object] = {}
    real_client = httpx.AsyncClient

    async def cache_sample(provider_id: str, payload: object):
        observed.update(provider_id=provider_id, payload=payload)

    monkeypatch.setattr(
        provider_field_discovery,
        "cache_oidc_field_samples",
        cache_sample,
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"data": {"userId": "user-42", "userName": "Mapped User"}},
                )
            ),
        ),
    )
    provider = SimpleNamespace(
        id="provider-42",
        config={
            "user_info_url": "https://identity.example/userinfo",
            "field_mapping": {
                "user_id": "data.userId",
                "name": "data.userName",
            },
        },
    )

    await OAuth2AuthProvider(provider=provider).get_user_info("access-token")

    assert observed == {
        "provider_id": "provider-42",
        "payload": {"data": {"userId": "user-42", "userName": "Mapped User"}},
    }


def test_oauth_mapping_does_not_implicitly_add_data_prefix():
    provider = OAuth2AuthProvider(config={
        "field_mapping": {
            "user_id": "userId",
            "name": "userName",
        },
    })
    payload = {"data": {"userId": "user-42", "userName": "Mapped User"}}

    assert provider._get_field(payload, "user_id") == ""
    assert provider._get_field(payload, "name") == ""


def test_configured_oauth_path_is_authoritative_over_standard_fallback():
    provider = OAuth2AuthProvider(config={
        "field_mapping": {"name": "data.userName"},
    })
    payload = {
        "name": "Wrong standard fallback",
        "data": {},
    }

    assert provider._get_field(payload, "name") == ""
