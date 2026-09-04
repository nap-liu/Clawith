"""Validate context and runtime-governance constraints online after migration.

The schema migrations install these constraints as ``NOT VALID`` so new writes
are enforced immediately without scanning existing rows under their DDL locks. This
release command validates each table in its own bounded transaction; ordinary
reads and writes remain compatible, and a lock conflict fails quickly.

Usage:
    python -m app.scripts.validate_context_governance_constraints
    python -m app.scripts.validate_context_governance_constraints --apply
    python -m app.scripts.validate_context_governance_constraints --verify
"""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import async_session

_CONSTRAINTS = (
    ("llm_models", "ck_llm_models_keep_recent_turns", "c"),
    ("llm_models", "ck_llm_models_context_usage_ratio", "c"),
    ("agents", "ck_agents_daily_memory_load_days", "c"),
    ("agents", "ck_agents_temperature", "c"),
    ("agents", "ck_agents_reasoning_effort", "c"),
    ("tasks", "fk_tasks_model_id_llm_models", "f"),
    ("tasks", "ck_tasks_temperature", "c"),
    ("tasks", "ck_tasks_reasoning_effort", "c"),
    ("agent_schedules", "fk_agent_schedules_model_id_llm_models", "f"),
    ("agent_schedules", "ck_agent_schedules_temperature", "c"),
    ("agent_schedules", "ck_agent_schedules_reasoning_effort", "c"),
    ("agent_triggers", "fk_agent_triggers_model_id_llm_models", "f"),
    ("agent_triggers", "ck_agent_triggers_temperature", "c"),
    ("agent_triggers", "ck_agent_triggers_reasoning_effort", "c"),
    ("subagent_runs", "fk_subagent_runs_model_id_llm_models", "f"),
    ("subagent_runs", "ck_subagent_runs_temperature", "c"),
    ("subagent_runs", "ck_subagent_runs_reasoning_effort", "c"),
)

_STATE_SQL = text(
    """
    SELECT constraint_row.convalidated
      FROM pg_constraint AS constraint_row
      JOIN pg_class AS table_row ON table_row.oid = constraint_row.conrelid
      JOIN pg_namespace AS schema_row ON schema_row.oid = table_row.relnamespace
     WHERE schema_row.nspname = 'public'
       AND table_row.relname = :table_name
       AND constraint_row.conname = :constraint_name
       AND constraint_row.contype::text = :constraint_type
    """
)


async def _constraint_state(
    table_name: str,
    constraint_name: str,
    constraint_type: str,
) -> bool | None:
    async with async_session() as db:
        value = (
            await db.execute(
                _STATE_SQL,
                {
                    "table_name": table_name,
                    "constraint_name": constraint_name,
                    "constraint_type": constraint_type,
                },
            )
        ).scalar_one_or_none()
        await db.rollback()
        return bool(value) if value is not None else None


async def run(*, apply: bool, verify: bool) -> int:
    states_before = {
        constraint_name: await _constraint_state(
            table_name,
            constraint_name,
            constraint_type,
        )
        for table_name, constraint_name, constraint_type in _CONSTRAINTS
    }
    missing = [name for name, state in states_before.items() if state is None]
    if missing:
        print(
            json.dumps(
                {
                    "mode": "apply" if apply else ("verify" if verify else "dry-run"),
                    "error": "constraints_not_found",
                    "constraints": missing,
                }
            )
        )
        return 2

    if apply:
        for table_name, constraint_name, _constraint_type in _CONSTRAINTS:
            if states_before[constraint_name]:
                continue
            async with async_session() as db:
                try:
                    await db.execute(text("SET LOCAL lock_timeout = '2s'"))
                    await db.execute(text("SET LOCAL statement_timeout = '10min'"))
                    await db.execute(
                        text(
                            f'ALTER TABLE public."{table_name}" '
                            f'VALIDATE CONSTRAINT "{constraint_name}"'
                        )
                    )
                    await db.commit()
                except SQLAlchemyError as exc:
                    await db.rollback()
                    print(
                        json.dumps(
                            {
                                "mode": "apply",
                                "validated": False,
                                "constraint": constraint_name,
                                "error": exc.__class__.__name__,
                            }
                        )
                    )
                    return 3

    states_after = {
        constraint_name: await _constraint_state(
            table_name,
            constraint_name,
            constraint_type,
        )
        for table_name, constraint_name, constraint_type in _CONSTRAINTS
    }
    validated = all(states_after.values())
    print(
        json.dumps(
            {
                "mode": "apply" if apply else ("verify" if verify else "dry-run"),
                "validated": validated,
                "constraints": states_after,
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
