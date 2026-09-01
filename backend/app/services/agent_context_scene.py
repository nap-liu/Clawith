"""Scene quick-action rendering for agent context."""

SCENE_QUICK_ACTION_CONTEXT_MAX_CHARS = 24_000
SCENE_QUICK_ACTION_CONTEXT_TRUNCATION_NOTICE = (
    "[Additional AI-visible quick actions were omitted because the scene quick-action "
    "context reached its 24000-character limit.]"
)


def _markdown_table_cell(value: object, *, max_chars: int | None = None) -> str:
    escaped = (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
        .replace("\r", "<br>")
    )
    if max_chars is None or len(escaped) <= max_chars:
        return escaped
    prefix = escaped[: max_chars - 1]
    if (len(prefix) - len(prefix.rstrip("\\"))) % 2 == 1:
        prefix = prefix[:-1]
    return prefix + "…"


def _render_scene_quick_actions(actions: list[dict]) -> str:
    """Render AI-visible actions as a compact, bounded Markdown table."""
    header = "| Title | Type | Content | AI Context |\n|---|---|---|---|"
    rows: list[str] = []
    used_chars = len(header)
    content_budget = SCENE_QUICK_ACTION_CONTEXT_MAX_CHARS - len(SCENE_QUICK_ACTION_CONTEXT_TRUNCATION_NOTICE) - 2
    omitted = False
    for action in actions:
        if not isinstance(action, dict) or not action.get("ai_visible", action.get("enabled", True)):
            continue
        action_type = str(action.get("type") or "").strip()
        if action_type not in {"send_message", "open_uri"}:
            continue
        content = action.get("message") if action_type == "send_message" else action.get("uri")
        row = (
            "| "
            + " | ".join(
                (
                    _markdown_table_cell(
                        action.get("label") or action.get("id") or "Quick action",
                        max_chars=160,
                    ),
                    action_type,
                    _markdown_table_cell(content, max_chars=12_000),
                    _markdown_table_cell(
                        str(action.get("ai_context") or "").strip(),
                        max_chars=4_000,
                    ),
                )
            )
            + " |"
        )
        if used_chars + 1 + len(row) > content_budget:
            omitted = True
            break
        rows.append(row)
        used_chars += 1 + len(row)
    if not rows and not omitted:
        return ""
    rendered = "\n".join((header, *rows))
    if omitted:
        rendered += "\n\n" + SCENE_QUICK_ACTION_CONTEXT_TRUNCATION_NOTICE
    return rendered
