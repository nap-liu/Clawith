"""Authentication helpers shared by Gateway API modules."""

import hashlib

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent

def _hash_key(key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()


async def _get_agent_by_key(api_key: str, db: AsyncSession) -> Agent:
    """Authenticate an OpenClaw agent by its API key."""
    # First try plaintext (new behavior)
    result = await db.execute(
        select(Agent).where(
            Agent.api_key_hash == api_key,
            Agent.agent_type == "openclaw",
        )
    )
    agent = result.scalar_one_or_none()

    # Fallback to hashed (legacy behavior)
    if not agent:
        key_hash = _hash_key(api_key)
        result = await db.execute(
            select(Agent).where(
                Agent.api_key_hash == key_hash,
                Agent.agent_type == "openclaw",
            )
        )
        agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return agent
