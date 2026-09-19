"""Observable authentication-state intersection tests."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials

from app.api.auth_login import login
from app.core.security import create_access_token, get_current_user, hash_password
from app.database import async_session, engine
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.schemas.schemas import UserLogin
from app.services.auth_provider import BaseAuthProvider, ExternalUserInfo
from app.services.authentication_principal import activate_authenticated_source
from app.services.directory_user_status import sync_tenant_user_statuses


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_principal(*, identity_active=True, user_active=True, tenant_active=True):
    async with async_session() as db:
        suffix = uuid.uuid4().hex[:10]
        tenant = Tenant(
            name=f"Auth State {suffix}",
            slug=f"auth-state-{suffix}",
            is_active=tenant_active,
        )
        identity = Identity(
            email=f"auth-state-{suffix}@example.com",
            username=f"auth-state-{suffix}",
            password_hash=hash_password("correct-password"),
            email_verified=True,
            is_active=identity_active,
        )
        db.add_all([tenant, identity])
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Authentication State",
            role="member",
            is_active=user_active,
        )
        db.add(user)
        await db.commit()
        return user.id, identity.email, tenant.id


@pytest.mark.parametrize("disabled_layer", ["identity", "tenant"])
async def test_password_login_rejects_each_inactive_principal_layer(disabled_layer):
    _user_id, email, tenant_id = await _seed_principal(
        identity_active=disabled_layer != "identity",
        user_active=disabled_layer != "user",
        tenant_active=disabled_layer != "tenant",
    )
    async with async_session() as db:
        with pytest.raises(HTTPException) as exc_info:
            await login(
                UserLogin(
                    login_identifier=email,
                    password="correct-password",
                    tenant_id=tenant_id,
                ),
                BackgroundTasks(),
                db,
            )
    assert exc_info.value.status_code == 403


async def test_valid_password_reactivates_selected_tenant_membership():
    user_id, email, tenant_id = await _seed_principal(user_active=False)
    async with async_session() as db:
        result = await login(
            UserLogin(
                login_identifier=email,
                password="correct-password",
                tenant_id=tenant_id,
            ),
            BackgroundTasks(),
            db,
            Response(),
        )
        await db.commit()
    assert result.user.id == user_id
    async with async_session() as db:
        user = await db.get(User, user_id)
        assert user is not None and user.is_active is True


async def test_valid_password_does_not_bypass_explicit_login_suspension():
    user_id, email, tenant_id = await _seed_principal(user_active=False)
    async with async_session() as db:
        user = await db.get(User, user_id)
        user.is_login_suspended = True
        await db.commit()
    async with async_session() as db:
        with pytest.raises(HTTPException) as exc_info:
            await login(
                UserLogin(
                    login_identifier=email,
                    password="correct-password",
                    tenant_id=tenant_id,
                ),
                BackgroundTasks(),
                db,
            )
    assert exc_info.value.status_code == 403


@pytest.mark.parametrize("last_writer", ["login", "directory"])
async def test_fresh_login_and_directory_sync_serialize_by_commit_order(last_writer):
    user_id, _email, tenant_id = await _seed_principal()
    async with async_session() as db:
        provider = IdentityProvider(
            provider_type="oauth2",
            name=f"Concurrency {uuid.uuid4().hex[:8]}",
            tenant_id=tenant_id,
            is_active=True,
            sso_login_enabled=True,
            config={},
        )
        db.add(provider)
        await db.flush()
        member = OrgMember(
            tenant_id=tenant_id,
            provider_id=provider.id,
            external_id=f"subject-{uuid.uuid4().hex}",
            name="Concurrent Login",
            user_id=user_id,
            status="active",
        )
        db.add(member)
        await db.commit()
        provider_id = provider.id
        member_id = member.id

    first = async_session()
    second = async_session()
    try:
        if last_writer == "login":
            member = await first.get(OrgMember, member_id)
            member.status = "deleted"
            await first.flush()
            await sync_tenant_user_statuses(
                first,
                tenant_id=tenant_id,
                changed_provider_id=provider_id,
            )

            async def login_after_directory():
                user = await second.get(User, user_id)
                provider = await second.get(IdentityProvider, provider_id)
                source = await second.get(OrgMember, member_id)
                await activate_authenticated_source(
                    second,
                    user=user,
                    provider=provider,
                    member=source,
                    method="oauth2",
                )
                await second.commit()

            pending = asyncio.create_task(login_after_directory())
            await asyncio.sleep(0.1)
            assert not pending.done()
            await first.commit()
            await asyncio.wait_for(pending, timeout=3)
        else:
            user = await first.get(User, user_id)
            provider = await first.get(IdentityProvider, provider_id)
            member = await first.get(OrgMember, member_id)
            await activate_authenticated_source(
                first,
                user=user,
                provider=provider,
                member=member,
                method="oauth2",
            )

            async def directory_after_login():
                source = await second.get(OrgMember, member_id)
                source.status = "deleted"
                await second.flush()
                await sync_tenant_user_statuses(
                    second,
                    tenant_id=tenant_id,
                    changed_provider_id=provider_id,
                )
                await second.commit()

            pending = asyncio.create_task(directory_after_login())
            await asyncio.sleep(0.1)
            assert not pending.done()
            await first.commit()
            await asyncio.wait_for(pending, timeout=3)
    finally:
        await first.close()
        await second.close()

    async with async_session() as db:
        user = await db.get(User, user_id)
        member = await db.get(OrgMember, member_id)
        expected_active = last_writer == "login"
        assert user is not None and user.is_active is expected_active
        assert member is not None
        assert (member.status == "active") is expected_active


@pytest.mark.parametrize("disabled_layer", ["identity", "user", "tenant"])
async def test_existing_token_rejects_each_inactive_principal_layer(disabled_layer):
    user_id, _email, _tenant_id = await _seed_principal(
        identity_active=disabled_layer != "identity",
        user_active=disabled_layer != "user",
        tenant_active=disabled_layer != "tenant",
    )
    token = create_access_token(str(user_id), "member")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    request = Request({"type": "http", "scheme": "http", "path": "/", "headers": []})
    async with async_session() as db:
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request, Response(), credentials, db)
    assert exc_info.value.status_code == 401


class _ExistingSSOProvider(BaseAuthProvider):
    provider_type = "test"

    def __init__(self, user):
        super().__init__(provider=SimpleNamespace(config={}), config={})
        self.user = user

    async def get_authorization_url(self, redirect_uri, state):
        return redirect_uri

    async def exchange_code_for_token(self, code, redirect_uri=None):
        return {"access_token": code}

    async def get_user_info(self, access_token):
        return ExternalUserInfo(provider_type="test", provider_user_id=access_token)

    async def _ensure_provider(self, db, tenant_id=None):
        return self.provider

    async def _find_or_create_enterprise_user(self, db, user_info, tenant_id):
        return self.user, False


@pytest.mark.parametrize("disabled_layer", ["identity", "user", "tenant"])
async def test_sso_rejects_each_inactive_principal_layer(disabled_layer):
    user_id, _email, tenant_id = await _seed_principal(
        identity_active=disabled_layer != "identity",
        user_active=disabled_layer != "user",
        tenant_active=disabled_layer != "tenant",
    )
    async with async_session() as db:
        user = await db.get(User, user_id)
        provider = _ExistingSSOProvider(user)
        with pytest.raises(HTTPException) as exc_info:
            await provider.find_or_create_user(
                db,
                ExternalUserInfo(provider_type="test", provider_user_id="subject"),
                tenant_id=str(tenant_id),
            )
    assert exc_info.value.status_code == 403
