import json
from app.services.llm.confirmation_tool import find_request_confirmation_call


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
