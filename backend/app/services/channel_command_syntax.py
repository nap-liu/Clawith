"""Syntax and display helpers for external channel commands."""

COMMANDS = {
    "/new",
    "/reset",
    "/help",
    "/stop",
    "/status",
    "/thinking",
    "/think",
    "/scene",
    "/model",
}


def _parse_command(text: str) -> tuple[str, str | None]:
    raw_parts = text.strip().split(maxsplit=1)
    if raw_parts and raw_parts[0].lower() == "/model":
        return "/model", raw_parts[1].strip() if len(raw_parts) == 2 else None
    parts = text.strip().lower().split()
    if not parts:
        return "", None
    command = parts[0]
    arg = parts[1] if len(parts) == 2 else None
    if len(parts) > 2:
        return command, "__invalid__"
    return command, arg


def _help_message() -> str:
    return (
        "可用指令：\n"
        "/new 或 /reset：开启新对话，清除当前上下文\n"
        "/thinking on：开启数字员工的 IM 思考输出\n"
        "/thinking off：关闭数字员工的 IM 思考输出\n"
        "/thinking status：查看数字员工的 IM 思考输出状态（/think 可作为简写）\n"
        "/scene <场景标识>：从下一条消息起激活指定场景\n"
        "/scene status：查看当前场景；/scene off：退出当前场景\n"
        "/model list：查看可用模型；/model <模型名>：切换当前会话模型\n"
        "/model use <模型名>：切换名称为 list、status、default 的模型\n"
        "/model status：查看当前模型；/model default：恢复默认模型\n"
        "/stop：停止当前这轮正在执行的工作\n"
        "/status：查看当前数字员工和会话状态\n"
        "/help：查看帮助"
    )


def _thinking_status_label(enabled: bool) -> str:
    return "开启" if enabled else "关闭"


def _format_token_count(value: int) -> str:
    count = max(0, int(value or 0))
    for divisor, suffix in (
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    ):
        if count >= divisor:
            compact = count / divisor
            precision = 0 if compact >= 100 else 1
            formatted = f"{compact:.{precision}f}"
            if "." in formatted:
                formatted = formatted.rstrip("0").rstrip(".")
            return formatted + suffix
    return str(count)
