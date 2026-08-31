"""SQL execution helpers extracted from the unified agent tool service."""

DEFAULT_SQL_MAX_ROWS = 5_000
HARD_SQL_MAX_ROWS = 50_000
DEFAULT_SQL_MAX_BYTES = 16 * 1024 * 1024
HARD_SQL_MAX_BYTES = 64 * 1024 * 1024
SQL_DISPLAY_CHAR_BUDGET = 64_000
SQL_FETCH_BATCH = 1_000


def _clamp_sql_max_rows(raw) -> int:
    """Clamp the agent-supplied max_rows into [1, HARD_SQL_MAX_ROWS]; default on garbage."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SQL_MAX_ROWS
    return min(max(1, v), HARD_SQL_MAX_ROWS)


def _resolve_sql_max_bytes() -> int:
    """Per-fetch byte budget, env-overridable, clamped to the hard ceiling."""
    import os

    override = os.environ.get("CLAWITH_SQL_MAX_BYTES")
    if override:
        try:
            return min(max(int(override), 64 * 1024), HARD_SQL_MAX_BYTES)
        except ValueError:
            pass
    return DEFAULT_SQL_MAX_BYTES


async def _bounded_collect(row_source, max_rows: int, max_bytes: int):
    """Collect rows from an async row source under dual limits.

    Stops when either max_rows or max_bytes is reached; sets truncated=True if
    more rows exist beyond the limit (N+1: the source yielded one more row).
    Always returns at least one row when the source is non-empty, even if that
    single row exceeds max_bytes.

    Returns (rows: list[tuple], truncated: bool).
    """
    rows: list = []
    total_bytes = 0
    truncated = False
    async for row in row_source:
        if len(rows) >= max_rows:
            truncated = True  # N+1: one more row exists beyond the cap
            break
        row_bytes = sum(
            len(str(v)) for v in row
        )  # char-count approximation; CJK uses more real bytes — max_rows + host swap are the primary guards
        if rows and total_bytes + row_bytes > max_bytes:
            truncated = True
            break
        rows.append(tuple(row))
        total_bytes += row_bytes
    return rows, truncated


async def _sql_execute(arguments: dict) -> str:
    """Execute SQL on any database via connection URI.

    Parses and clamps max_rows (default DEFAULT_SQL_MAX_ROWS, hard ceiling HARD_SQL_MAX_ROWS)
    and resolves max_bytes (env-overridable, hard ceiling HARD_SQL_MAX_BYTES), then passes both
    through to the DB-specific backend so that streaming and dual-limit enforcement are active
    for every engine.
    """
    import asyncio

    connection_string = arguments.get("connection_string", "").strip()
    sql = arguments.get("sql", "").strip()
    timeout = min(int(arguments.get("timeout", 30)), 120)
    max_rows = _clamp_sql_max_rows(arguments.get("max_rows", DEFAULT_SQL_MAX_ROWS))
    max_bytes = _resolve_sql_max_bytes()

    if not connection_string:
        return "❌ Missing required argument 'connection_string'"
    if not sql:
        return "❌ Missing required argument 'sql'"

    uri_lower = connection_string.lower()
    try:
        if uri_lower.startswith("sqlite"):
            return await asyncio.wait_for(
                _sql_execute_sqlite(connection_string, sql, max_rows, max_bytes), timeout=timeout
            )
        elif uri_lower.startswith("mysql"):
            return await asyncio.wait_for(
                _sql_execute_mysql(connection_string, sql, max_rows, max_bytes), timeout=timeout
            )
        elif uri_lower.startswith("postgresql") or uri_lower.startswith("postgres"):
            return await asyncio.wait_for(
                _sql_execute_postgres(connection_string, sql, max_rows, max_bytes), timeout=timeout
            )
        else:
            return "❌ Unsupported database type. Supported: mysql://, postgresql://, sqlite:///"
    except asyncio.TimeoutError:
        return f"❌ Query timed out after {timeout}s"
    except Exception as e:
        return f"❌ Database error: {type(e).__name__}: {str(e)[:500]}"


async def _sql_execute_sqlite(connection_string: str, sql: str, max_rows: int, max_bytes: int) -> str:
    """Execute SQL on SQLite (step-based, naturally streaming)."""
    import aiosqlite

    db_path = connection_string.replace("sqlite:///", "", 1)
    if not db_path:
        return "❌ Invalid SQLite connection string. Use: sqlite:///path/to/db.sqlite"

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(sql)
        if cursor.description:
            columns = [d[0] for d in cursor.description]

            async def _source():
                while True:
                    chunk = await cursor.fetchmany(SQL_FETCH_BATCH)
                    if not chunk:
                        break
                    for r in chunk:
                        yield tuple(r)

            _src = _source()
            try:
                rows, truncated = await _bounded_collect(_src, max_rows, max_bytes)
            finally:
                await _src.aclose()
            return _format_sql_result(columns, rows, truncated, max_rows)
        else:
            await db.commit()
            return f"✅ Statement executed successfully. Rows affected: {cursor.rowcount}"


async def _sql_execute_mysql(connection_string: str, sql: str, max_rows: int, max_bytes: int) -> str:
    """Execute SQL on MySQL/StarRocks via server-side streaming cursor (SSCursor)."""
    import aiomysql
    from urllib.parse import urlparse, parse_qs, unquote

    parsed = urlparse(connection_string)
    conn = await aiomysql.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 3306,
        user=unquote(parsed.username or "root"),
        password=unquote(parsed.password or ""),
        db=parsed.path.lstrip("/") if parsed.path else None,
        charset=parse_qs(parsed.query).get("charset", ["utf8mb4"])[0],
    )
    try:
        # SSCursor = unbuffered/server-side; rows stream instead of being read
        # whole into client memory on execute() (the default Cursor is buffered).
        async with conn.cursor(aiomysql.SSCursor) as cursor:
            await cursor.execute(sql)
            if cursor.description:
                columns = [d[0] for d in cursor.description]

                async def _source():
                    while True:
                        chunk = await cursor.fetchmany(SQL_FETCH_BATCH)
                        if not chunk:
                            break
                        for r in chunk:
                            yield r

                _src = _source()
                try:
                    rows, truncated = await _bounded_collect(_src, max_rows, max_bytes)
                finally:
                    await _src.aclose()
                return _format_sql_result(columns, rows, truncated, max_rows)
            else:
                await conn.commit()
                return f"✅ Statement executed successfully. Rows affected: {cursor.rowcount}"
    finally:
        conn.close()


async def _sql_execute_postgres(connection_string: str, sql: str, max_rows: int, max_bytes: int) -> str:
    """Execute SQL on PostgreSQL. SELECT-like statements stream via an in-transaction cursor."""
    import asyncpg

    dsn = connection_string
    if dsn.startswith("postgresql://"):
        dsn = "postgres://" + dsn[len("postgresql://") :]

    conn = await asyncpg.connect(dsn)
    try:
        stmt = await conn.prepare(sql)
        if stmt.get_attributes():
            columns = [attr.name for attr in stmt.get_attributes()]
            # asyncpg cursors must run inside a transaction.
            async with conn.transaction():
                # Reuse the prepared stmt (avoids a second PREPARE round-trip); the
                # portal it opens is closed automatically when the transaction exits.
                cur = await stmt.cursor()

                async def _source():
                    while True:
                        chunk = await cur.fetch(SQL_FETCH_BATCH)
                        if not chunk:
                            break
                        for r in chunk:
                            yield tuple(r.values())

                _src = _source()
                try:
                    rows, truncated = await _bounded_collect(_src, max_rows, max_bytes)
                finally:
                    await _src.aclose()
            return _format_sql_result(columns, rows, truncated, max_rows)
        else:
            result = await conn.execute(sql)
            return f"✅ Statement executed successfully. {result}"
    finally:
        await conn.close()


def _format_sql_result(columns: list, rows: list, truncated: bool, max_rows: int) -> str:
    """Format query rows as a text table, bounded by SQL_DISPLAY_CHAR_BUDGET.

    Shows as many rows as fit the display char budget (so the result stays well
    under the global tool-output budget and is never force-persisted), and
    appends explicit truncation + aggregation guidance when the fetch hit a cap.
    """
    if not rows:
        return f"Query returned 0 rows.\nColumns: {', '.join(columns)}"

    str_rows = [[str(v) if v is not None else "NULL" for v in row] for row in rows]
    widths = [min(max(len(c), max((len(r[i]) for r in str_rows), default=0)), 50) for i, c in enumerate(columns)]
    header = " | ".join(c.ljust(w) for c, w in zip(columns, widths))
    separator = "-+-".join("-" * w for w in widths)
    lines = [header, separator]
    char_count = len(header) + len(separator) + 2
    shown = 0
    for row in str_rows:
        line = " | ".join(str(v)[:50].ljust(w) for v, w in zip(row, widths))
        if char_count + len(line) + 1 > SQL_DISPLAY_CHAR_BUDGET:
            break
        lines.append(line)
        char_count += len(line) + 1
        shown += 1

    result = "\n".join(lines)
    if shown < len(rows):
        result += f"\n(已 fetch {len(rows)} 行,展示前 {shown} 行)"
    else:
        result += f"\n({len(rows)} rows)"

    if truncated:
        result += (
            f"\n\n⚠️ 已达返回上限:返回 {len(rows)} 行,可能还有更多,结果不完整。\n"
            f"请勿基于这些行做整体统计/计数/求和 —— 数据不全。\n"
            f"建议在 SQL 内聚合,例如:\n"
            f"  SELECT col, COUNT(*), SUM(x) FROM t GROUP BY col;\n"
            f"或加 WHERE/LIMIT 缩小范围;确需更多明细可传 max_rows(上限 {HARD_SQL_MAX_ROWS},当前 {max_rows})。"
        )
    return result
