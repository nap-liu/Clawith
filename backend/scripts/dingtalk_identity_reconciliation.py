"""Audit and repair strict legacy DingTalk identity splits.

This is a one-shot operational command.  It does not import ``app.main`` and
therefore does not start IM listeners, schedulers, trigger daemons, or workers.

Report mode is read-only:

    REPORT_HMAC_KEY=... python -m scripts.dingtalk_identity_reconciliation \
      report --provider-id <uuid> --output /secure/path/report.json

Apply mode accepts only immutable safe candidates from a report and re-fetches
current DingTalk claims before every transaction:

    REPORT_HMAC_KEY=... python -m scripts.dingtalk_identity_reconciliation \
      apply --report /secure/path/report.json \
      --confirm APPLY_LEGACY_DINGTALK_REPAIR
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from types import SimpleNamespace
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.services.dingtalk_identity_reconciliation import (
    dingtalk_legacy_identity_reconciler,
    fetch_fresh_dingtalk_claims,
)
from app.services.canonical_user_resolver import (
    CanonicalIdentityConflict,
    CanonicalUserConflict,
)


def _claims_hmac(key: bytes, *, email: str | None, phone: str | None) -> str:
    payload = json.dumps(
        {"email": email, "phone": phone},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _snapshot_digest(snapshot: dict) -> str:
    payload = json.dumps(
        snapshot,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _snapshot_signature(key: bytes, snapshot: dict) -> str:
    payload = json.dumps(
        snapshot,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _hmac_key() -> bytes:
    value = os.environ.get("REPORT_HMAC_KEY", "")
    if len(value) < 32:
        raise RuntimeError("REPORT_HMAC_KEY must contain at least 32 characters")
    return value.encode()


async def create_report(
    *,
    provider_id: uuid.UUID,
    output: Path,
    limit: int | None,
    external_id: str | None,
    request_interval: float,
) -> dict:
    key = _hmac_key()
    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider_id)
        if provider is None or provider.provider_type != "dingtalk":
            raise RuntimeError("active DingTalk provider not found")
        query = (
            select(OrgMember.id)
            .where(
                OrgMember.provider_id == provider_id,
                OrgMember.status == "active",
                OrgMember.external_id.is_not(None),
                OrgMember.user_id.is_not(None),
            )
            .order_by(OrgMember.id)
        )
        if external_id:
            query = query.where(OrgMember.external_id == external_id)
        if limit:
            query = query.limit(limit)
        member_ids = list((await db.execute(query)).scalars().all())

    rows: list[dict] = []
    counts: Counter[str] = Counter()
    for index, member_id in enumerate(member_ids):
        async with async_session() as db:
            provider = await db.get(IdentityProvider, provider_id)
            member = await db.get(OrgMember, member_id)
            if provider is None or member is None or not member.external_id:
                continue
            claims = await fetch_fresh_dingtalk_claims(
                provider,
                member.external_id,
            )
            if claims is None:
                status = "fresh_claims_unavailable"
                counts[status] += 1
                rows.append(
                    {
                        "member_id": str(member.id),
                        "external_id_hmac": hmac.new(
                            key,
                            member.external_id.encode(),
                            hashlib.sha256,
                        ).hexdigest(),
                        "status": status,
                    }
                )
                await db.rollback()
            else:
                try:
                    outcome = await dingtalk_legacy_identity_reconciler.reconcile(
                        db,
                        provider=provider,
                        org_member=member,
                        claims=claims,
                        apply=False,
                    )
                except (CanonicalIdentityConflict, CanonicalUserConflict) as exc:
                    outcome = SimpleNamespace(
                        status="ambiguous",
                        source_user_id=member.user_id,
                        target_user_id=None,
                        reason=type(exc).__name__,
                    )
                counts[outcome.status] += 1
                rows.append(
                    {
                        "member_id": str(member.id),
                        "source_user_id": (
                            str(outcome.source_user_id)
                            if outcome.source_user_id
                            else None
                        ),
                        "target_user_id": (
                            str(outcome.target_user_id)
                            if outcome.target_user_id
                            else None
                        ),
                        "external_id_hmac": hmac.new(
                            key,
                            member.external_id.encode(),
                            hashlib.sha256,
                        ).hexdigest(),
                        "claims_hmac": _claims_hmac(
                            key,
                            email=claims.email,
                            phone=claims.phone,
                        ),
                        "has_email": bool(claims.email),
                        "has_mobile": bool(claims.phone),
                        "alternate_email_conflict": (
                            claims.has_alternate_email_conflict
                        ),
                        "status": outcome.status,
                        "reason": outcome.reason,
                    }
                )
                await db.rollback()
        if request_interval > 0 and index + 1 < len(member_ids):
            await asyncio.sleep(request_interval)

    snapshot = {
        "report_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider_id": str(provider_id),
        "row_count": len(rows),
        "counts": dict(sorted(counts.items())),
        "rows": rows,
        "nonce": secrets.token_hex(16),
    }
    envelope = {
        "snapshot": snapshot,
        "sha256": _snapshot_digest(snapshot),
        "hmac_sha256": _snapshot_signature(key, snapshot),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(envelope, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return envelope


async def apply_report(*, report: Path, confirm: str) -> Counter[str]:
    if confirm != "APPLY_LEGACY_DINGTALK_REPAIR":
        raise RuntimeError("apply requires the exact confirmation phrase")
    key = _hmac_key()
    envelope = json.loads(report.read_text(encoding="utf-8"))
    snapshot = envelope.get("snapshot")
    if (
        not isinstance(snapshot, dict)
        or envelope.get("sha256") != _snapshot_digest(snapshot)
        or not hmac.compare_digest(
            str(envelope.get("hmac_sha256") or ""),
            _snapshot_signature(key, snapshot),
        )
    ):
        raise RuntimeError("report digest is invalid")

    provider_id = uuid.UUID(snapshot["provider_id"])
    results: Counter[str] = Counter()
    for row in snapshot.get("rows", []):
        if row.get("status") != "safe_candidate":
            continue
        async with async_session() as db:
            try:
                provider = await db.get(IdentityProvider, provider_id)
                member = await db.get(OrgMember, uuid.UUID(row["member_id"]))
                if provider is None or member is None or not member.external_id:
                    results["missing_current_row"] += 1
                    await db.rollback()
                    continue
                if str(member.user_id) != row.get("source_user_id"):
                    results["source_changed"] += 1
                    await db.rollback()
                    continue
                claims = await fetch_fresh_dingtalk_claims(
                    provider,
                    member.external_id,
                )
                if claims is None:
                    results["fresh_claims_unavailable"] += 1
                    await db.rollback()
                    continue
                outcome = await dingtalk_legacy_identity_reconciler.reconcile(
                    db,
                    provider=provider,
                    org_member=member,
                    claims=claims,
                    apply=True,
                )
                if (
                    outcome.repaired
                    and str(outcome.target_user_id) == row.get("target_user_id")
                ):
                    await db.commit()
                    results["repaired"] += 1
                else:
                    await db.rollback()
                    results[
                        f"skipped_{outcome.reason or outcome.status}"
                    ] += 1
            except (CanonicalIdentityConflict, CanonicalUserConflict) as exc:
                await db.rollback()
                results[f"skipped_{type(exc).__name__}"] += 1
    return results


async def _main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--provider-id", type=uuid.UUID, required=True)
    report_parser.add_argument("--output", type=Path, required=True)
    report_parser.add_argument("--limit", type=int)
    report_parser.add_argument("--external-id")
    report_parser.add_argument("--request-interval", type=float, default=0.1)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--report", type=Path, required=True)
    apply_parser.add_argument("--confirm", required=True)

    args = parser.parse_args()
    if args.mode == "report":
        envelope = await create_report(
            provider_id=args.provider_id,
            output=args.output,
            limit=args.limit,
            external_id=args.external_id,
            request_interval=max(args.request_interval, 0),
        )
        print(
            json.dumps(
                {
                    "report_id": envelope["snapshot"]["report_id"],
                    "row_count": envelope["snapshot"]["row_count"],
                    "counts": envelope["snapshot"]["counts"],
                    "sha256": envelope["sha256"],
                },
                sort_keys=True,
            )
        )
    else:
        results = await apply_report(report=args.report, confirm=args.confirm)
        print(json.dumps(dict(sorted(results.items())), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
