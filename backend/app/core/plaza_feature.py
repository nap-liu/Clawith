"""Global product switch for the retired Plaza capability."""

PLAZA_ENABLED = False

PLAZA_TOOL_NAMES = frozenset(
    {
        "plaza_get_new_posts",
        "plaza_create_post",
        "plaza_add_comment",
    }
)

PLAZA_NOTIFICATION_TYPES = frozenset({"plaza_comment", "plaza_reply"})
PLAZA_ACTIVITY_TYPES = frozenset({"plaza_post"})


def plaza_enabled() -> bool:
    """Return the canonical Plaza availability for every runtime surface."""
    return PLAZA_ENABLED
