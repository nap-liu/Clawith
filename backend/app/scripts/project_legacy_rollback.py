"""Prepare and restore the database boundary for a legacy backend rollback.

The project release adds project-scoped Agent rows that older binaries do not
understand.  An older API must therefore never connect with the schema-owner
database role: that role bypasses row-level security and can resolve a project
Agent by a known UUID.

Run ``apply`` with the candidate image after stopping writers, then start the
legacy API and worker with the dedicated login printed by this helper.  Run
``restore`` with the candidate image after stopping the legacy processes.

The helper does not delete or update business rows and never persists the
legacy login password.  Its database changes are transactional and reversible.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.database import engine

LEGACY_ROLE = "clawith_project_legacy"
PASSWORD_ENV = "PROJECT_LEGACY_ROLLBACK_PASSWORD"
STATE_TABLE = "project_legacy_rollback_state"
CLASSIFIER_FUNCTION = "clawith_legacy_is_project_agent"
LOCK_NAME = "clawith_project_legacy_rollback"


@dataclass(frozen=True)
class TableBoundary:
    table_name: str
    predicate: str


def _ident(value: str) -> str:
    """Quote a PostgreSQL identifier obtained from the catalog or constants."""

    return '"' + value.replace('"', '""') + '"'


def _policy_name(kind: str, table_name: str) -> str:
    digest = hashlib.sha256(table_name.encode()).hexdigest()[:8]
    return f"clw_legacy_{kind}_{table_name[:28]}_{digest}"


async def _formatted_ddl(
    connection: AsyncConnection,
    template: str,
    *values: str,
) -> str:
    placeholders = ", ".join(f"CAST(:value_{index} AS text)" for index in range(len(values)))
    statement = text(f"SELECT format(CAST(:template AS text), {placeholders})")
    parameters: dict[str, str] = {"template": template}
    parameters.update({f"value_{index}": value for index, value in enumerate(values)})
    return str(await connection.scalar(statement, parameters))


async def _role_exists(connection: AsyncConnection) -> bool:
    return bool(
        await connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
            {"role": LEGACY_ROLE},
        )
    )


async def _table_boundaries(connection: AsyncConnection) -> list[TableBoundary]:
    """Return every Agent-linked table the old backend can query directly.

    Foreign-key discovery keeps the boundary normalized as Agent-linked tables
    evolve.  The explicit project/session predicates cover durable project work
    whose identity is not represented by an Agent foreign key alone.  Plaza
    uses a polymorphic author rather than a foreign key and is included here as
    the sole explicit adapter.
    """

    rows = (
        await connection.execute(
            text(
                """
                SELECT tc.table_name, array_agg(kcu.column_name ORDER BY kcu.column_name)
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage AS ccu
                  ON ccu.constraint_name = tc.constraint_name
                 AND ccu.table_schema = tc.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = 'public'
                  AND ccu.table_schema = 'public'
                  AND ccu.table_name = 'agents'
                  AND tc.table_name <> 'agents'
                GROUP BY tc.table_name
                ORDER BY tc.table_name
                """
            )
        )
    ).all()

    by_table: dict[str, list[str]] = {
        str(table_name): [str(column) for column in columns] for table_name, columns in rows
    }
    predicates: dict[str, list[str]] = {"agents": ["scope IS DISTINCT FROM 'project'"]}
    for table_name, columns in by_table.items():
        predicates.setdefault(table_name, []).extend(
            f"NOT public.{_ident(CLASSIFIER_FUNCTION)}({_ident(column_name)})" for column_name in columns
        )

    # Some worker queues, including subagent_runs, carry a project boundary but
    # do not reference Agent with a foreign key. Discover that boundary itself
    # so a legacy worker cannot claim project work.
    project_tables = (
        await connection.execute(
            text(
                """
                SELECT table_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND column_name = 'project_id'
                ORDER BY table_name
                """
            )
        )
    ).scalars()
    for table_name in project_tables:
        predicates.setdefault(str(table_name), []).append("project_id IS NULL")

    for table_name in ("plaza_posts", "plaza_comments", "plaza_likes"):
        exists = await connection.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = :table_name
                )
                """
            ),
            {"table_name": table_name},
        )
        if exists:
            predicates.setdefault(table_name, []).append(
                f"(author_type IS DISTINCT FROM 'agent' OR NOT public.{_ident(CLASSIFIER_FUNCTION)}(author_id))"
            )
    return [TableBoundary(table_name, " AND ".join(checks)) for table_name, checks in sorted(predicates.items())]


async def _ensure_state_table(connection: AsyncConnection) -> None:
    await connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS public.{_ident(STATE_TABLE)} (
                singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                legacy_role text NOT NULL,
                role_created boolean NOT NULL,
                table_rls_state jsonb NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    )


async def _load_state(connection: AsyncConnection) -> dict[str, Any] | None:
    row = (
        (
            await connection.execute(
                text(
                    f"""
                SELECT legacy_role, role_created, table_rls_state, applied_at
                FROM public.{_ident(STATE_TABLE)}
                WHERE singleton = true
                FOR UPDATE
                """
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


async def _capture_rls_state(
    connection: AsyncConnection,
    boundaries: list[TableBoundary],
) -> dict[str, dict[str, bool]]:
    table_names = [boundary.table_name for boundary in boundaries]
    rows = (
        await connection.execute(
            text(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relnamespace = 'public'::regnamespace
                  AND relname = ANY(:table_names)
                """
            ),
            {"table_names": table_names},
        )
    ).all()
    return {
        str(table_name): {
            "enabled": bool(enabled),
            "forced": bool(forced),
        }
        for table_name, enabled, forced in rows
    }


async def _ensure_role(
    connection: AsyncConnection,
    password: str | None,
    *,
    allow_existing: bool,
) -> bool:
    role_exists = await _role_exists(connection)
    if role_exists and not allow_existing:
        raise RuntimeError(
            f"database role {LEGACY_ROLE} already exists before rollback apply; refusing to alter an unmanaged role"
        )
    if not role_exists:
        if not password:
            raise RuntimeError(f"{PASSWORD_ENV} is required when creating the dedicated legacy role")
        await connection.execute(text(f"CREATE ROLE {_ident(LEGACY_ROLE)} LOGIN"))
    await connection.execute(
        text(
            f"ALTER ROLE {_ident(LEGACY_ROLE)} LOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
        )
    )
    if password:
        ddl = await _formatted_ddl(
            connection,
            "ALTER ROLE %I LOGIN PASSWORD %L",
            LEGACY_ROLE,
            password,
        )
        await connection.execute(text(ddl))
    return not role_exists


async def _grant_legacy_runtime_access(connection: AsyncConnection) -> None:
    database_name = str(await connection.scalar(text("SELECT current_database()")))
    statements = [
        await _formatted_ddl(
            connection,
            "GRANT CONNECT ON DATABASE %I TO %I",
            database_name,
            LEGACY_ROLE,
        ),
        f"GRANT USAGE ON SCHEMA public TO {_ident(LEGACY_ROLE)}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {_ident(LEGACY_ROLE)}",
        f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {_ident(LEGACY_ROLE)}",
    ]
    for statement in statements:
        await connection.execute(text(statement))
    await connection.execute(text(f"REVOKE ALL ON TABLE public.{_ident(STATE_TABLE)} FROM {_ident(LEGACY_ROLE)}"))


async def _ensure_classifier(connection: AsyncConnection) -> None:
    await connection.execute(
        text(
            f"""
            CREATE OR REPLACE FUNCTION public.{_ident(CLASSIFIER_FUNCTION)}(candidate uuid)
            RETURNS boolean
            LANGUAGE sql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $$
                SELECT COALESCE((
                    SELECT scope = 'project'
                    FROM public.agents
                    WHERE id = candidate
                ), false)
            $$
            """
        )
    )
    await connection.execute(text(f"REVOKE ALL ON FUNCTION public.{_ident(CLASSIFIER_FUNCTION)}(uuid) FROM PUBLIC"))
    await connection.execute(
        text(f"GRANT EXECUTE ON FUNCTION public.{_ident(CLASSIFIER_FUNCTION)}(uuid) TO {_ident(LEGACY_ROLE)}")
    )


async def _replace_policy(
    connection: AsyncConnection,
    boundary: TableBoundary,
    kind: str,
    clause: str,
) -> None:
    table_name = _ident(boundary.table_name)
    policy_name = _ident(_policy_name(kind, boundary.table_name))
    await connection.execute(text(f"DROP POLICY IF EXISTS {policy_name} ON public.{table_name}"))
    await connection.execute(text(clause))


async def _apply_table_boundary(
    connection: AsyncConnection,
    boundary: TableBoundary,
    original_state: dict[str, bool],
) -> None:
    table_name = _ident(boundary.table_name)
    await connection.execute(text(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY"))

    if not original_state["enabled"]:
        public_policy = _ident(_policy_name("public", boundary.table_name))
        await _replace_policy(
            connection,
            boundary,
            "public",
            f"CREATE POLICY {public_policy} ON public.{table_name} "
            "AS PERMISSIVE FOR ALL TO PUBLIC USING (true) WITH CHECK (true)",
        )

    allow_policy = _ident(_policy_name("allow", boundary.table_name))
    await _replace_policy(
        connection,
        boundary,
        "allow",
        f"CREATE POLICY {allow_policy} ON public.{table_name} AS PERMISSIVE "
        f"FOR ALL TO {_ident(LEGACY_ROLE)} USING ({boundary.predicate}) "
        f"WITH CHECK ({boundary.predicate})",
    )
    limit_policy = _ident(_policy_name("limit", boundary.table_name))
    await _replace_policy(
        connection,
        boundary,
        "limit",
        f"CREATE POLICY {limit_policy} ON public.{table_name} AS RESTRICTIVE "
        f"FOR ALL TO {_ident(LEGACY_ROLE)} USING ({boundary.predicate}) "
        f"WITH CHECK ({boundary.predicate})",
    )


async def apply(password: str | None = None) -> dict[str, Any]:
    async with engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": LOCK_NAME})
        await _ensure_state_table(connection)
        existing_state = await _load_state(connection)
        role_created_now = await _ensure_role(
            connection,
            password,
            allow_existing=existing_state is not None,
        )
        await _grant_legacy_runtime_access(connection)
        await _ensure_classifier(connection)
        boundaries = await _table_boundaries(connection)

        if existing_state:
            if existing_state["legacy_role"] != LEGACY_ROLE:
                raise RuntimeError("legacy rollback state belongs to a different database role")
            original_rls_state = existing_state["table_rls_state"]
        else:
            original_rls_state = await _capture_rls_state(connection, boundaries)
            await connection.execute(
                text(
                    f"""
                    INSERT INTO public.{_ident(STATE_TABLE)}
                        (singleton, legacy_role, role_created, table_rls_state)
                    VALUES (true, :legacy_role, :role_created, CAST(:table_rls_state AS jsonb))
                    """
                ),
                {
                    "legacy_role": LEGACY_ROLE,
                    "role_created": role_created_now,
                    "table_rls_state": json.dumps(original_rls_state),
                },
            )

        missing_state = {
            boundary.table_name for boundary in boundaries if boundary.table_name not in original_rls_state
        }
        if missing_state:
            raise RuntimeError(
                "rollback boundary changed while active; restore before re-applying: "
                + ", ".join(sorted(missing_state))
            )
        for boundary in boundaries:
            await _apply_table_boundary(
                connection,
                boundary,
                original_rls_state[boundary.table_name],
            )

        return {
            "action": "apply",
            "applied": existing_state is None,
            "legacy_role": LEGACY_ROLE,
            "protected_tables": len(boundaries),
            "password_stored": False,
            "required_process_roles": ["api", "worker"],
            "owner_database_url_allowed": False,
        }


async def restore() -> dict[str, Any]:
    async with engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": LOCK_NAME})
        await _ensure_state_table(connection)
        state = await _load_state(connection)
        if not state:
            return {"action": "restore", "restored": False, "legacy_role": LEGACY_ROLE}

        original_rls_state = state["table_rls_state"]
        for table_name, original_state in original_rls_state.items():
            quoted_table = _ident(table_name)
            for kind in ("public", "allow", "limit"):
                await connection.execute(
                    text(f"DROP POLICY IF EXISTS {_ident(_policy_name(kind, table_name))} ON public.{quoted_table}")
                )
            await connection.execute(
                text(
                    f"ALTER TABLE public.{quoted_table} "
                    + ("FORCE" if original_state["forced"] else "NO FORCE")
                    + " ROW LEVEL SECURITY"
                )
            )
            await connection.execute(
                text(
                    f"ALTER TABLE public.{quoted_table} "
                    + ("ENABLE" if original_state["enabled"] else "DISABLE")
                    + " ROW LEVEL SECURITY"
                )
            )

        await connection.execute(text(f"DROP FUNCTION IF EXISTS public.{_ident(CLASSIFIER_FUNCTION)}(uuid)"))
        await connection.execute(text(f"DELETE FROM public.{_ident(STATE_TABLE)} WHERE singleton = true"))
        if state["role_created"] and await _role_exists(connection):
            await connection.execute(text(f"DROP OWNED BY {_ident(LEGACY_ROLE)}"))
            await connection.execute(text(f"DROP ROLE {_ident(LEGACY_ROLE)}"))

        return {
            "action": "restore",
            "restored": True,
            "legacy_role": LEGACY_ROLE,
            "restored_tables": len(original_rls_state),
        }


async def status() -> dict[str, Any]:
    async with engine.connect() as connection:
        state_table_exists = bool(
            await connection.scalar(
                text("SELECT to_regclass(:table_name) IS NOT NULL"),
                {"table_name": f"public.{STATE_TABLE}"},
            )
        )
        if state_table_exists:
            row = (
                (
                    await connection.execute(
                        text(
                            f"""
                            SELECT legacy_role, role_created, table_rls_state, applied_at
                            FROM public.{_ident(STATE_TABLE)}
                            WHERE singleton = true
                            """
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            state = dict(row) if row else None
        else:
            state = None
        return {
            "action": "status",
            "active": state is not None,
            "legacy_role": LEGACY_ROLE,
            "role_exists": await _role_exists(connection),
            "protected_tables": len(state["table_rls_state"]) if state else 0,
            "password_stored": False,
        }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "restore", "status"))
    return parser.parse_args()


async def _run() -> dict[str, Any]:
    args = _parse_args()
    if args.action == "apply":
        return await apply(os.environ.get(PASSWORD_ENV))
    if args.action == "restore":
        return await restore()
    return await status()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_run()), sort_keys=True))
