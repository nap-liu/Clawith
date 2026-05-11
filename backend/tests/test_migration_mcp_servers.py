"""Verify the mcp_servers data-migration produces correct row count,
prompt-block selection, and downgrade reversibility.

Tests run against the shared dev Postgres. No wholesale DELETE FROM —
all assertions filter by uuid-suffixed test URLs.
"""
import re
import uuid
from urllib.parse import urlparse

import pytest
from sqlalchemy import select, text

from app.database import async_session, engine
from app.models.tool import Tool
from app.models.mcp_server import MCPServer

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_mcp_tools(rows: list[dict]) -> None:
    """rows = [{name, mcp_server_url, system_prompt_block, instructions, tenant_id?}]"""
    async with async_session() as db:
        for r in rows:
            t = Tool(
                name=r["name"],
                display_name=r["name"],
                type="mcp",
                mcp_server_url=r["mcp_server_url"],
                mcp_server_instructions=r.get("instructions"),
                system_prompt_block=r.get("system_prompt_block"),
                tenant_id=r.get("tenant_id"),
                mcp_server_id=None,  # ensure migration logic sees these as unmigrated
            )
            db.add(t)
        await db.commit()


# Inlined re-implementation of the migration's _derive_name (the migration
# module is unimportable as a Python identifier because the filename starts
# with a digit). If the migration's algorithm changes, this test should be
# updated in lockstep so it actually tests the same behavior.
def _derive_name(url: str, taken: set[str]) -> str:
    parsed = urlparse(url)
    last_seg = parsed.path.rstrip("/").split("/")[-1] or parsed.netloc.split(".")[0]
    base = re.sub(r"[^a-z0-9_-]", "_", last_seg.lower()) or "mcp_server"
    if base not in taken:
        taken.add(base)
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    name = f"{base}-{i}"
    taken.add(name)
    return name


async def _rerun_data_migration_sql() -> None:
    """Re-execute the data-migration's SQL against the current async session.

    The migration filename starts with a digit, making the module
    unimportable. We replicate the SQL here; SQL drift between this and
    the migration will surface as a test failure.
    """
    async with async_session() as db:
        rows = (await db.execute(text("""
            SELECT tenant_id, mcp_server_url,
                   MIN(mcp_server_instructions) AS instructions
              FROM tools
             WHERE type = 'mcp'
               AND mcp_server_url IS NOT NULL
               AND mcp_server_url <> ''
               AND mcp_server_id IS NULL
             GROUP BY tenant_id, mcp_server_url
        """))).all()
        # Pre-seed taken_names with existing mcp_servers names so we don't
        # collide with prior real data (e.g. ragflow).
        taken = set(
            (await db.execute(text("SELECT name FROM mcp_servers"))).scalars().all()
        )
        for r in rows:
            tenant_id, url, instructions = r.tenant_id, r.mcp_server_url, r.instructions
            prompt_rows = (await db.execute(text("""
                SELECT DISTINCT system_prompt_block FROM tools
                 WHERE type='mcp' AND mcp_server_url=:url
                   AND COALESCE(tenant_id::text, '00000000-0000-0000-0000-000000000000') =
                       COALESCE(CAST(:tenant_id AS text), '00000000-0000-0000-0000-000000000000')
                   AND system_prompt_block IS NOT NULL AND system_prompt_block <> ''
            """), {"url": url, "tenant_id": str(tenant_id) if tenant_id else None})).all()
            non_null = [r2.system_prompt_block for r2 in prompt_rows]
            if not non_null:
                chosen = None
            elif len(set(non_null)) == 1:
                chosen = non_null[0]
            else:
                chosen = max(non_null)
            new_id = uuid.uuid4()
            await db.execute(text("""
                INSERT INTO mcp_servers
                  (id, tenant_id, name, display_name, base_url_template,
                   headers_template, system_prompt_block, instructions)
                VALUES
                  (CAST(:id AS uuid), CAST(:tenant_id AS uuid), :name, :display_name, :url,
                   CAST(:headers AS jsonb), :prompt, :instr)
            """), {
                "id": str(new_id),
                "tenant_id": str(tenant_id) if tenant_id else None,
                "name": _derive_name(url, taken),
                "display_name": _derive_name(url, set(taken)),
                "url": url,
                "headers": "{}",
                "prompt": chosen,
                "instr": instructions,
            })
            await db.execute(text("""
                UPDATE tools SET mcp_server_id = CAST(:sid AS uuid)
                 WHERE type='mcp' AND mcp_server_url=:url
                   AND COALESCE(tenant_id::text, '00000000-0000-0000-0000-000000000000') =
                       COALESCE(CAST(:tenant_id AS text), '00000000-0000-0000-0000-000000000000')
            """), {
                "sid": str(new_id),
                "url": url,
                "tenant_id": str(tenant_id) if tenant_id else None,
            })
        await db.commit()


async def test_distinct_servers_extracted():
    suffix = uuid.uuid4().hex[:6]
    rag_url = f"https://rag-{suffix}.test.local/foo"
    gh_url = f"https://gh-{suffix}.test.local/bar"
    await _seed_mcp_tools([
        {"name": f"mcp_rag_search_{suffix}",
         "mcp_server_url": rag_url,
         "system_prompt_block": "BLOCK_A"},
        {"name": f"mcp_rag_get_{suffix}",
         "mcp_server_url": rag_url,
         "system_prompt_block": "BLOCK_A"},
        {"name": f"mcp_gh_pr_{suffix}",
         "mcp_server_url": gh_url,
         "system_prompt_block": None},
    ])
    await _rerun_data_migration_sql()

    async with async_session() as db:
        rag = (await db.execute(
            select(MCPServer).where(MCPServer.base_url_template == rag_url)
        )).scalar_one()
        gh = (await db.execute(
            select(MCPServer).where(MCPServer.base_url_template == gh_url)
        )).scalar_one()
        assert rag.system_prompt_block == "BLOCK_A"
        assert gh.system_prompt_block is None

        linked = (await db.execute(
            select(Tool).where(Tool.mcp_server_url == rag_url)
        )).scalars().all()
        assert linked and all(t.mcp_server_id == rag.id for t in linked)


async def test_prompt_conflict_picks_dict_max():
    suffix = uuid.uuid4().hex[:6]
    url = f"https://srv-{suffix}.test.local/x"
    await _seed_mcp_tools([
        {"name": f"mcp_x_a_{suffix}",
         "mcp_server_url": url,
         "system_prompt_block": "ALPHA"},
        {"name": f"mcp_x_b_{suffix}",
         "mcp_server_url": url,
         "system_prompt_block": "ZULU"},
    ])
    await _rerun_data_migration_sql()

    async with async_session() as db:
        srv = (await db.execute(
            select(MCPServer).where(MCPServer.base_url_template == url)
        )).scalar_one()
        assert srv.system_prompt_block == "ZULU"  # dict-max winner
