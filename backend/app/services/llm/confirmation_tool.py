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
            "用本工具向用户出示一张确认卡片征求许可。调用即把控制权交给用户并结束当前回合。"
            "用户点击卡片按钮后,你会在后续回合被告知他点了哪个按钮(按钮文字),"
            "然后由你自己决定后续——同意就自己去执行该操作,拒绝就据此继续对话。"
            "平台只忠实地把用户的点击带回给你,不会替你执行任何操作。卡片有效期 24 小时,过期作废。"
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
                    "description": "(可选,仅展示用)你打算执行的动作,会显示在卡片上让用户知情。平台不会替你执行;确认后由你自己去做。",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": "要执行的工具名(必须是你当前已启用的工具)",
                        },
                        "args": {"type": "object", "description": "该工具的参数"},
                    },
                    "required": ["tool"],
                },
                "buttons": {
                    "type": "array",
                    "description": (
                        "卡片上的按钮,由你动态定义,数量和行为不限。最常见是两个:确认/取消"
                        "(不传 buttons 就默认用这两个),但你也可以放任意按钮,如「同意」「驳回」"
                        "「稍后再说」「方案A」「方案B」等。用户点击后,平台会把该按钮的 value 和文字"
                        "原样带回给你,由你判断处理。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "按钮显示文字"},
                            "value": {
                                "type": "string",
                                "description": "该按钮的回传值,点击后原样带回给你(自定义,如 confirm/cancel/approve/plan_a)",
                            },
                            "color": {
                                "type": "string",
                                "enum": ["blue", "red", "gray"],
                                "description": "按钮配色,只能是 blue(主/蓝)、red(危险/红)、gray(次要/灰)三选一,默认 blue。",
                            },
                        },
                        "required": ["text", "value"],
                    },
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
    # NB: tools.icon is varchar(10) — keep the name short (lucide "shield"; the
    # 12-char "shield-check" overflows the column and the seed INSERT fails).
    "icon": "shield",
    "is_default": True,  # 默认下发给新 agent;存量 agent 走 Task 8 的 fan-out
    "parameters_schema": REQUEST_CONFIRMATION_TOOL_DEFINITION["function"]["parameters"],
    "config": {},
    # Standard builtin-tool config — the ONLY deployment-specific knob is which DingTalk
    # interactive-card template renders the card. The card's FIELD CONTRACT is fixed and
    # platform-owned (title/summary/action_preview/risk/status/buttons); a configured template
    # must bind exactly those variables (see docs/dingtalk-confirmation-card-template.md). No
    # field-mapping config — the template is a skin, the fields are the fixed skeleton.
    "config_schema": {
        "fields": [
            {
                "key": "card_template_id",
                # label/placeholder/help_text/help_link_label are i18n KEYS — the frontend
                # renders them through t() (zh/en in src/i18n). Plain-string labels from other
                # tools pass through t() unchanged, so this stays consistent.
                "label": "agent.tools.reqConfirm.cardTemplateId",
                "type": "string",
                # Per-AGENT only — the template is registered under each agent's own DingTalk
                # app, so a single company-wide value is meaningless. Hidden from the global
                # tool config; set it on each agent's request_confirmation tool config.
                "agent_only": True,
                "placeholder": "agent.tools.reqConfirm.cardTemplateIdPlaceholder",
                "help_text": "agent.tools.reqConfirm.cardTemplateIdHelp",
                "help_url": "/templates/dingtalk-confirmation-card-template.json",
                "help_link_label": "agent.tools.reqConfirm.downloadTemplate",
                "description": (
                    "用于在钉钉投递确认卡片的互动卡片模板 ID(钉钉开发者后台「卡片平台」创建并发布后获得)。"
                    "该模板必须绑定以下变量,否则卡片无法正确渲染:title(标题)、summary(markdown 正文)、"
                    "action_preview(将执行动作预览)、risk(标题颜色,CSS 颜色名)、status(状态文案)、"
                    "buttons(按钮数组,元素含 text/color/status/action)。字段契约固定、不可配置;"
                    "详见 docs/dingtalk-confirmation-card-template.md。不配则跳过钉钉发卡,web 卡片不受影响。"
                ),
            },
        ]
    },
}


@dataclass(frozen=True)
class ConfirmationCall:
    valid: bool
    title: str
    summary: str
    action: dict | None
    risk_level: str
    error: str | None = None
    call_id: str = ""
    buttons: list | None = None  # agent-defined [{text, value, color}], None → default 确认/取消


def _parse_args(tc: dict) -> dict | None:
    """Parse tool call arguments. Return None if JSON is invalid, {} if absent."""
    raw = (tc.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return None


def find_request_confirmation_call(tool_calls: list[dict] | None) -> ConfirmationCall | None:
    for tc in tool_calls or []:
        if ((tc.get("function") or {}).get("name") or "") != REQUEST_CONFIRMATION_TOOL_NAME:
            continue
        cid = tc.get("id") or ""
        args = _parse_args(tc)
        if args is None:
            return ConfirmationCall(
                False, "", "", None, "medium", "request_confirmation arguments must be valid JSON", cid
            )
        title = (args.get("title") or "").strip()
        summary = (args.get("summary") or "").strip()
        action = args.get("action")
        risk = args.get("risk_level") or "medium"
        if not title or not summary:
            return ConfirmationCall(
                False, title, summary, None, risk, "request_confirmation 需要非空 title 和 summary", cid
            )
        if action is not None:
            if not isinstance(action, dict) or not (action.get("tool") or "").strip():
                return ConfirmationCall(
                    False, title, summary, None, risk, "action 必须是 {tool, args} 且 tool 非空", cid
                )
            if action.get("tool") == REQUEST_CONFIRMATION_TOOL_NAME:
                return ConfirmationCall(
                    False, title, summary, None, risk, "action.tool 不能是 request_confirmation 自身", cid
                )
        # Dynamic, agent-defined buttons (any number / behavior). None → caller defaults
        # to 确认/取消. Sanitise to {text, value, color}.
        raw_buttons = args.get("buttons")
        buttons = None
        if isinstance(raw_buttons, list):
            buttons = [
                {
                    "text": str(b.get("text") or ""),
                    "value": str(b.get("value") or ""),
                    "color": str(b.get("color") or ""),
                }
                for b in raw_buttons
                if isinstance(b, dict) and (b.get("text") or b.get("value"))
            ] or None
        return ConfirmationCall(
            True, title, summary, action, risk if risk in ("low", "medium", "high") else "medium", None, cid, buttons
        )
    return None
