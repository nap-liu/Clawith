"""Unit + sqlite-integration tests for sql_execute memory-safe scaling."""
from app.services.agent_tools import (
    _clamp_sql_max_rows,
    DEFAULT_SQL_MAX_ROWS,
    HARD_SQL_MAX_ROWS,
    SQL_DISPLAY_CHAR_BUDGET,
)


def test_clamp_default_when_missing_or_invalid():
    assert _clamp_sql_max_rows(None) == DEFAULT_SQL_MAX_ROWS
    assert _clamp_sql_max_rows("abc") == DEFAULT_SQL_MAX_ROWS
    assert _clamp_sql_max_rows("") == DEFAULT_SQL_MAX_ROWS


def test_clamp_respects_hard_ceiling():
    assert _clamp_sql_max_rows(999_999) == HARD_SQL_MAX_ROWS
    assert _clamp_sql_max_rows(HARD_SQL_MAX_ROWS + 1) == HARD_SQL_MAX_ROWS


def test_clamp_floor_is_one():
    assert _clamp_sql_max_rows(0) == 1
    assert _clamp_sql_max_rows(-5) == 1


def test_clamp_passes_valid_value():
    assert _clamp_sql_max_rows(2000) == 2000
    assert _clamp_sql_max_rows("3000") == 3000


from app.services.agent_tools import _bounded_collect


async def _gen(rows):
    for r in rows:
        yield r


async def test_bounded_collect_under_both_limits_no_truncation():
    rows, truncated = await _bounded_collect(_gen([(1, "a"), (2, "b")]), max_rows=10, max_bytes=1_000_000)
    assert rows == [(1, "a"), (2, "b")]
    assert truncated is False


async def test_bounded_collect_row_limit_triggers_truncation():
    rows, truncated = await _bounded_collect(_gen([(1,), (2,), (3,)]), max_rows=2, max_bytes=1_000_000)
    assert rows == [(1,), (2,)]
    assert truncated is True


async def test_bounded_collect_exact_row_count_no_truncation():
    rows, truncated = await _bounded_collect(_gen([(1,), (2,)]), max_rows=2, max_bytes=1_000_000)
    assert rows == [(1,), (2,)]
    assert truncated is False


async def test_bounded_collect_byte_budget_triggers_truncation():
    rows, truncated = await _bounded_collect(_gen([("xxxxx",), ("yyyyy",), ("zzzzz",)]), max_rows=100, max_bytes=8)
    assert rows == [("xxxxx",)]
    assert truncated is True


async def test_bounded_collect_always_returns_at_least_one_row():
    rows, truncated = await _bounded_collect(_gen([("x" * 50,)]), max_rows=100, max_bytes=8)
    assert rows == [("x" * 50,)]
    assert truncated is False


from app.services.agent_tools import _format_sql_result


def test_format_empty_rows():
    out = _format_sql_result(["id", "name"], [], truncated=False, max_rows=5000)
    assert "0 rows" in out
    assert "id, name" in out


def test_format_normal_rows_no_truncation():
    out = _format_sql_result(["id"], [(1,), (2,)], truncated=False, max_rows=5000)
    assert "id" in out
    assert "(2 rows)" in out
    assert "硬上限" not in out


def test_format_truncated_appends_aggregation_guidance():
    out = _format_sql_result(["id"], [(1,), (2,)], truncated=True, max_rows=5000)
    assert "硬上限" in out
    assert "GROUP BY" in out
    assert "max_rows" in out
    assert "50000" in out


def test_format_display_budget_limits_shown_rows():
    rows = [("x" * 100,) for _ in range(2000)]
    out = _format_sql_result(["c"], rows, truncated=False, max_rows=5000)
    assert len(out) <= SQL_DISPLAY_CHAR_BUDGET + 200
    assert "展示前" in out
