"""The actual migration process must fail promptly when its online lock is held."""

import asyncio
import sys
import time

import pytest
from sqlalchemy import text

from app.database import engine
from tests.test_group_policy import isolated_engine  # noqa: F401


@pytest.mark.asyncio
async def test_alembic_connection_bounds_bootstrap_lock_wait():
    async with engine.connect() as connection:
        await connection.execute(text("SELECT pg_advisory_lock(724019381)"))
        await connection.commit()
        start = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "alembic", "upgrade", "heads",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
            assert process.returncode != 0
            assert "lock timeout" in (stdout + stderr).decode().lower()
            assert time.monotonic() - start < 15
            assert await connection.scalar(text("SELECT 1")) == 1
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(724019381)"))
            await connection.commit()
