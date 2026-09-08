"""Focused API/database risks for OAuth clients and temporary login links."""
import asyncio
import json
import time
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.main import app
from app.core.security import create_access_token, decode_access_token
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.models.openapi_application import OpenAPICredential
from app.services.openapi_applications import digest, now
from app.services.canonical_user_resolver import CanonicalUserResolver


@pytest.mark.asyncio
async def test_oauth_and_generic_login_contract():
    async with async_session() as db:
        tenant = Tenant(name="OAuth contract", slug=f"oauth-{uuid.uuid4().hex}")
        other = Tenant(name="Other organization", slug=f"other-{uuid.uuid4().hex}")
        db.add_all([tenant, other])
        await db.flush()
        identities = [Identity(phone=f"155{uuid.uuid4().int % 100000000:08}", is_active=True) for _ in range(3)]
        db.add_all(identities)
        await db.flush()
        users = [User(tenant_id=tenant.id if index < 2 else other.id, identity_id=identity.id,
                      display_name=f"OAuth user {index}", role="platform_admin" if index == 0 else "member", is_active=True)
                 for index, identity in enumerate(identities)]
        db.add_all(users)
        await db.flush()
        agents = [Agent(tenant_id=user.tenant_id, creator_id=user.id, name=f"OAuth employee {index}", access_mode="private")
                  for index, user in enumerate(users)]
        db.add_all(agents)
        await db.commit()
    headers = {"Authorization": f"Bearer {create_access_token(str(users[0].id), users[0].role)}"}
    base = "/api/openapi/v1"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/enterprise/openapi/applications", headers=headers, json={
            "name": "OAuth contract client", "trust_user_identity": True,
            "scopes": ["employees:read", "auth:login"], "embed_origins": ["http://localhost:64513"],
        })
        assert created.status_code == 201
        application = created.json()
        app_id, secret = application["client_id"], application["client_secret"]
        basic = httpx.BasicAuth(app_id, secret)
        listing = await client.get("/api/enterprise/openapi/applications", headers=headers)
        assert secret not in listing.text and "secret_hash" not in listing.text
        response = await client.post(base + "/auth/token", json={"grant_type": "client_credentials"}, auth=basic)
        assert response.status_code == 400 and response.json()["error"] == "invalid_request"
        response = await client.post(base + "/auth/token", data={"grant_type": "password"}, auth=basic)
        assert response.json()["error"] == "unsupported_grant_type"
        response = await client.post(base + "/auth/token", data={"grant_type": "client_credentials"})
        assert response.status_code == 401 and response.headers["www-authenticate"].startswith("Basic ")
        response = await client.post(base + "/auth/token", data={"grant_type": "client_credentials", "scope": "admin:all"}, auth=basic)
        assert response.status_code == 400 and response.json()["error"] == "invalid_scope"
        response = await client.post(base + "/auth/token", data={"grant_type": "client_credentials", "scope": "employees:read\tauth:login"}, auth=basic)
        assert response.status_code == 400 and response.json()["error"] == "invalid_scope"
        response = await client.post(base + "/auth/token", data={"grant_type": "client_credentials", "extension": "ignored"}, auth=basic)
        assert response.status_code == 200

        async def oauth(scope=None):
            data = {"grant_type": "client_credentials"}
            if scope is not None:
                data["scope"] = scope
            response = await client.post(base + "/auth/token", data=data, auth=basic)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store" and response.headers["pragma"] == "no-cache"
            return response.json()["access_token"]

        token = await oauth()
        bearer = {"Authorization": f"Bearer {token}"}
        claim = {"subject": str(users[1].id), "phone": "+86" + identities[1].phone, "asserted_at": int(time.time())}
        response = await client.post(base + "/digital-employees/search", headers=bearer, json={"user": claim})
        assert response.status_code == 200
        assert {item["id"] for item in response.json()["items"]} == {str(agents[1].id)}
        response = await client.post(base + "/digital-employees/search", headers=bearer,
                                     json={"user": {**claim, "phone": "0086" + identities[1].phone}})
        assert response.status_code == 200
        response = await client.post(base + f"/digital-employees/{agents[2].id}/access", headers=bearer, json={"user": claim})
        assert response.status_code in {403, 404}
        response = await client.post(base + "/digital-employees/search", json={"user": claim})
        assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
        response = await client.post(base + "/digital-employees/search", headers=bearer,
                                     json={"user": {**claim, "phone": identities[0].phone}})
        assert response.status_code == 403
        response = await client.post(base + "/digital-employees/search", headers=bearer,
                                     json={"user": {**claim, "asserted_at": int(time.time()) - 90}})
        assert response.status_code == 400
        limited = await oauth("employees:read")
        response = await client.post(base + "/auth/links", headers={"Authorization": f"Bearer {limited}"},
                                     json={"user": claim, "redirect_uri": "/"})
        assert response.status_code == 403 and response.json()["error"] == "insufficient_scope"

        async def link(target="/", extra=None):
            return await client.post(base + "/auth/links", headers=bearer,
                                     json={"user": claim, "redirect_uri": target, **(extra or {})})

        for target in ["//outside.example/", "/%2foutside.example", "/%5coutside.example", "javascript:alert(1)", "https://outside.example/"]:
            assert (await link(target)).status_code == 400
        assert (await link("/", {"embed_origin": "https://outside.example"})).status_code == 403
        issued = await link(f"/h5/agents/{agents[1].id}/chat")
        assert issued.status_code == 200
        code = parse_qs(urlsplit(issued.json()["login_url"]).query)["code"][0]
        assert identities[1].phone not in code and secret not in code
        with pytest.raises(HTTPException) as invalid_code:
            decode_access_token(code)
        assert invalid_code.value.status_code == 401
        response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {code}"})
        assert response.status_code == 401
        response = await client.post(base + "/auth/link-exchange", json={"code": code[:-2] + "xx"})
        assert response.status_code == 401
        exchanges = await asyncio.gather(*[client.post(base + "/auth/link-exchange", json={"code": code}) for _ in range(2)])
        assert sorted(response.status_code for response in exchanges) == [200, 401]
        logged_in = next(response for response in exchanges if response.status_code == 200)
        assert logged_in.json()["user"]["id"] == str(users[1].id)
        assert "access_token=" in logged_in.headers["set-cookie"]
        response = await client.get(f"/api/agents/{agents[1].id}", headers={"Authorization": f"Bearer {logged_in.json()['access_token']}"})
        assert response.status_code == 200
        # Signing arbitrary relative destinations is page-independent.
        assert (await link("/settings?tab=profile")).status_code == 200

        second = await client.post("/api/enterprise/openapi/applications", headers=headers, json={
            "name": "Independent OAuth client",
        })
        other_basic = httpx.BasicAuth(second.json()["client_id"], second.json()["client_secret"])
        assert (await client.post(base + "/auth/revoke", auth=other_basic, data={"token": token})).status_code == 200
        assert (await client.get(base + "/capabilities", headers=bearer)).status_code == 200
        expired = await link()
        expired_code = parse_qs(urlsplit(expired.json()["login_url"]).query)["code"][0]
        async with async_session() as db:
            row = await db.get(OpenAPICredential, digest(expired_code))
            row.expires_at = now() - timedelta(seconds=1)
            await db.commit()
        assert (await client.post(base + "/auth/link-exchange", json={"code": expired_code})).status_code == 401
        async with async_session() as db:
            member = await db.get(User, users[1].id)
            member.is_active = False
            await db.commit()
        assert (await link()).status_code == 403
        async with async_session() as db:
            member = await db.get(User, users[1].id)
            member.is_active = True
            duplicate = Identity(phone="+86" + identities[1].phone, is_active=True)
            foreign = Identity(phone="+442079460123", is_active=True)
            db.add_all([duplicate, foreign])
            await db.commit()
            resolved = await CanonicalUserResolver().resolve_identity_claims(
                db, phone="+44 20 7946 0123", email=None, enrich=False, ordered_fields=["phone"], phone_equivalence=True)
            assert resolved.identity.id == foreign.id
        assert (await link()).status_code == 409
        async with async_session() as db:
            await db.delete(await db.get(Identity, duplicate.id))
            await db.delete(await db.get(Identity, foreign.id))
            await db.commit()

        await client.post(base + "/auth/revoke", auth=basic, data={"token": token})
        response = await client.get(base + "/capabilities", headers=bearer)
        assert response.status_code == 401
        assert (await client.post(base + "/auth/revoke", auth=basic, data={"token": "unknown"})).status_code == 200
        assert (await client.post(base + "/auth/revoke", auth=basic, data={"token": token})).status_code == 200
        token = await oauth()
        bearer = {"Authorization": f"Bearer {token}"}
        issued = await link()
        code = parse_qs(urlsplit(issued.json()["login_url"]).query)["code"][0]
        rotated = await client.post(f"/api/enterprise/openapi/applications/{app_id}/rotate-secret", headers=headers)
        assert rotated.status_code == 200
        assert (await client.get(base + "/capabilities", headers=bearer)).status_code == 401
        assert (await client.post(base + "/auth/link-exchange", json={"code": code})).status_code == 401
        assert (await client.post(base + "/auth/token", auth=basic, data={"grant_type": "client_credentials"})).status_code == 401
        async with async_session() as db:
            audits = (await db.execute(select(AuditLog).where(AuditLog.details["application_id"].as_string() == app_id))).scalars().all()
            assert len(audits) >= 10
            raw = json.dumps([row.details for row in audits])
            assert secret not in raw and code not in raw and identities[1].phone not in raw
