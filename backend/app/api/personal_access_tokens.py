"""PAT (Personal Access Token) management REST API.

Endpoints:
    POST   /api/personal-access-tokens        create a new PAT
    GET    /api/personal-access-tokens        list caller's active PATs
    DELETE /api/personal-access-tokens/{id}   revoke a PAT
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.personal_access_token import PATCreate, PATCreated, PATOut
from app.services.pat_service import issue_pat, list_pats, revoke_pat

router = APIRouter(prefix="/personal-access-tokens", tags=["personal-access-tokens"])


@router.post("", response_model=PATCreated, status_code=status.HTTP_201_CREATED)
async def create_pat(
    body: PATCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PATCreated:
    """Issue a new PAT for the authenticated user.

    The plaintext token is returned exactly once in the response.
    Store it securely — it cannot be retrieved again.
    """
    try:
        token_plaintext, row = await issue_pat(
            db,
            user=current_user,
            name=body.name,
            expires_at=body.expires_at,
            scope=body.scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return PATCreated(
        id=row.id,
        name=row.name,
        token=token_plaintext,
        token_prefix=row.token_prefix,
        scope=row.scope,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


@router.get("", response_model=list[PATOut])
async def get_pats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[PATOut]:
    """List all active (non-revoked) PATs for the authenticated user."""
    rows = await list_pats(db, user=current_user)
    return [PATOut.model_validate(row) for row in rows]


@router.delete("/{token_id}", status_code=status.HTTP_200_OK)
async def delete_pat(
    token_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Revoke a PAT belonging to the authenticated user.

    Returns 404 if the token does not exist or belongs to another user.
    """
    ok = await revoke_pat(db, user=current_user, token_id=token_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    return {"ok": True}
