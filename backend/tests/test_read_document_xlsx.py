"""read_document xlsx rendering must not let trailing empty columns flood the
output with tabs.

Root cause of "Excel 内容被截断了 / 看不到完整 SQL": each row was joined across
up to _READ_DOCUMENT_MAX_COLUMNS (80) cells, so a sparse row (e.g. one value in
a far-right SQL column) emitted dozens of trailing ``\\t`` characters. Those tabs
consumed the max_chars budget and pushed the real SQL past the truncation line.

Fix: trim trailing empty cells per row (inner gaps are kept so column alignment
within the populated range survives).
"""

from __future__ import annotations

from app.services.agent_tools import _render_xlsx_row, _safe_document_cell_text


def test_safe_document_cell_text_does_not_truncate_long_sql():
    # Regression: a single cell holding a long SQL口径 (>>500 chars) used to be
    # chopped at 500 chars with "...[cell truncated]" — unrecoverable for the agent.
    # The tool layer must now return cell content in full; oversize whole-document
    # output is handled non-lossily by the overflow-to-file path instead.
    sql = "SELECT\n" + ",\n".join(f"  SUM(col_{i}) AS metric_{i}" for i in range(200))
    assert len(sql) > 500
    out = _safe_document_cell_text(sql)
    assert out == sql
    assert "[cell truncated]" not in out


def test_safe_document_cell_text_passthrough_short_values():
    assert _safe_document_cell_text(None) == ""
    assert _safe_document_cell_text("当日数据") == "当日数据"
    assert _safe_document_cell_text(42) == "42"


def test_safe_document_cell_text_still_guards_pathological_int():
    # Degenerate huge integers are kept guarded: str() on them is pathologically
    # slow and can raise under CPython's int_max_str_digits.
    assert _safe_document_cell_text(1 << 5000) == "[large integer omitted]"


def test_render_xlsx_row_keeps_full_sql_cell():
    # A populated far-right SQL cell longer than the old 500-char cap must render
    # in full, not get sliced mid-statement.
    sql = "SELECT " + "x," * 400 + "y FROM yk.t"
    row = ("模块", "子模块", sql) + (None,) * 77
    out = _render_xlsx_row(row)
    assert sql in out
    assert "[cell truncated]" not in out


def test_render_xlsx_row_trims_trailing_empty_cells():
    # The real failure mode: openpyxl iter_rows(max_col=80) yields a value in
    # column 1 followed by 79 None cells → 79 wasted trailing tabs.
    row = ("当日数据",) + (None,) * 79
    assert _render_xlsx_row(row) == "当日数据"


def test_render_xlsx_row_preserves_inner_empty_cells():
    # A far-right SQL column with empty cells before it must keep its position,
    # so the value stays attached to its header column on render.
    row = ("模块", None, None, "SELECT 1 FROM yk.t") + (None,) * 76
    assert _render_xlsx_row(row) == "模块\t\t\tSELECT 1 FROM yk.t"


def test_render_xlsx_row_all_empty_is_blank():
    assert _render_xlsx_row((None,) * 80) == ""


def test_render_xlsx_row_no_trailing_tab():
    row = ("a", "b", None, None)
    out = _render_xlsx_row(row)
    assert out == "a\tb"
    assert not out.endswith("\t")
