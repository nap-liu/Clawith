"""Exact provider connection selection for multi-provider IM channels."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.channel_user_errors import ChannelUserResolutionError
from app.services.channel_user_service import ChannelUserService


@pytest.mark.asyncio
async def test_explicit_channel_provider_id_selects_exact_active_connection():
    tenant_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    provider = SimpleNamespace(
        id=provider_id,
        tenant_id=tenant_id,
        provider_type="dingtalk",
        is_active=True,
        config={"identity_match_policy": {"ordered_fields": ["email", "phone"]}},
    )
    db = AsyncMock()
    db.get.return_value = provider

    selected = await ChannelUserService()._ensure_provider(
        db,
        "dingtalk",
        tenant_id,
        provider_id=str(provider_id),
        installation_scope="robot:one",
    )

    assert selected is provider
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_channel_provider_cannot_cross_tenant():
    provider = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider_type="dingtalk",
        is_active=True,
        config={},
    )
    db = AsyncMock()
    db.get.return_value = provider

    with pytest.raises(ChannelUserResolutionError, match="agent tenant"):
        await ChannelUserService()._ensure_provider(
            db,
            "dingtalk",
            uuid.uuid4(),
            provider_id=provider.id,
        )
