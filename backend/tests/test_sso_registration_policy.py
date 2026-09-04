"""Trusted SSO JIT provisioning is independent of public self-registration."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.auth_provider import BaseAuthProvider, ExternalUserInfo


class _Provider(BaseAuthProvider):
    provider_type = "test"

    async def get_authorization_url(self, redirect_uri, state):
        return f"{redirect_uri}?state={state}"

    async def exchange_code_for_token(self, code, redirect_uri=None):
        return {"access_token": code}

    async def get_user_info(self, access_token):
        return ExternalUserInfo(provider_type="test", provider_user_id=access_token)


@pytest.mark.asyncio
async def test_sso_new_user_is_allowed_when_registration_is_disabled():
    tenant_id = uuid.uuid4()
    provider_model = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant_id, provider_type="test", config={}
    )
    auth = _Provider(provider=provider_model)
    auth._ensure_provider = AsyncMock(return_value=provider_model)
    created_user = SimpleNamespace(
        id=uuid.uuid4(), identity_id=uuid.uuid4(), identity=SimpleNamespace(is_active=True),
        tenant_id=tenant_id, is_active=True,
    )
    auth._find_or_create_enterprise_user = AsyncMock(
        return_value=(created_user, True)
    )
    db = AsyncMock()
    db.get.side_effect = [
        SimpleNamespace(is_active=True),
        SimpleNamespace(is_active=True),
    ]

    result = await auth.find_or_create_user(
        db,
        ExternalUserInfo(provider_type="test", provider_user_id="subject"),
        tenant_id=str(tenant_id),
    )

    assert result == (created_user, True)
    db.execute.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_sso_existing_user_remains_allowed_when_registration_is_disabled():
    tenant_id = uuid.uuid4()
    provider_model = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant_id, provider_type="test", config={}
    )
    auth = _Provider(provider=provider_model)
    auth._ensure_provider = AsyncMock(return_value=provider_model)
    existing_user = SimpleNamespace(
        id=uuid.uuid4(), identity_id=uuid.uuid4(), identity=SimpleNamespace(is_active=True),
        tenant_id=None, is_active=True,
    )
    auth._find_or_create_enterprise_user = AsyncMock(
        return_value=(existing_user, False)
    )
    db = AsyncMock()

    result = await auth.find_or_create_user(
        db,
        ExternalUserInfo(provider_type="test", provider_user_id="subject"),
        tenant_id=str(tenant_id),
    )

    assert result == (existing_user, False)
    db.execute.assert_not_awaited()
    db.rollback.assert_not_awaited()
