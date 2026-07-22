"""Backfill exact OAuth subject bindings.

Revision ID: oauth_subject_bindings
Revises: session_context_termination
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "oauth_subject_bindings"
down_revision: Union[str, Sequence[str], None] = "session_context_termination"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SUBJECT_POLICY = "custom-user-id-or-sub-userId-id:v1"
_EMAIL_POLICY = "custom-email-or-email:v1"


def _authority_scope(provider_id: uuid.UUID, config: dict) -> str:
    mapping = config.get("field_mapping") if isinstance(config.get("field_mapping"), dict) else {}
    payload = {
        "authorize_url": str(config.get("authorize_url") or ""),
        "client_id": str(config.get("client_id") or config.get("app_id") or ""),
        "email_selector": str(mapping.get("email") or _EMAIL_POLICY),
        "issuer": str(config.get("issuer") or ""),
        "provider_id": str(provider_id),
        "subject_selector": str(mapping.get("user_id") or _SUBJECT_POLICY),
        "token_url": str(config.get("token_url") or ""),
        "user_info_url": str(config.get("user_info_url") or ""),
        "version": "v1",
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return f"oauth2-authority:v1:{hashlib.sha256(canonical.encode()).hexdigest()}"


def upgrade() -> None:
    connection = op.get_bind()
    op.create_check_constraint(
        "ck_oauth_binding_provider_required",
        "channel_user_bindings",
        "channel_type <> 'oauth2' OR (provider_id IS NOT NULL AND id_type = 'subject')",
    )

    rows = connection.execute(
        sa.text(
            """
            SELECT om.id AS member_id,
                   om.tenant_id,
                   om.provider_id,
                   btrim(om.external_id) AS subject,
                   om.user_id,
                   usr.identity_id,
                   usr.is_active AS user_active,
                   usr.tenant_id AS user_tenant_id,
                   ip.tenant_id AS provider_tenant_id,
                   ip.config AS provider_config
            FROM org_members AS om
            JOIN identity_providers AS ip ON ip.id = om.provider_id
            LEFT JOIN users AS usr ON usr.id = om.user_id
            WHERE ip.provider_type = 'oauth2'
              AND ip.is_active = true
              AND ip.sso_login_enabled = true
              AND om.status = 'active'
              AND om.external_id IS NOT NULL
              AND btrim(om.external_id) <> ''
              AND om.user_id IS NOT NULL
            ORDER BY om.provider_id, btrim(om.external_id), om.id
            """
        )
    ).mappings().all()

    candidates: dict[tuple[uuid.UUID, str, str], dict] = {}
    for row in rows:
        if (
            row["tenant_id"] is None
            or row["provider_tenant_id"] != row["tenant_id"]
            or row["user_tenant_id"] != row["tenant_id"]
            or row["identity_id"] is None
            or not row["user_active"]
        ):
            raise RuntimeError(f"unsafe OAuth binding backfill row: org_member={row['member_id']}")
        provider_config = row["provider_config"] if isinstance(row["provider_config"], dict) else {}
        scope = _authority_scope(row["provider_id"], provider_config)
        key = (row["tenant_id"], scope, row["subject"])
        previous = candidates.get(key)
        if previous and previous["user_id"] != row["user_id"]:
            raise RuntimeError(
                "OAuth provider subject maps to multiple users: "
                f"provider={row['provider_id']} subject_sha256={hashlib.sha256(row['subject'].encode()).hexdigest()}"
            )
        candidates[key] = dict(row)

    existing_rows = connection.execute(
        sa.text(
            """
            SELECT tenant_id, provider_id, installation_scope, subject, user_id
            FROM channel_user_bindings
            WHERE channel_type = 'oauth2' AND id_type = 'subject'
            """
        )
    ).mappings().all()
    existing = {
        (row["tenant_id"], row["installation_scope"], row["subject"]): row for row in existing_rows
    }
    for key, row in candidates.items():
        existing_row = existing.get(key)
        if existing_row and (
            existing_row["provider_id"] != row["provider_id"]
            or existing_row["user_id"] != row["user_id"]
        ):
            raise RuntimeError("existing OAuth subject binding does not match its exact provider and user")
        connection.execute(
            sa.text(
                """
                INSERT INTO channel_user_bindings
                    (id, tenant_id, provider_id, installation_scope, channel_type,
                     id_type, subject, user_id, created_at)
                VALUES
                    (:id, :tenant_id, :provider_id, :installation_scope, 'oauth2',
                     'subject', :subject, :user_id, CURRENT_TIMESTAMP)
                ON CONFLICT (tenant_id, installation_scope, id_type, subject) DO NOTHING
                """
            ),
            {
                "id": uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"clawith:oauth2:{key[0]}:{key[1]}:{key[2]}",
                ),
                "tenant_id": row["tenant_id"],
                "provider_id": row["provider_id"],
                "installation_scope": key[1],
                "subject": key[2],
                "user_id": row["user_id"],
            },
        )

    checksum_input = "\n".join(
        f"{key[0]}|{row['provider_id']}|{hashlib.sha256(key[2].encode()).hexdigest()}|{row['user_id']}"
        for key, row in sorted(candidates.items(), key=lambda item: tuple(str(part) for part in item[0]))
    )
    checksum = hashlib.sha256(checksum_input.encode()).hexdigest()
    print(f"OAuth subject binding backfill: rows={len(candidates)} checksum={checksum}")


def downgrade() -> None:
    # The backfilled rows become live trust anchors and may be indistinguishable
    # from bindings subsequently used by the application. Preserve them when
    # rolling code back; only remove the schema guard introduced here.
    op.drop_constraint(
        "ck_oauth_binding_provider_required",
        "channel_user_bindings",
        type_="check",
    )
