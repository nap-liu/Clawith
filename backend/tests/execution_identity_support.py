"""Shared database fixture and user seed for execution identity tests."""

import pytest

from app.database import engine
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _dispose_engine_between_cases():
    await engine.dispose()
    yield
    await engine.dispose()


async def _user(db, tenant_id, role, suffix):
    identity = Identity(
        username=f"executor_{suffix}",
        email=f"executor_{suffix}@test.local",
        password_hash="test",
    )
    db.add(identity)
    await db.flush()
    user = User(
        identity_id=identity.id,
        tenant_id=tenant_id,
        display_name=suffix,
        role=role,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user
