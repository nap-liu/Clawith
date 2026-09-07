import json
from app.services.llm.confirmation_tool import (
    find_request_confirmation_call,
)


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
    assert call.force_confirmation is True


def test_non_blocking_confirmation_is_explicit():
    call = find_request_confirmation_call([_tc("request_confirmation", {
        "title": "是否继续",
        "summary": "可以忽略",
        "force_confirmation": False,
    })])
    assert call is not None and call.valid is True
    assert call.force_confirmation is False


def test_force_confirmation_must_be_boolean():
    call = find_request_confirmation_call([_tc("request_confirmation", {
        "title": "是否继续",
        "summary": "必须明确",
        "force_confirmation": "false",
    })])
    assert call is not None and call.valid is False
    assert "boolean" in call.error


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
