from __future__ import annotations

import json
from types import SimpleNamespace
import uuid

import pytest

from app.database import async_session, engine
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.tenant import Tenant
from app.models.user import User
from app.services.canonical_user_resolver import CanonicalIdentityConflict
from scripts.dingtalk_identity_reconciliation import (
    _snapshot_digest,
    _snapshot_signature,
    apply_report,
)


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


def test_reconciliation_snapshot_signature_is_keyed_and_deterministic():
    snapshot = {
        "report_id": "report-1",
        "provider_id": "provider-1",
        "rows": [{"member_id": "member-1", "status": "safe_candidate"}],
    }

    signature = _snapshot_signature(b"a" * 32, snapshot)

    assert signature == _snapshot_signature(b"a" * 32, snapshot)
    assert signature != _snapshot_signature(b"b" * 32, snapshot)


@pytest.mark.asyncio
async def test_apply_rejects_tampered_snapshot_before_database_access(
    monkeypatch,
    tmp_path,
):
    key = "test-report-key-" + "x" * 32
    monkeypatch.setenv("REPORT_HMAC_KEY", key)
    snapshot = {
        "report_id": "report-1",
        "provider_id": "00000000-0000-0000-0000-000000000001",
        "rows": [],
    }
    envelope = {
        "snapshot": snapshot,
        "sha256": _snapshot_digest(snapshot),
        "hmac_sha256": _snapshot_signature(key.encode(), snapshot),
    }
    envelope["snapshot"]["rows"].append(
        {"member_id": "tampered", "status": "safe_candidate"}
    )
    path = tmp_path / "report.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(RuntimeError, match="digest is invalid"):
        await apply_report(
            report=path,
            confirm="APPLY_LEGACY_DINGTALK_REPAIR",
        )


@pytest.mark.asyncio
async def test_apply_rolls_back_conflict_row_and_continues_next_candidate(
    monkeypatch,
    tmp_path,
):
    key = "test-report-key-" + "y" * 32
    monkeypatch.setenv("REPORT_HMAC_KEY", key)
    async with async_session() as db:
        tenant = Tenant(name="Batch Continue", slug=f"batch-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.flush()
        provider = IdentityProvider(
            provider_type="dingtalk",
            name="DingTalk Batch",
            is_active=True,
            tenant_id=tenant.id,
            config={},
        )
        db.add(provider)
        await db.flush()
        rows = []
        for index in range(2):
            user = User(
                tenant_id=tenant.id,
                display_name=f"Source {index}",
                role="member",
                source="dingtalk",
                registration_source="dingtalk_org_sync",
                is_active=True,
            )
            db.add(user)
            await db.flush()
            member = OrgMember(
                tenant_id=tenant.id,
                provider_id=provider.id,
                external_id=f"staff-{index}",
                user_id=user.id,
                name=user.display_name,
                status="active",
            )
            db.add(member)
            await db.flush()
            rows.append(
                {
                    "member_id": str(member.id),
                    "source_user_id": str(user.id),
                    "target_user_id": str(uuid.uuid4()),
                    "status": "safe_candidate",
                }
            )
        await db.commit()
        provider_id = provider.id

    snapshot = {
        "report_id": str(uuid.uuid4()),
        "provider_id": str(provider_id),
        "rows": rows,
    }
    envelope = {
        "snapshot": snapshot,
        "sha256": _snapshot_digest(snapshot),
        "hmac_sha256": _snapshot_signature(key.encode(), snapshot),
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    async def fresh_claims(_provider, external_id):
        return SimpleNamespace(external_id=external_id)

    calls = 0

    async def reconcile(_db, *, provider, org_member, claims, apply):
        nonlocal calls
        calls += 1
        assert apply is True
        if calls == 1:
            raise CanonicalIdentityConflict("changed after report")
        expected_target = rows[1]["target_user_id"]
        return SimpleNamespace(
            repaired=True,
            target_user_id=uuid.UUID(expected_target),
            reason=None,
            status="repaired",
        )

    monkeypatch.setattr(
        "scripts.dingtalk_identity_reconciliation.fetch_fresh_dingtalk_claims",
        fresh_claims,
    )
    monkeypatch.setattr(
        "scripts.dingtalk_identity_reconciliation."
        "dingtalk_legacy_identity_reconciler.reconcile",
        reconcile,
    )

    results = await apply_report(
        report=path,
        confirm="APPLY_LEGACY_DINGTALK_REPAIR",
    )

    assert calls == 2
    assert results["skipped_CanonicalIdentityConflict"] == 1
    assert results["repaired"] == 1
