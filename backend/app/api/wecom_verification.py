"""WeCom domain verification file route."""

import re

from fastapi import APIRouter, Depends, Response
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.identity import IdentityProvider

router = APIRouter()

_VERIFY_FILENAME_RE = re.compile(r"^WW_verify_[A-Za-z0-9_]{1,64}\.txt$")


@router.get("/wecom-verify/{filename}")
async def serve_wecom_verify_file(
    filename: str,
    db: AsyncSession = Depends(get_db),
):
    """Serve a WeCom domain verification file.

    Looks across all active WeCom IdentityProviders for one whose config
    contains the requested filename. Returns the verification content as
    plain text so WeCom's ownership-check bot can confirm it.

    Security: filename is validated against a strict whitelist regex before
    any DB lookup to prevent path traversal or injection attacks.
    """
    # Strict allowlist: only WW_verify_*.txt filenames are legal
    if not _VERIFY_FILENAME_RE.fullmatch(filename):
        return Response(status_code=404)

    # Search all active WeCom providers for a matching verification entry
    result = await db.execute(
        select(IdentityProvider).where(
            IdentityProvider.provider_type == "wecom",
            IdentityProvider.is_active == True,
        )
    )
    providers = result.scalars().all()

    for provider in providers:
        config = provider.config or {}
        verify_files: dict = config.get("wecom_verify_files", {})
        if filename in verify_files:
            content = verify_files[filename]
            logger.info(
                f"[WeCom Verify] Serving {filename} for tenant {provider.tenant_id}"
            )
            return Response(content=content, media_type="text/plain")

    return Response(status_code=404)
