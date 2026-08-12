"""Online validation for the large execution-identity foreign key.

The schema migration adds this constraint as ``NOT VALID`` so deployment does
not scan a potentially large history table while holding the migration lock.
New writes are still checked immediately by PostgreSQL. Run this command after
the identity finalizer has completed and old producers have stopped.

Safe defaults: without ``--apply`` the command only reports current state.

Usage:
    python -m app.scripts.validate_execution_identity_fk
    python -m app.scripts.validate_execution_identity_fk --apply
    python -m app.scripts.validate_execution_identity_fk --verify
"""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text

from app.database import async_session


_CONSTRAINT = "fk_trigger_executions_execution_user_id_users"

_STATE_SQL = text(
    """
    SELECT constraint_row.convalidated,
           constraint_row.condeferrable,
           constraint_row.condeferred
      FROM pg_constraint AS constraint_row
      JOIN pg_class AS table_row ON table_row.oid = constraint_row.conrelid
      JOIN pg_namespace AS schema_row ON schema_row.oid = table_row.relnamespace
     WHERE schema_row.nspname = 'public'
       AND table_row.relname = 'trigger_executions'
       AND constraint_row.conname = :constraint_name
       AND constraint_row.contype = 'f'
    """
)

_ORPHAN_COUNT_SQL = text(
    """
    SELECT count(*)
      FROM trigger_executions AS execution
      LEFT JOIN users AS execution_user ON execution_user.id = execution.execution_user_id
     WHERE execution.execution_user_id IS NOT NULL
       AND execution_user.id IS NULL
    """
)


async def _state(db) -> dict[str, bool] | None:
    row = (
        await db.execute(_STATE_SQL, {"constraint_name": _CONSTRAINT})
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


async def run(*, apply: bool, verify: bool) -> int:
    async with async_session() as db:
        state_before = await _state(db)
        if state_before is None:
            await db.rollback()
            print(json.dumps({"mode": "apply" if apply else "verify", "error": "constraint_not_found"}))
            return 2

        orphan_count = int(await db.scalar(_ORPHAN_COUNT_SQL) or 0)
        if orphan_count:
            await db.rollback()
            print(
                json.dumps(
                    {
                        "mode": "apply" if apply else "verify",
                        "constraint": _CONSTRAINT,
                        "validated": bool(state_before["convalidated"]),
                        "orphan_count": orphan_count,
                        "error": "orphan_execution_users",
                    }
                )
            )
            return 3

        if apply and not state_before["convalidated"]:
            # VALIDATE CONSTRAINT uses a write-compatible PostgreSQL lock. Keep
            # lock acquisition bounded so this maintenance command never waits
            # behind an unexpected long transaction during release.
            await db.execute(text("SET LOCAL lock_timeout = '2s'"))
            await db.execute(text("SET LOCAL statement_timeout = '10min'"))
            await db.execute(
                text(
                    'ALTER TABLE public.trigger_executions '
                    f'VALIDATE CONSTRAINT "{_CONSTRAINT}"'
                )
            )
            await db.commit()
        else:
            await db.rollback()

        async with async_session() as verify_db:
            state_after = await _state(verify_db)
            await verify_db.rollback()

        validated = bool(state_after and state_after["convalidated"])
        print(
            json.dumps(
                {
                    "mode": "apply" if apply else ("verify" if verify else "dry-run"),
                    "constraint": _CONSTRAINT,
                    "validated": validated,
                    "orphan_count": orphan_count,
                }
            )
        )
        return 0 if (not verify or validated) else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(apply=args.apply, verify=args.verify)))


if __name__ == "__main__":
    main()
