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

from app.services.agent_tools import _render_xlsx_row


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
