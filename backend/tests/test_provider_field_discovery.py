"""OAuth/OIDC field discovery behavior without external network access."""

import json

import httpx
import pytest

from app.services import provider_field_discovery
from app.services.provider_field_discovery import (
    cache_oidc_field_samples,
    discover_oidc_field_samples,
)


@pytest.mark.asyncio
async def test_real_userinfo_sample_precedes_metadata_and_filters_credentials():
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/token":
            assert request.headers["authorization"].startswith("Basic ")
            assert request.content == b"grant_type=client_credentials"
            return httpx.Response(
                200,
                json={"access_token": "private-access-token", "token_type": "Bearer"},
            )
        if request.url.path == "/userinfo":
            assert request.headers["authorization"] == "Bearer private-access-token"
            return httpx.Response(
                200,
                json={
                    "userId": "employee-1",
                    "data": {
                        "userId": "nested-employee-1",
                        "email": "employee@example.com",
                        "access_token": "nested-token",
                    },
                    "roles": [{"id": "role-1"}, {"id": "role-2"}],
                    "client_secret": "nested-secret",
                    "authorization": "sensitive-header",
                },
            )
        raise AssertionError("OIDC metadata must not be requested after userinfo succeeds")

    result = await discover_oidc_field_samples(
        {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "token_url": "https://identity.example/token",
            "user_info_url": "https://identity.example/userinfo",
            "issuer": "https://identity.example",
        },
        transport=httpx.MockTransport(handler),
    )

    assert result == {
        "source": "userinfo",
        "paths": ["data.email", "data.userId", "roles.id", "userId"],
        "fields": [
            {"path": "data.email", "sample_value": "employee@example.com"},
            {"path": "data.userId", "sample_value": "nested-employee-1"},
            {"path": "roles.id", "sample_value": "role-1"},
            {"path": "userId", "sample_value": "employee-1"},
        ],
    }
    assert requests == [("POST", "/token"), ("GET", "/userinfo")]
    serialized = repr(result).lower()
    assert "private-access-token" not in serialized
    assert "nested-token" not in serialized
    assert "nested-secret" not in serialized
    assert "sensitive-header" not in serialized


@pytest.mark.asyncio
async def test_failed_client_credentials_requires_real_userinfo_sample():
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/token":
            return httpx.Response(401, json={"error": "invalid_client"})
        raise AssertionError(f"unexpected discovery request: {request.url}")

    result = await discover_oidc_field_samples(
        {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "token_url": "https://identity.example/token",
            "user_info_url": "https://identity.example/userinfo",
            "issuer": "https://identity.example",
        },
        transport=httpx.MockTransport(handler),
    )

    assert result == {
        "source": "authorization_required",
        "paths": [],
        "fields": [],
    }
    assert requests == [("POST", "/token")]


@pytest.mark.asyncio
async def test_recent_login_sample_preserves_real_wrapper_path(monkeypatch):
    stored: dict[str, str] = {}

    async def set_value(key: str, value: str, _ttl: int):
        stored[key] = value

    async def get_value(key: str):
        return stored.get(key)

    monkeypatch.setattr(provider_field_discovery, "set_cached_token", set_value)
    monkeypatch.setattr(provider_field_discovery, "get_cached_token", get_value)

    await cache_oidc_field_samples(
        "provider-1",
        {
            "data": {
                "userId": "employee-1",
                "profile": {"name": "Sample User"},
                "access_token": "never-cache-this",
            }
        },
    )
    result = await discover_oidc_field_samples(
        {},
        provider_id="provider-1",
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(
                AssertionError("cached samples must avoid provider requests")
            )
        ),
    )

    assert result["source"] == "recent_login"
    assert result["paths"] == ["data.profile.name", "data.userId"]
    assert result["fields"] == [
        {"path": "data.profile.name", "sample_value": "Sample User"},
        {"path": "data.userId", "sample_value": "employee-1"},
    ]
    assert "never-cache-this" not in json.dumps(result)
