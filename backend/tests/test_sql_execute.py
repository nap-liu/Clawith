"""Unit + sqlite-integration tests for sql_execute memory-safe scaling."""
from app.services.agent_tools import (
    _clamp_sql_max_rows,
    DEFAULT_SQL_MAX_ROWS,
    HARD_SQL_MAX_ROWS,
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
