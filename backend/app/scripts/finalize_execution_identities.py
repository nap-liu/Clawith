"""Idempotently finalize legacy background execution identities.

Safe defaults: no write occurs unless ``--apply`` is supplied. Run after all
old backend instances have exited so rows they wrote with NULL identities are
included. Completed trigger history is intentionally immutable.

Usage:
    python -m app.scripts.finalize_execution_identities          # dry-run
    python -m app.scripts.finalize_execution_identities --apply
    python -m app.scripts.finalize_execution_identities --verify
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from sqlalchemy import text

from app.database import async_session


_ORIGIN_UUID = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    "[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_COUNTS_SQL = text(
    """
    SELECT 'trigger' AS resource_type, count(*) AS missing
      FROM agent_triggers
     WHERE execution_user_id IS NULL OR created_by_user_id IS NULL
    UNION ALL
    SELECT 'task', count(*) FROM tasks WHERE execution_user_id IS NULL
    UNION ALL
    SELECT 'schedule', count(*) FROM agent_schedules WHERE execution_user_id IS NULL
    UNION ALL
    SELECT 'unfinished_execution', count(*) FROM trigger_executions
      WHERE execution_user_id IS NULL AND status IN ('pending', 'processing')
    """
)


async def _counts(db) -> dict[str, int]:
    rows = (await db.execute(_COUNTS_SQL)).all()
    return {name: int(count) for name, count in rows}


async def run(*, apply: bool, verify: bool) -> int:
    run_id = str(uuid.uuid4())
    async with async_session() as db:
        await db.execute(text("SET LOCAL lock_timeout = '2s'"))
        before = await _counts(db)
        if not apply:
            await db.rollback()
            print(json.dumps({"mode": "verify" if verify else "dry-run", "missing": before}))
            return 1 if verify and any(before.values()) else 0

        locked = await db.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtext('finalize_execution_identities_v1'))")
        )
        if not locked:
            await db.rollback()
            print(json.dumps({"mode": "apply", "error": "another finalizer is running"}))
            return 2

        trigger_result = await db.execute(
            text(
                f"""
                UPDATE agent_triggers AS trigger
                   SET execution_user_id = COALESCE(
                           trigger.execution_user_id,
                           CASE
                               WHEN trigger.type = 'on_message'
                                AND COALESCE(trigger.config->>'_origin_session_id', '') <> ''
                                AND COALESCE(trigger.config->>'_origin_user_id', '') ~* '{_ORIGIN_UUID}'
                                AND EXISTS (
                                    SELECT 1 FROM users
                                     WHERE users.id = (trigger.config->>'_origin_user_id')::uuid
                                       AND users.tenant_id = agent.tenant_id
                                )
                               THEN (trigger.config->>'_origin_user_id')::uuid
                               ELSE agent.creator_id
                           END
                       ),
                       created_by_user_id = COALESCE(
                           trigger.created_by_user_id,
                           CASE
                               WHEN COALESCE(trigger.config->>'_origin_user_id', '') ~* '{_ORIGIN_UUID}'
                                AND EXISTS (
                                    SELECT 1 FROM users
                                     WHERE users.id = (trigger.config->>'_origin_user_id')::uuid
                                       AND users.tenant_id = agent.tenant_id
                                )
                               THEN (trigger.config->>'_origin_user_id')::uuid
                               ELSE agent.creator_id
                           END
                       )
                  FROM agents AS agent
                 WHERE trigger.agent_id = agent.id
                   AND (
                       trigger.execution_user_id IS NULL
                       OR trigger.created_by_user_id IS NULL
                   )
                """
            )
        )
        task_result = await db.execute(
            text(
                """
                UPDATE tasks AS task
                   SET execution_user_id = CASE
                       WHEN task.type::text = 'supervision' THEN task.created_by
                       ELSE agent.creator_id
                   END
                  FROM agents AS agent
                 WHERE task.agent_id = agent.id
                   AND task.execution_user_id IS NULL
                """
            )
        )
        schedule_result = await db.execute(
            text(
                """
                UPDATE agent_schedules AS schedule
                   SET execution_user_id = agent.creator_id
                  FROM agents AS agent
                 WHERE schedule.agent_id = agent.id
                   AND schedule.execution_user_id IS NULL
                """
            )
        )
        execution_result = await db.execute(
            text(
                f"""
                UPDATE trigger_executions AS execution
                   SET execution_user_id = COALESCE(
                       CASE
                           WHEN trigger.type = 'on_message'
                            AND COALESCE(execution.payload->>'_origin_session_id', '') <> ''
                            AND COALESCE(execution.payload->>'_origin_user_id', '') ~* '{_ORIGIN_UUID}'
                            AND EXISTS (
                                SELECT 1 FROM users
                                 WHERE users.id = (execution.payload->>'_origin_user_id')::uuid
                                   AND users.tenant_id = agent.tenant_id
                            )
                           THEN (execution.payload->>'_origin_user_id')::uuid
                           ELSE NULL
                       END,
                       trigger.execution_user_id,
                       agent.creator_id
                   )
                  FROM agent_triggers AS trigger, agents AS agent
                 WHERE execution.trigger_id = trigger.id
                   AND execution.agent_id = agent.id
                   AND execution.execution_user_id IS NULL
                   AND execution.status IN ('pending', 'processing')
                """
            )
        )
        changed = {
            "trigger": int(trigger_result.rowcount or 0),
            "task": int(task_result.rowcount or 0),
            "schedule": int(schedule_result.rowcount or 0),
            "unfinished_execution": int(execution_result.rowcount or 0),
        }
        after = await _counts(db)
        await db.execute(
            text(
                """
                INSERT INTO audit_logs (id, action, details, created_at)
                VALUES (:id, 'execution_identity_finalized',
                        CAST(:details AS json), now())
                """
            ),
            {
                "id": uuid.uuid4(),
                "details": json.dumps(
                    {"run_id": run_id, "before": before, "changed": changed, "after": after}
                )
            },
        )
        if any(after.values()):
            await db.rollback()
            print(json.dumps({"mode": "apply", "run_id": run_id, "error": "verification failed", "after": after}))
            return 3
        await db.commit()
        print(json.dumps({"mode": "apply", "run_id": run_id, "before": before, "changed": changed, "after": after}))
        return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(apply=args.apply, verify=args.verify)))


if __name__ == "__main__":
    main()
