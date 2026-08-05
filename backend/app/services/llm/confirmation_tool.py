import json
from dataclasses import dataclass
from typing import Any

REQUEST_CONFIRMATION_TOOL_NAME = "request_confirmation"

REQUEST_CONFIRMATION_TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REQUEST_CONFIRMATION_TOOL_NAME,
        "description": (
            "向用户展示确认卡片并等待明确选择。适用于后续处理需要用户确认、授权、接受风险或选择处理分支的场景，"
            "尤其是不可逆或有外部副作用的操作；普通信息展示或不需要等待用户选择时不要使用。"
            "如果需要在卡片前提供说明或答复，必须在同一次响应的 content 字段中输出完整正文；"
            "工具参数仅描述卡片本身。content 与卡片相互独立，不得将独立正文放入 summary 或其他卡片参数。"
            "调用本工具后，当前回合结束并等待用户处理卡片。用户点击按钮后，后续回合会返回按钮的文字和值，"
            "由你根据用户选择决定后续处理。action 仅用于展示拟执行动作，本工具不会代替你执行该动作。"
            "卡片有效期为 24 小时，过期后作废。force_confirmation=true 时，用户必须点击卡片按钮才能继续；"
            "force_confirmation=false 时，用户也可以发送新消息跳过本次确认，届时会明确返回本次操作未获得确认。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "卡片标题，简短点明需要用户确认或选择的事项。",
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "卡片的 Markdown 正文：说明做什么、关键参数、影响范围、是否可逆，"
                        "讲清楚让用户能够判断。仅承载卡片自身的确认信息，不承载 content 中的独立正文。"
                    ),
                },
                "action": {
                    "type": "object",
                    "description": (
                        "可选的动作预览，用于告知用户确认后拟执行的操作。仅用于展示，"
                        "本工具不会自动执行；获得确认后仍需由你执行实际操作。"
                    ),
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": "拟执行的工具名称，必须是当前已启用的工具。",
                        },
                        "args": {
                            "type": "object",
                            "description": "拟执行工具的参数，仅用于动作预览。",
                        },
                    },
                    "required": ["tool"],
                },
                "buttons": {
                    "type": "array",
                    "description": (
                        "卡片按钮列表。不传时默认使用“确认”和“取消”；也可以按当前选择动态定义其他按钮。"
                        "用户点击后，按钮的 text 和 value 会原样返回，由你据此处理。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "按钮上显示的文字。"},
                            "value": {
                                "type": "string",
                                "description": "按钮对应的返回值，用户点击后原样返回，用于识别用户选择。",
                            },
                            "color": {
                                "type": "string",
                                "enum": ["blue", "red", "gray"],
                                "description": (
                                    "按钮颜色，只能是 blue、red 或 gray；blue 表示主要操作，"
                                    "red 表示危险操作，gray 表示次要操作，默认 blue。"
                                ),
                            },
                        },
                        "required": ["text", "value"],
                    },
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "确认事项的风险等级，仅影响卡片的视觉提示，不改变处理逻辑，默认 medium。",
                },
                "force_confirmation": {
                    "type": "boolean",
                    "description": (
                        "是否必须通过卡片按钮完成选择，默认 true。true 表示卡片处理前不接受普通输入；"
                        "false 表示用户可以发送新消息跳过本次确认，并返回本次操作未获得确认。"
                    ),
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
    # NOT category "system": system tools are protocol-level, always-on, and the UI rejects
    # disabling them (see api/tools.py). The confirmation card is an OPT-IN feature the user
    # turns on per agent, so it lives in a normal, toggleable category.
    "category": "communication",
    # NB: tools.icon is varchar(10) — keep the name short (lucide "shield"; the
    # 12-char "shield-check" overflows the column and the seed INSERT fails).
    "icon": "shield",
    # Company agents receive the confirmation capability by default so they can
    # ask a human before carrying out irreversible or externally visible work.
    "is_default": True,
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
                # Configurable at BOTH levels: a company-wide default (the same DingTalk card
                # template can be reused across apps) and a per-agent override. The existing
                # _get_tool_config priority (agent → tenant → tool default) handles both, so no
                # agent_only restriction.
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
    force_confirmation: bool = True


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
        force_confirmation = args.get("force_confirmation")
        if force_confirmation is None:
            force_confirmation = True
        if not isinstance(force_confirmation, bool):
            return ConfirmationCall(
                False,
                title,
                summary,
                action,
                risk if risk in ("low", "medium", "high") else "medium",
                "force_confirmation 必须是 boolean",
                cid,
                buttons,
            )
        return ConfirmationCall(
            True,
            title,
            summary,
            action,
            risk if risk in ("low", "medium", "high") else "medium",
            None,
            cid,
            buttons,
            force_confirmation,
        )
    return None
