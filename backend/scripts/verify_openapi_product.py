"""Isolated local product walkthrough. Run only in the Docker validation stack."""
import asyncio
import json
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

import app.main  # noqa: F401 -- register the platform model graph
from app.core.security import create_access_token
from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import Identity, User


async def main():
    database = get_settings().DATABASE_URL.rsplit("/", 1)[-1]
    if not (database.startswith("test_") or database.endswith("_test")):
        raise RuntimeError("This walkthrough requires an isolated test database")
    async with async_session() as db:
        tenant = Tenant(name="OpenAPI validation", slug=f"openapi-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.flush()
        identity = Identity(phone=f"1555{str(int(time.time()))[-7:]}", is_active=True, is_platform_admin=True)
        db.add(identity)
        await db.flush()
        user = User(tenant_id=tenant.id, identity_id=identity.id, display_name="OpenAPI administrator", role="platform_admin", is_active=True)
        db.add(user)
        await db.flush()
        employee = Agent(tenant_id=tenant.id, creator_id=user.id, name="OpenAPI employee", access_mode="company", role_description="Integration validation")
        db.add(employee)
        await db.commit()
        fixture = {"tenant_id": str(tenant.id), "user_id": str(user.id), "phone": identity.phone,
                   "employee_id": str(employee.id), "admin_token": create_access_token(str(user.id), user.role)}
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=30) as client:
        response = await client.post("/api/admin/openapi/applications", headers={"Authorization": f"Bearer {fixture['admin_token']}"}, json={
            "name": "OpenAPI validation client", "tenant_id": fixture["tenant_id"], "trust_user_identity": True,
            "scopes": ["employees:read", "auth:login"], "embed_origins": ["http://localhost:64513"],
            "redirect_origins": ["http://localhost:64514"],
        })
        assert response.status_code == 201, (response.status_code, response.text)
        created = response.json()
        fixture.update(client_id=created["client_id"], client_secret=created["client_secret"])
        response = await client.post("/api/openapi/v1/auth/token", auth=httpx.BasicAuth(fixture["client_id"], fixture["client_secret"]),
                                     data={"grant_type": "client_credentials"})
        assert response.status_code == 200, (response.status_code, response.text)
        fixture["system_token"] = response.json()["access_token"]
        headers = {"Authorization": f"Bearer {fixture['system_token']}"}
        user = {"subject": fixture["user_id"], "phone": fixture["phone"], "asserted_at": int(time.time())}
        response = await client.post("/api/openapi/v1/digital-employees/search", headers=headers, json={"user": user})
        assert response.status_code == 200, (response.status_code, response.text)
        employee = next(item for item in response.json()["items"] if item["id"] == fixture["employee_id"])
        response = await client.post("/api/openapi/v1/auth/links", headers=headers, json={"user": user, "redirect_uri": employee["access_url"], "embed_origin": "http://localhost:64513"})
        assert response.status_code == 200, (response.status_code, response.text)
        code = parse_qs(urlsplit(response.json()["login_url"]).query)["code"][0]
        response = await client.post("/api/openapi/v1/auth/link-exchange", json={"code": code})
        assert response.status_code == 200, (response.status_code, response.text)
        assert response.json()["user"]["id"] == fixture["user_id"]
        token = response.json()["access_token"]
        response = await client.get(f"/api/agents/{fixture['employee_id']}", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, (response.status_code, response.text)
        fixture["access_url"] = employee["access_url"]
        Path("/tmp/openapi-fixture.json").write_text(json.dumps(fixture))
        Path("/tmp/openapi-fixture.json").chmod(0o600)
        print("PASS: admin creates client -> OAuth token -> visible employee -> generic signed login link -> ordinary user login -> existing employee API")
        print("Fixture credentials saved only inside isolated container /tmp/openapi-fixture.json")


if __name__ == "__main__":
    asyncio.run(main())
