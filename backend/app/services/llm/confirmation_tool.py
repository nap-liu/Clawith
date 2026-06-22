import json
from dataclasses import dataclass
from typing import Any

REQUEST_CONFIRMATION_TOOL_NAME = "request_confirmation"

REQUEST_CONFIRMATION_TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REQUEST_CONFIRMATION_TOOL_NAME,
        "description": (
            "在执行不可逆或有对外副作用的业务操作前(例如创建单据、写入外部系统、对外发送消息),"
            "用本工具向用户出示一张确认卡片并取得用户手动确认。把要执行的动作作为 action 挟带传入;"
            "用户点击确认后平台才会执行该动作。调用本工具即把控制权交给用户并结束当前回合——"
            "调用后不要再自行执行该动作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "卡片标题,简短点明要做的事,如 '创建采购单 PO-2026-0312'",
                },
                "summary": {
                    "type": "string",
                    "description": "markdown 正文:做什么 / 关键参数 / 影响范围 / 是否可逆,讲清楚让用户能判断",
                },
                "action": {
                    "type": "object",
                    "description": "用户确认后平台代为执行的动作。省略则为纯确认(不执行任何动作,只回收用户的是/否)。",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": "要执行的工具名(必须是你当前已启用的工具)",
                        },
                        "args": {"type": "object", "description": "该工具的参数"},
                    },
                    "required": ["tool"],
                },
                "confirm_label": {
                    "type": "string",
                    "description": "确认按钮文案,默认 '确认'",
                },
                "cancel_label": {
                    "type": "string",
                    "description": "取消按钮文案,默认 '取消'",
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "风险等级,仅影响卡片配色,默认 medium",
                },
            },
            "required": ["title", "summary"],
        },
    },
}

REQUEST_CONFIRMATION_TOOL_SEED: dict[str, Any] = {
    "name": REQUEST_CONFIRMATION_TOOL_NAME,
    "display_name": "Request Confirmation",
    "description": REQUEST_CONFIRMATION_TOOL_DEFINITION["function"]["description"],
    "category": "system",
    "icon": "shield-check",
    "is_default": True,  # 默认下发给新 agent;存量 agent 走 Task 8 的 fan-out
    "parameters_schema": REQUEST_CONFIRMATION_TOOL_DEFINITION["function"]["parameters"],
    "config": {},
    "config_schema": {},
}


@dataclass
class ConfirmationCall:
    valid: bool
    title: str
    summary: str
    action: dict | None
    risk_level: str
    error: str | None = None


def _parse_args(tc: dict) -> dict:
    raw = (tc.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def find_request_confirmation_call(tool_calls: list[dict] | None) -> ConfirmationCall | None:
    for tc in tool_calls or []:
        if ((tc.get("function") or {}).get("name") or "") != REQUEST_CONFIRMATION_TOOL_NAME:
            continue
        args = _parse_args(tc)
        title = (args.get("title") or "").strip()
        summary = (args.get("summary") or "").strip()
        action = args.get("action")
        risk = args.get("risk_level") or "medium"
        if not title or not summary:
            return ConfirmationCall(
                False, title, summary, None, risk, "request_confirmation 需要非空 title 和 summary"
            )
        if action is not None:
            if not isinstance(action, dict) or not (action.get("tool") or "").strip():
                return ConfirmationCall(False, title, summary, None, risk, "action 必须是 {tool, args} 且 tool 非空")
            if action.get("tool") == REQUEST_CONFIRMATION_TOOL_NAME:
                return ConfirmationCall(
                    False, title, summary, None, risk, "action.tool 不能是 request_confirmation 自身"
                )
        return ConfirmationCall(True, title, summary, action, risk if risk in ("low", "medium", "high") else "medium")
    return None
