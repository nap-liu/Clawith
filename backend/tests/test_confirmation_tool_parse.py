import json
from app.services.llm.confirmation_tool import (
    REQUEST_CONFIRMATION_TOOL_SEED,
    find_request_confirmation_call,
)
from app.models.tool import Tool


def _tc(name, args):
    return {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}


def test_none_when_absent():
    assert find_request_confirmation_call([_tc("write_file", {"path": "a"})]) is None


def test_valid_with_action():
    call = find_request_confirmation_call([_tc("request_confirmation", {
        "title": "创建采购单", "summary": "金额 ¥48,500",
        "action": {"tool": "sql_execute", "args": {"sql": "INSERT ..."}}})])
    assert call is not None and call.valid is True
    assert call.title == "创建采购单"
    assert call.action["tool"] == "sql_execute"
    assert call.risk_level == "medium"


def test_invalid_missing_title():
    call = find_request_confirmation_call([_tc("request_confirmation", {"summary": "x"})])
    assert call is not None and call.valid is False and call.error


def test_invalid_nested_action():
    call = find_request_confirmation_call([_tc("request_confirmation", {
        "title": "t", "summary": "s", "action": {"tool": "request_confirmation", "args": {}}})])
    assert call.valid is False


def test_invalid_malformed_json_args():
    # Construct a tool call with malformed JSON arguments directly (bypassing json.dumps)
    tc = {"id": "c1", "function": {"name": "request_confirmation", "arguments": '{"title": "t", "summary":'}}
    call = find_request_confirmation_call([tc])
    assert call is not None and call.valid is False
    assert "JSON" in call.error


def test_seed_string_fields_fit_tools_table_columns():
    """The seed is INSERTed verbatim into `tools`; over-long values fail the INSERT
    at startup seeding (icon is varchar(10) — the 12-char 'shield-check' overflowed
    and the tool was never seeded). Validate against the real model column limits."""
    limits = {
        col.name: col.type.length
        for col in Tool.__table__.columns
        if getattr(col.type, "length", None) is not None
    }
    for field in ("name", "display_name", "category", "icon", "source", "type"):
        val = REQUEST_CONFIRMATION_TOOL_SEED.get(field)
        if val is None or field not in limits:
            continue
        assert len(val) <= limits[field], (
            f"seed[{field!r}]={val!r} ({len(val)} chars) exceeds tools.{field} varchar({limits[field]})"
        )
