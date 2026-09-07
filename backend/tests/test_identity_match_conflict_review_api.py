"""Observable API behavior for tenant-scoped identity conflict review."""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.database import async_session, engine
from app.main import app
from app.models.audit import AuditLog
from app.models.identity import IdentityProvider
from app.models.org import ChannelUserBinding, OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.canonical_user_resolver import canonical_user_resolver
from app.services.provider_identity_policy import (
    record_identity_match_conflict,
    record_subject_contact_conflict,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_connections():
    await engine.dispose()
    yield
    await engine.dispose()


async def _user(*, tenant_id, role: str, label: str) -> tuple[User, str]:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        identity = Identity(
            username=f"{label}-{suffix}",
            email=f"{label}-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name=label,
            role=role,
            tenant_id=tenant_id,
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user, create_access_token(str(user.id), role)


async def _seed_scope():
    async with async_session() as db:
        tenant_a = Tenant(name="Conflict A", slug=f"conflict-a-{uuid.uuid4().hex[:8]}")
        tenant_b = Tenant(name="Conflict B", slug=f"conflict-b-{uuid.uuid4().hex[:8]}")
        db.add_all([tenant_a, tenant_b])
        await db.flush()
        provider_a = IdentityProvider(
            provider_type="dingtalk",
            name="Directory A",
            tenant_id=tenant_a.id,
            is_active=True,
            config={},
        )
        provider_b = IdentityProvider(
            provider_type="dingtalk",
            name="Directory B",
            tenant_id=tenant_b.id,
            is_active=True,
            config={},
        )
        db.add_all([provider_a, provider_b])
        await db.commit()
        return tenant_a, tenant_b, provider_a, provider_b


async def test_conflict_queue_is_admin_only_tenant_scoped_and_pii_safe():
    tenant_a, tenant_b, provider_a, provider_b = await _seed_scope()
    _admin_a, admin_token = await _user(tenant_id=tenant_a.id, role="org_admin", label="admin-a")
    _member, member_token = await _user(tenant_id=tenant_a.id, role="member", label="member-a")
    affected, _ = await _user(tenant_id=tenant_a.id, role="member", label="affected")

    async with async_session() as db:
        own_conflict = AuditLog(
            user_id=affected.id,
            action="identity_match_lower_priority_conflict",
            details={
                "tenant_id": str(tenant_a.id),
                "provider_id": str(provider_a.id),
                "provider_type": "dingtalk",
                "source": "directory_contact",
                "matched_by": "phone",
                "conflicting_fields": ["email"],
                "raw_email": "must-not-leak@example.com",
            },
        )
        foreign_conflict = AuditLog(
            action="provider_subject_contact_conflict",
            details={
                "tenant_id": str(tenant_b.id),
                "provider_id": str(provider_b.id),
                "source": "im_inbound",
                "subject": "secret-provider-subject",
            },
        )
        db.add_all([own_conflict, foreign_conflict])
        await db.commit()
        own_id = own_conflict.id
        foreign_id = foreign_conflict.id

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get(
            "/api/enterprise/identity-conflicts",
            params={"provider_id": str(provider_a.id)},
            headers=admin_headers,
        )
        member_denied = await client.get(
            "/api/enterprise/identity-conflicts", headers=member_headers
        )
        foreign_provider_hidden = await client.get(
            "/api/enterprise/identity-conflicts",
            params={"provider_id": str(provider_b.id)},
            headers=admin_headers,
        )
        foreign_conflict_hidden = await client.post(
            f"/api/enterprise/identity-conflicts/{foreign_id}/review",
            json={"status": "reviewing"},
            headers=admin_headers,
        )
        foreign_repair_hidden = await client.post(
            f"/api/enterprise/identity-conflicts/{foreign_id}/resolve",
            json={"action": "keep_people_separate"},
            headers=admin_headers,
        )

    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    item = listed.json()["items"][0]
    assert item["id"] == str(own_id)
    assert item["provider_name"] == "Directory A"
    assert item["source"] == "directory_contact"
    assert item["reason"] == "lower_priority_identity_mismatch"
    assert item["matched_by"] == "phone"
    assert item["conflicting_fields"] == ["email"]
    assert item["status"] == "pending"
    assert item["evidence"] == []
    assert item["allowed_actions"] == []
    assert item["repair_unavailable_reason"] == "missing_verified_evidence"
    assert item["masked_identifier"].startswith("••••")
    serialized = json.dumps(item)
    assert "must-not-leak" not in serialized
    assert "secret-provider-subject" not in serialized
    assert str(affected.id) not in serialized
    assert str(provider_a.id) not in serialized
    assert member_denied.status_code == 403
    assert foreign_provider_hidden.status_code == 404
    assert foreign_conflict_hidden.status_code == 404
    assert foreign_repair_hidden.status_code == 404


async def test_review_status_is_append_only_and_does_not_change_accounts():
    tenant, _foreign_tenant, provider, _foreign_provider = await _seed_scope()
    admin, token = await _user(tenant_id=tenant.id, role="org_admin", label="review-admin")
    affected, _ = await _user(tenant_id=tenant.id, role="member", label="review-target")
    original_identity_id = affected.identity_id
    async with async_session() as db:
        conflict = AuditLog(
            user_id=affected.id,
            action="provider_subject_contact_conflict",
            details={
                "tenant_id": str(tenant.id),
                "provider_id": str(provider.id),
                "source": "sso_login",
                "matched_by": "email",
            },
        )
        db.add(conflict)
        await db.commit()
        conflict_id = conflict.id

    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing_outcome = await client.post(
            f"/api/enterprise/identity-conflicts/{conflict_id}/review",
            json={"status": "reviewed"},
            headers=headers,
        )
        reviewing = await client.post(
            f"/api/enterprise/identity-conflicts/{conflict_id}/review",
            json={"status": "reviewing"},
            headers=headers,
        )
        reviewed = await client.post(
            f"/api/enterprise/identity-conflicts/{conflict_id}/review",
            json={
                "status": "reviewed",
                "outcome": "manual_identity_repair_required",
            },
            headers=headers,
        )
        listed = await client.get(
            "/api/enterprise/identity-conflicts",
            params={"provider_id": str(provider.id)},
            headers=headers,
        )

    assert missing_outcome.status_code == 422
    assert reviewing.status_code == 200, reviewing.text
    assert reviewing.json()["status"] == "reviewing"
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "reviewed"
    assert reviewed.json()["outcome"] == "manual_identity_repair_required"
    assert listed.json()["items"][0]["status"] == "reviewed"

    async with async_session() as db:
        unchanged = await db.get(User, affected.id)
        original_conflict = await db.get(AuditLog, conflict_id)
        review_count = await db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "identity_match_conflict_reviewed",
                AuditLog.user_id == admin.id,
            )
        )
        assert unchanged is not None
        assert unchanged.identity_id == original_identity_id
        assert unchanged.is_active is True
        assert original_conflict is not None
        assert original_conflict.action == "provider_subject_contact_conflict"
        assert review_count == 2


async def test_global_admin_must_choose_an_explicit_tenant():
    tenant, _foreign_tenant, _provider, _foreign_provider = await _seed_scope()
    _admin, token = await _user(tenant_id=None, role="platform_admin", label="global-admin")
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unscoped = await client.get(
            "/api/enterprise/identity-conflicts", headers=headers
        )
        scoped = await client.get(
            "/api/enterprise/identity-conflicts",
            params={"tenant_id": str(tenant.id)},
            headers=headers,
        )

    assert unscoped.status_code == 400
    assert scoped.status_code == 200
    assert scoped.json() == {"items": [], "total": 0}


async def _seed_actionable_conflict(*, source_email_owned: bool = True):
    tenant, foreign_tenant, provider, _foreign_provider = await _seed_scope()
    admin, token = await _user(
        tenant_id=tenant.id, role="org_admin", label="repair-admin"
    )
    bound, _ = await _user(tenant_id=tenant.id, role="member", label="Bound User")
    target, _ = await _user(tenant_id=tenant.id, role="member", label="Phone User")
    email_target, _ = await _user(
        tenant_id=tenant.id, role="member", label="Email User"
    )
    source_phone = f"86138{uuid.uuid4().int % 10**8:08d}"
    source_email = f"source-{uuid.uuid4().hex[:8]}@example.com"
    bound_phone = f"86139{uuid.uuid4().int % 10**8:08d}"
    bound_email = f"bound-{uuid.uuid4().hex[:8]}@example.com"
    external_id = f"staff-{uuid.uuid4().hex[:10]}"
    open_id = f"open-{uuid.uuid4().hex[:10]}"

    async with async_session() as db:
        provider_row = await db.get(IdentityProvider, provider.id)
        provider_row.config = {
            "identity_match_policy": {"ordered_fields": ["phone", "email"]}
        }
        bound_row = await db.get(User, bound.id)
        target_row = await db.get(User, target.id)
        email_row = await db.get(User, email_target.id)
        bound_row.identity.phone = bound_phone
        bound_row.identity.email = bound_email
        target_row.identity.phone = source_phone
        target_row.identity.email = None
        email_row.identity.phone = None
        if source_email_owned:
            email_row.identity.email = source_email
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            external_id=external_id,
            open_id=open_id,
            name="Conflicted directory account",
            phone=source_phone,
            email=source_email,
            status="active",
            user_id=bound.id,
        )
        db.add(member)
        await db.flush()
        claims = await canonical_user_resolver.resolve_identity_claims(
            db,
            email=source_email,
            phone=source_phone,
            enrich=False,
            ordered_fields=["phone", "email"],
        )
        await record_subject_contact_conflict(
            db,
            provider=provider_row,
            tenant_id=tenant.id,
            user_id=bound.id,
            claims=claims,
            source="directory_contact",
            source_member_id=member.id,
        )
        db.add_all(
            [
                ChannelUserBinding(
                    tenant_id=tenant.id,
                    provider_id=provider.id,
                    installation_scope="provider:test",
                    channel_type="dingtalk",
                    id_type="staff_id",
                    subject=external_id,
                    user_id=bound.id,
                ),
                ChannelUserBinding(
                    tenant_id=tenant.id,
                    provider_id=provider.id,
                    installation_scope="agent:test",
                    channel_type="dingtalk",
                    id_type="open_id",
                    subject=open_id,
                    user_id=bound.id,
                ),
            ]
        )
        await db.commit()
        conflict_id = await db.scalar(
            select(AuditLog.id).where(
                AuditLog.action == "provider_subject_contact_conflict",
                AuditLog.user_id == bound.id,
            )
        )
        return {
            "tenant": tenant,
            "foreign_tenant": foreign_tenant,
            "provider": provider,
            "admin": admin,
            "token": token,
            "bound": bound,
            "target": target,
            "email_target": email_target,
            "member_id": member.id,
            "conflict_id": conflict_id,
            "source_phone": source_phone,
            "source_email": source_email,
            "bound_phone": bound_phone,
            "bound_email": bound_email,
        }


async def test_actionable_conflict_lists_masked_values_and_tenant_users():
    seeded = await _seed_actionable_conflict()
    headers = {"Authorization": f"Bearer {seeded['token']}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/enterprise/identity-conflicts",
            params={"provider_id": str(seeded["provider"].id)},
            headers=headers,
        )

    assert response.status_code == 200, response.text
    item = next(
        row
        for row in response.json()["items"]
        if row["id"] == str(seeded["conflict_id"])
    )
    assert item["bound_user"]["display_name"] == "Bound User"
    evidence = {row["field"]: row for row in item["evidence"]}
    assert evidence["phone"]["source_value"] == seeded["source_phone"]
    assert evidence["phone"]["candidate_user"]["display_name"] == "Phone User"
    assert evidence["phone"]["candidate_user"]["phone"] == seeded["source_phone"]
    assert evidence["phone"]["is_highest_priority"] is True
    assert evidence["email"]["source_value"] == seeded["source_email"]
    assert evidence["email"]["candidate_user"]["display_name"] == "Email User"
    assert evidence["email"]["candidate_user"]["email"] == seeded["source_email"]
    assert evidence["email"]["is_conflicting"] is True
    assert set(item["allowed_actions"]) == {
        "rebind_source_to_highest_priority",
        "merge_users",
        "keep_people_separate",
    }
    assert {candidate["display_name"] for candidate in item["merge_candidates"]} == {
        "Bound User",
        "Phone User",
        "Email User",
    }
    serialized = response.text
    assert seeded["source_phone"] in serialized
    assert seeded["source_email"] in serialized
    assert str(seeded["bound"].id) not in serialized
    assert str(seeded["target"].id) not in serialized


async def test_rebind_resolution_updates_source_and_channel_bindings_once():
    seeded = await _seed_actionable_conflict()
    headers = {"Authorization": f"Bearer {seeded['token']}"}
    path = f"/api/enterprise/identity-conflicts/{seeded['conflict_id']}/resolve"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            path,
            json={"action": "rebind_source_to_highest_priority"},
            headers=headers,
        )
        repeated = await client.post(
            path,
            json={"action": "rebind_source_to_highest_priority"},
            headers=headers,
        )

    assert response.status_code == 200, response.text
    assert repeated.status_code == 200, repeated.text
    assert response.json()["status"] == "resolved"
    assert response.json()["resolution_action"] == "rebind_source_to_highest_priority"
    async with async_session() as db:
        member = await db.get(OrgMember, seeded["member_id"])
        binding_users = set(
            (
                await db.execute(
                    select(ChannelUserBinding.user_id).where(
                        ChannelUserBinding.provider_id == seeded["provider"].id
                    )
                )
            ).scalars()
        )
        resolution_count = await db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "identity_match_conflict_resolved",
                AuditLog.details["conflict_id"].as_string()
                == str(seeded["conflict_id"]),
            )
        )
        users_still_exist = await db.scalar(
            select(func.count(User.id)).where(
                User.id.in_([seeded["bound"].id, seeded["target"].id])
            )
        )
    assert member.user_id == seeded["target"].id
    assert binding_users == {seeded["target"].id}
    assert resolution_count == 1
    assert users_still_exist == 2


async def test_merge_can_select_an_unowned_current_source_contact():
    seeded = await _seed_actionable_conflict(source_email_owned=False)
    headers = {"Authorization": f"Bearer {seeded['token']}"}
    path = f"/api/enterprise/identity-conflicts/{seeded['conflict_id']}/resolve"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        listed = await client.get(
            "/api/enterprise/identity-conflicts",
            params={"provider_id": str(seeded["provider"].id)},
            headers=headers,
        )
        item = listed.json()["items"][0]
        retained = next(
            user for user in item["merge_candidates"]
            if user["display_name"] == "Bound User"
        )
        phone = next(
            user for user in item["merge_candidates"]
            if user["display_name"] == "Phone User"
        )
        response = await client.post(
            path,
            json={
                "action": "merge_users",
                "target_reference": retained["reference"],
                "field_sources": {
                    "phone": phone["reference"],
                    "email": "SOURCE_ACCOUNT",
                },
            },
            headers=headers,
        )

    assert response.status_code == 200, response.text
    async with async_session() as db:
        retained = await db.get(User, seeded["bound"].id)
        assert retained.identity.email == seeded["source_email"]


async def test_merge_resolution_converges_all_tenant_identity_entrances():
    seeded = await _seed_actionable_conflict()
    headers = {"Authorization": f"Bearer {seeded['token']}"}
    path = f"/api/enterprise/identity-conflicts/{seeded['conflict_id']}/resolve"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        listed = await client.get(
            "/api/enterprise/identity-conflicts",
            params={"provider_id": str(seeded["provider"].id)},
            headers=headers,
        )
        item = next(
            row
            for row in listed.json()["items"]
            if row["id"] == str(seeded["conflict_id"])
        )
        retained = next(
            candidate
            for candidate in item["merge_candidates"]
            if candidate["display_name"] == "Bound User"
        )
        phone_source = next(
            candidate for candidate in item["merge_candidates"]
            if candidate["display_name"] == "Phone User"
        )
        response = await client.post(
            path,
            json={
                "action": "merge_users",
                "target_reference": retained["reference"],
                "field_sources": {
                    "phone": phone_source["reference"],
                    "email": "SOURCE_ACCOUNT",
                },
            },
            headers=headers,
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "resolved"
    assert response.json()["resolution_action"] == "merge_users"
    async with async_session() as db:
        users = {
            user.id: user
            for user in (
                await db.execute(
                    select(User).where(
                        User.id.in_(
                            [
                                seeded["bound"].id,
                                seeded["target"].id,
                                seeded["email_target"].id,
                            ]
                        )
                    )
                )
            ).scalars()
        }
        member = await db.get(OrgMember, seeded["member_id"])
        binding_users = set(
            (
                await db.execute(
                    select(ChannelUserBinding.user_id).where(
                        ChannelUserBinding.provider_id == seeded["provider"].id
                    )
                )
            ).scalars()
        )
        resolution = await db.scalar(
            select(AuditLog).where(
                AuditLog.action == "identity_match_conflict_resolved",
                AuditLog.details["conflict_id"].as_string()
                == str(seeded["conflict_id"]),
            )
        )
        provider = await db.get(IdentityProvider, seeded["provider"].id)
        claims = await canonical_user_resolver.resolve_identity_claims(
            db,
            email=member.email,
            phone=member.phone,
            enrich=False,
            ordered_fields=["phone", "email"],
        )
        repeated = await record_identity_match_conflict(
            db,
            provider=provider,
            tenant_id=seeded["tenant"].id,
            claims=claims,
            source="directory_contact",
            user_id=member.user_id,
            source_member_id=member.id,
        )
    assert users[seeded["bound"].id].is_active is True
    assert users[seeded["target"].id].is_active is False
    assert users[seeded["email_target"].id].is_active is False
    assert users[seeded["bound"].id].identity.phone == seeded["source_phone"]
    assert users[seeded["bound"].id].identity.email == seeded["source_email"]
    assert users[seeded["target"].id].identity.phone is None
    assert users[seeded["email_target"].id].identity.email is None
    assert member.user_id == seeded["bound"].id
    assert binding_users == {seeded["bound"].id}
    assert resolution.details["merged_user_count"] == 2
    assert resolution.details["target_reference"] == retained["reference"]
    assert resolution.details["selected_contact_fields"] == ["email", "phone"]
    assert resolution.details["resolution_request"]["field_sources"] == {
        "email": "SOURCE_ACCOUNT",
        "phone": phone_source["reference"],
    }
    assert repeated is False


async def test_historical_conflict_is_revalidated_from_one_current_source_account():
    seeded = await _seed_actionable_conflict()
    async with async_session() as db:
        conflict = await db.get(AuditLog, seeded["conflict_id"])
        conflict.details = {
            "tenant_id": str(seeded["tenant"].id),
            "provider_id": str(seeded["provider"].id),
            "provider_type": "dingtalk",
            "source": "directory_contact",
            "matched_by": "phone",
            "conflicting_fields": ["email"],
        }
        await db.commit()

    headers = {"Authorization": f"Bearer {seeded['token']}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/enterprise/identity-conflicts",
            params={"provider_id": str(seeded["provider"].id)},
            headers=headers,
        )

    assert response.status_code == 200, response.text
    item = next(
        row
        for row in response.json()["items"]
        if row["id"] == str(seeded["conflict_id"])
    )
    assert item["repair_unavailable_reason"] is None
    assert "merge_users" in item["allowed_actions"]
    assert {evidence["field"] for evidence in item["evidence"]} == {
        "phone",
        "email",
    }


async def test_changed_source_evidence_is_shown_but_direct_repair_is_disabled():
    seeded = await _seed_actionable_conflict()
    changed_phone = f"86136{uuid.uuid4().int % 10**8:08d}"
    async with async_session() as db:
        member = await db.get(OrgMember, seeded["member_id"])
        member.phone = changed_phone
        await db.commit()

    headers = {"Authorization": f"Bearer {seeded['token']}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/enterprise/identity-conflicts",
            params={"provider_id": str(seeded["provider"].id)},
            headers=headers,
        )
        blocked = await client.post(
            f"/api/enterprise/identity-conflicts/{seeded['conflict_id']}/resolve",
            json={"action": "keep_people_separate"},
            headers=headers,
        )

    assert response.status_code == 200, response.text
    item = next(
        row
        for row in response.json()["items"]
        if row["id"] == str(seeded["conflict_id"])
    )
    phone = next(row for row in item["evidence"] if row["field"] == "phone")
    assert phone["source_value"] == changed_phone
    assert item["evidence_changed"] is True
    assert item["allowed_actions"] == []
    assert item["repair_unavailable_reason"] == "evidence_changed"
    assert blocked.status_code == 409


async def test_keep_separate_suppresses_same_evidence_but_changed_evidence_reopens():
    seeded = await _seed_actionable_conflict()
    headers = {"Authorization": f"Bearer {seeded['token']}"}
    path = f"/api/enterprise/identity-conflicts/{seeded['conflict_id']}/resolve"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            path,
            json={"action": "keep_people_separate"},
            headers=headers,
        )
        second = await client.post(
            path,
            json={"action": "keep_people_separate"},
            headers=headers,
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    async with async_session() as db:
        member = await db.get(OrgMember, seeded["member_id"])
        provider = await db.get(IdentityProvider, seeded["provider"].id)
        same_claims = await canonical_user_resolver.resolve_identity_claims(
            db,
            email=member.email,
            phone=member.phone,
            enrich=False,
            ordered_fields=["phone", "email"],
        )
        await record_subject_contact_conflict(
            db,
            provider=provider,
            tenant_id=seeded["tenant"].id,
            user_id=seeded["bound"].id,
            claims=same_claims,
            source="directory_contact",
            source_member_id=member.id,
        )
        await db.commit()
        conflict_count_same = await db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "provider_subject_contact_conflict",
                AuditLog.user_id == seeded["bound"].id,
            )
        )

        changed_phone = f"86137{uuid.uuid4().int % 10**8:08d}"
        changed_identity = Identity(phone=changed_phone)
        db.add(changed_identity)
        await db.flush()
        db.add(
            User(
                identity_id=changed_identity.id,
                tenant_id=seeded["tenant"].id,
                display_name="Changed Phone User",
                role="member",
                is_active=True,
            )
        )
        member.phone = changed_phone
        await db.flush()
        changed_claims = await canonical_user_resolver.resolve_identity_claims(
            db,
            email=member.email,
            phone=member.phone,
            enrich=False,
            ordered_fields=["phone", "email"],
        )
        await record_subject_contact_conflict(
            db,
            provider=provider,
            tenant_id=seeded["tenant"].id,
            user_id=seeded["bound"].id,
            claims=changed_claims,
            source="directory_contact",
            source_member_id=member.id,
        )
        await db.commit()
        conflict_count_changed = await db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "provider_subject_contact_conflict",
                AuditLog.user_id == seeded["bound"].id,
            )
        )
        resolution_count = await db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "identity_match_conflict_resolved",
                AuditLog.details["conflict_id"].as_string()
                == str(seeded["conflict_id"]),
            )
        )
    assert first.json()["resolution_action"] == "keep_people_separate"
    assert conflict_count_same == 1
    assert conflict_count_changed == 2
    assert resolution_count == 1
