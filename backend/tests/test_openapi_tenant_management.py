"""Tenant-scoped management and ordinary delegated login for existing users."""
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.database import async_session
from app.main import app
from app.models.openapi_application import OpenAPIApplication
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.mark.asyncio
async def test_company_management_is_scoped_and_supports_platform_admin_login():
    async with async_session() as db:
        tenants = [Tenant(name=f"Company {n}", slug=f"openapi-admin-{uuid.uuid4().hex}") for n in range(2)]
        db.add_all(tenants)
        await db.flush()
        identities = [Identity(phone=f"156{uuid.uuid4().int % 100000000:08}", is_active=True,
                               is_platform_admin=n == 4) for n in range(5)]
        db.add_all(identities)
        await db.flush()
        users = [User(tenant_id=tenants[1 if n == 1 else 0].id, identity_id=identity.id,
                      role=["org_admin", "org_admin", "member", "platform_admin", "member"][n],
                      display_name=f"Integration administrator {n}", is_active=True)
                 for n, identity in enumerate(identities)]
        db.add_all(users)
        await db.commit()
    headers = [{"Authorization": f"Bearer {create_access_token(str(user.id), user.role)}"} for user in users]
    path = "/api/enterprise/openapi/applications"
    business = "/api/openapi/v1"
    config = {"name": "Company integration", "trust_user_identity": True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        applications = []
        for index in (0, 1):
            response = await client.post(path, headers=headers[index], json=config)
            assert response.status_code == 201
            assert response.json()["tenant_id"] == str(tenants[index].id)
            applications.append(response.json())
        own, other = applications
        own_path, other_path = f"{path}/{own['id']}", f"{path}/{other['id']}"
        for index in (0, 3, 4):
            listing = await client.get(path, headers=headers[index])
            assert listing.status_code == 200
            assert [row["id"] for row in listing.json()] == [own["id"]]
            assert own["client_secret"] not in listing.text
            assert (await client.get(path, params={"tenant_id": str(tenants[1].id)}, headers=headers[index])).status_code == 403
            assert (await client.post(path, headers=headers[index], json={**config, "tenant_id": str(tenants[1].id)})).status_code == 422
            for method, suffix in [("PUT", ""), ("POST", "/rotate-secret"), ("POST", "/revoke"), ("GET", "/audit")]:
                response = await client.request(method, other_path + suffix, headers=headers[index],
                                                **({"json": config} if method == "PUT" else {}))
                assert response.status_code == 404
        for method, target in [("GET", path), ("POST", path), ("PUT", own_path),
                               ("POST", own_path + "/rotate-secret"), ("POST", own_path + "/revoke"),
                               ("GET", own_path + "/audit")]:
            response = await client.request(method, target, headers=headers[2],
                                            **({"json": config} if method in {"POST", "PUT"} else {}))
            assert response.status_code == 403
        # The previous platform-wide management surface must no longer exist.
        assert (await client.get("/api/admin/openapi/applications", headers=headers[3])).status_code == 404
        async with async_session() as db:
            untouched = await db.get(OpenAPIApplication, uuid.UUID(other["id"]))
            assert untouched.generation == 1 and untouched.enabled and untouched.revoked_at is None
            assert len((await db.scalars(select(OpenAPIApplication).where(
                OpenAPIApplication.tenant_id.in_([tenant.id for tenant in tenants]),
            ))).all()) == 2

        updated = await client.put(own_path, headers=headers[0], json={**config, "name": "Updated integration"})
        assert updated.status_code == 200 and updated.json()["name"] == "Updated integration"
        response = await client.post(business + "/auth/token", auth=httpx.BasicAuth(own["client_id"], own["client_secret"]),
                                     data={"grant_type": "client_credentials"})
        assert response.status_code == 200
        bearer = {"Authorization": "Bearer " + response.json()["access_token"]}
        for index in (3, 4):
            response = await client.post(business + "/auth/links", headers=bearer, json={
                "user": {"subject": str(users[index].id), "phone": identities[index].phone, "asserted_at": int(time.time())},
                "redirect_uri": "/explore",
            })
            assert response.status_code == 200
            code = parse_qs(urlsplit(response.json()["login_url"]).query)["code"][0]
            exchanged = await client.post(business + "/auth/link-exchange", json={"code": code})
            assert exchanged.status_code == 200
            assert exchanged.json()["user"]["id"] == str(users[index].id)
            me = await client.get("/api/auth/me", headers={
                "Authorization": "Bearer " + exchanged.json()["access_token"],
            })
            assert me.status_code == 200 and me.json()["id"] == str(users[index].id)
        other_token = await client.post(business + "/auth/token",
            auth=httpx.BasicAuth(other["client_id"], other["client_secret"]),
            data={"grant_type": "client_credentials"})
        assert other_token.status_code == 200
        foreign_login = await client.post(business + "/auth/links", headers={
            "Authorization": "Bearer " + other_token.json()["access_token"],
        }, json={"user": {"subject": str(users[3].id), "phone": identities[3].phone,
                          "asserted_at": int(time.time())}, "redirect_uri": "/explore"})
        assert foreign_login.status_code == 403
        response = await client.post(business + "/auth/links", headers=bearer, json={
            "user": {"subject": str(users[2].id), "phone": identities[2].phone, "asserted_at": int(time.time())},
            "redirect_uri": "/explore",
        })
        assert response.status_code == 200
        code = parse_qs(urlsplit(response.json()["login_url"]).query)["code"][0]
        async with async_session() as db:
            identity = await db.get(Identity, identities[2].id)
            identity.is_platform_admin = True
            await db.commit()
        exchanged = await client.post(business + "/auth/link-exchange", json={"code": code})
        assert exchanged.status_code == 200
        assert exchanged.json()["user"]["id"] == str(users[2].id)
        rotated = await client.post(own_path + "/rotate-secret", headers=headers[0])
        assert rotated.status_code == 200 and rotated.json()["client_secret"] != own["client_secret"]
        assert (await client.get(business + "/capabilities", headers=bearer)).status_code == 401
        assert (await client.post(own_path + "/revoke", headers=headers[0])).status_code == 200
        assert (await client.put(own_path, headers=headers[0], json=config)).status_code == 409
        audit = await client.get(own_path + "/audit", headers=headers[0])
        assert audit.status_code == 200
        assert {"openapi.application.create", "openapi.application.update", "openapi.application.rotate", "openapi.application.revoke"} <= {
            row["action"] for row in audit.json()
        }
        assert all(row["details"]["application_id"] == own["id"] for row in audit.json())
