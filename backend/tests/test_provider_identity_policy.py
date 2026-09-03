"""Observable coverage for provider-scoped ordered identity fields."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from fastapi import HTTPException

from app.api.enterprise_routes_identity import _ensure_provider_type_available
from app.database import async_session, engine
from app.models.identity import IdentityProvider
from app.models.tenant import Tenant
from app.models.user import Identity
from app.services.auth_provider import ExternalUserInfo
from app.services.canonical_user_resolver import canonical_user_resolver
from app.services.provider_identity_policy import (
    DEFAULT_IDENTITY_MATCH_ORDER,
    identity_match_order,
    with_identity_match_policy,
)
from app.services.registration_service import registration_service


@pytest.fixture(autouse=True)
async def _dispose_engine_between_cases():
    await engine.dispose()
    yield
    await engine.dispose()


def test_default_and_canonical_provider_match_orders():
    assert identity_match_order(None) == DEFAULT_IDENTITY_MATCH_ORDER
    assert identity_match_order({}) == DEFAULT_IDENTITY_MATCH_ORDER
    assert identity_match_order(
        {"identity_match_policy": {"ordered_fields": [" EMAIL "]}}
    ) == ("email",)

    config = with_identity_match_policy({}, [" EMAIL ", "Phone"])
    assert config["identity_match_policy"]["ordered_fields"] == ["email", "phone"]


@pytest.mark.parametrize(
    "ordered_fields",
    ([], [""], ["name"], ["phone", " PHONE "], [None], "phone"),
)
def test_provider_match_order_rejects_empty_duplicate_or_unsupported_fields(
    ordered_fields,
):
    with pytest.raises(ValueError):
        identity_match_order(
            {"identity_match_policy": {"ordered_fields": ordered_fields}}
        )


@pytest.mark.asyncio
async def test_email_only_resolver_does_not_match_or_enrich_phone():
    email = f"email-only-{uuid.uuid4().hex[:10]}@example.com"
    phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    async with async_session() as db:
        identity = Identity(email=email)
        db.add(identity)
        await db.commit()
        identity_id = identity.id

    async with async_session() as db:
        claims = await canonical_user_resolver.resolve_identity_claims(
            db,
            email=email.upper(),
            phone=phone,
            enrich=True,
            ordered_fields=["email"],
        )
        await db.commit()
        assert claims.identity.id == identity_id
        assert claims.matched_by == "email"
        assert claims.email == email
        assert claims.phone is None
        assert claims.identity.phone is None


@pytest.mark.asyncio
async def test_registration_uses_provider_single_field_subset_end_to_end():
    email = f"excluded-email-{uuid.uuid4().hex[:10]}@example.com"
    phone = f"8617{uuid.uuid4().int % 10**9:09d}"
    provider = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider_type="oauth2",
        config={"identity_match_policy": {"ordered_fields": ["phone"]}},
    )
    async with async_session() as db:
        email_identity = Identity(email=email)
        db.add(email_identity)
        await db.commit()
        email_identity_id = email_identity.id

    async with async_session() as db:
        resolved = await registration_service.find_or_create_identity(
            db,
            email=email,
            phone=phone,
            provider=provider,
            email_config=object(),
        )
        await db.commit()
        assert resolved.id != email_identity_id
        assert resolved.phone == phone
        assert resolved.email is None


@pytest.mark.asyncio
async def test_config_only_sso_callback_preserves_email_first_order():
    suffix = uuid.uuid4().hex[:10]
    domain = f"{suffix}.example.com"
    email = f"person@{domain}"
    phone = f"8616{uuid.uuid4().int % 10**9:09d}"
    async with async_session() as db:
        tenant = Tenant(
            name=f"Policy {suffix}",
            slug=f"policy-{suffix}",
            sso_domain=domain,
        )
        email_identity = Identity(email=email)
        phone_identity = Identity(phone=phone)
        db.add_all([tenant, email_identity, phone_identity])
        await db.commit()
        email_identity_id = email_identity.id

    auth_provider = SimpleNamespace(
        provider=None,
        config={"identity_match_policy": {"ordered_fields": ["email"]}},
        exchange_code_for_token=AsyncMock(return_value={"access_token": "opaque"}),
        get_user_info=AsyncMock(
            return_value=ExternalUserInfo(
                provider_type="oauth2",
                provider_user_id=f"subject-{suffix}",
                name="Policy User",
                email=email,
                mobile=phone,
                raw_data={"sub": f"subject-{suffix}"},
            )
        ),
    )
    async with async_session() as db:
        user, created, error = await registration_service.register_with_sso(
            db, "oauth2", "authorization-code", auth_provider
        )
        await db.commit()
        assert error is None
        assert created is True
        assert user.identity_id == email_identity_id


@pytest.mark.asyncio
async def test_tenant_can_configure_each_provider_type_only_once():
    async with async_session() as db:
        tenant = Tenant(name="Provider uniqueness", slug=f"provider-{uuid.uuid4().hex[:10]}")
        other_tenant = Tenant(name="Other tenant", slug=f"provider-{uuid.uuid4().hex[:10]}")
        db.add_all([tenant, other_tenant])
        await db.flush()
        db.add(IdentityProvider(
            tenant_id=tenant.id,
            provider_type="oauth2",
            name="Standard identity provider",
            config={},
        ))
        await db.commit()
        tenant_id = tenant.id
        other_tenant_id = other_tenant.id

    async with async_session() as db:
        with pytest.raises(HTTPException) as duplicate:
            await _ensure_provider_type_available(
                db,
                tenant_id=tenant_id,
                provider_type="oauth2",
            )
        assert duplicate.value.status_code == 409

    async with async_session() as db:
        await _ensure_provider_type_available(
            db,
            tenant_id=other_tenant_id,
            provider_type="oauth2",
        )
