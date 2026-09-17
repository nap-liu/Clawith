"""Syntax and display helpers for external channel commands."""

COMMANDS = {
    "/new",
    "/reset",
    "/help",
    "/commands",
    "/stop",
    "/continue",
    "/status",
    "/thinking",
    "/think",
    "/scene",
    "/model",
    "/reasoning",
}


def _parse_command(text: str) -> tuple[str, str | None]:
    raw_parts = text.strip().split(maxsplit=1)
    if raw_parts and raw_parts[0].lower() in {"/model", "/reasoning"}:
        return raw_parts[0].lower(), raw_parts[1].strip() if len(raw_parts) == 2 else None
    parts = text.strip().lower().split()
    if not parts:
        return "", None
    command = parts[0]
    arg = parts[1] if len(parts) == 2 else None
    if len(parts) > 2:
        return command, "__invalid__"
    return command, arg


def _help_message() -> str:
    from app.services.llm.failure_outcome import render_message

    return render_message("commands.help")


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
