"""Personal Access Token (PAT) service.

Issues, verifies, revokes and lists per-user long-lived tokens used for
MCP server inbound channel authentication.

Security contract:
- Plaintext is returned only once from ``issue_pat``; the caller must store it.
- Only the sha256 hex digest is persisted (``token_hash``).
- ``verify_pat`` does a single hash-based lookup — no plaintext / dual-query path.
- Tokens that are revoked or expired are treated as absent.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.personal_access_token import PersonalAccessToken
from app.models.user import User

if TYPE_CHECKING:
    import uuid


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def issue_pat(
    db: AsyncSession,
    *,
    user: User,
    name: str,
    expires_at: datetime | None = None,
) -> tuple[str, PersonalAccessToken]:
    """Issue a new PAT for *user*.

    Returns ``(token_plaintext, row)``.  The plaintext is **never stored**;
    the caller is responsible for delivering it to the end-user exactly once.

    Raises ``ValueError`` when ``user.tenant_id`` is ``None`` — PATs are
    always tenant-scoped.
    """
    if user.tenant_id is None:
        raise ValueError("Cannot issue a PAT for a user with no tenant_id")

    token = "clw_" + secrets.token_urlsafe(32)

    row = PersonalAccessToken(
        user_id=user.id,
        tenant_id=user.tenant_id,
        name=name,
        token_hash=_sha256(token),
        token_prefix=token[:8],
        expires_at=expires_at,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return token, row


async def verify_pat(
    db: AsyncSession,
    token: str,
) -> tuple[User | None, "uuid.UUID | None"]:
    """Verify *token* and return ``(user, tenant_id)`` or ``(None, None)``.

    Fast-path rejections (no DB hit):
    - Empty string or does not start with ``clw_``.

    Single-hash lookup:
    - Revoked or expired rows → ``(None, None)``.
    - Missing user or inactive user → ``(None, None)``.

    On success: refreshes ``last_used_at`` and commits.
    """
    if not token or not token.startswith("clw_"):
        return None, None

    token_hash = _sha256(token)

    result = await db.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.token_hash == token_hash)
    )
    pat = result.scalar_one_or_none()

    if pat is None:
        return None, None

    if pat.revoked_at is not None:
        return None, None

    now = _now_utc()
    if pat.expires_at is not None:
        # Support both tz-aware and tz-naive expires_at from DB
        expires = pat.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now > expires:
            return None, None

    # Refresh last_used_at
    pat.last_used_at = now
    db.add(pat)

    # Load user
    user_result = await db.execute(select(User).where(User.id == pat.user_id))
    user = user_result.scalar_one_or_none()

    if user is None or not user.is_active:
        await db.rollback()
        return None, None

    await db.commit()
    return user, pat.tenant_id


async def revoke_pat(
    db: AsyncSession,
    *,
    user: User,
    token_id: "uuid.UUID",
) -> bool:
    """Revoke the PAT identified by *token_id* for *user*.

    Returns ``True`` if the row was found and revoked, ``False`` otherwise
    (including when the token belongs to a different user).
    """
    result = await db.execute(
        select(PersonalAccessToken).where(
            PersonalAccessToken.id == token_id,
            PersonalAccessToken.user_id == user.id,
        )
    )
    pat = result.scalar_one_or_none()

    if pat is None:
        return False

    pat.revoked_at = _now_utc()
    db.add(pat)
    await db.commit()
    return True


async def list_pats(
    db: AsyncSession,
    *,
    user: User,
) -> list[PersonalAccessToken]:
    """Return all non-revoked PATs for *user*, newest first."""
    result = await db.execute(
        select(PersonalAccessToken)
        .where(
            PersonalAccessToken.user_id == user.id,
            PersonalAccessToken.revoked_at.is_(None),
        )
        .order_by(PersonalAccessToken.created_at.desc())
    )
    return list(result.scalars().all())
