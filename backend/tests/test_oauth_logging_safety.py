"""OAuth provider logs must never contain credentials or identity values."""

from __future__ import annotations

import pytest
from loguru import logger

from app.services.auth_provider import OAuth2AuthProvider


pytestmark = pytest.mark.asyncio


class _Response:
    def json(self):
        return {
            "sub": "private-subject",
            "email": "private@example.test",
            "phone_number": "+8613800000000",
            "name": "Private Person",
        }


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, *_args, **_kwargs):
        return _Response()


async def test_oauth_logs_exclude_userinfo_and_token_response_values(monkeypatch):
    monkeypatch.setattr(
        "app.services.auth_provider_oauth.httpx.AsyncClient",
        lambda: _Client(),
    )
    provider = OAuth2AuthProvider(
        config={"user_info_url": "https://provider.invalid/userinfo"}
    )
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)))
    try:
        user_info = await provider.get_user_info("private-access-token")
        fallback = await provider.get_user_info_from_token_data(
            {
                "access_token": "private-access-token",
                "refresh_token": "private-refresh-token",
                "userInfo": {
                    "sub": "fallback-private-subject",
                    "email": "fallback-private@example.test",
                    "nested": {"id_token": "private-nested-id-token"},
                },
            }
        )
    finally:
        logger.remove(sink_id)

    assert user_info.email == "private@example.test"
    assert fallback.email == "fallback-private@example.test"
    assert fallback.raw_data["userInfo"]["email"] == "fallback-private@example.test"
    assert "access_token" not in fallback.raw_data
    assert "refresh_token" not in fallback.raw_data
    assert "id_token" not in fallback.raw_data["userInfo"]["nested"]
    rendered = "".join(messages)
    for sensitive_value in (
        "private-access-token",
        "private-refresh-token",
        "private-nested-id-token",
        "private-subject",
        "private@example.test",
        "+8613800000000",
        "Private Person",
        "fallback-private-subject",
        "fallback-private@example.test",
    ):
        assert sensitive_value not in rendered
