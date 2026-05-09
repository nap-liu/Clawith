from app.services.placeholder_engine import (
    PlaceholderContext,
    PROMPT_SAFE_ROOTS,
    ALL_ROOTS,
)


def test_prompt_safe_excludes_user_and_params():
    assert "user" not in PROMPT_SAFE_ROOTS
    assert "params" not in PROMPT_SAFE_ROOTS
    assert PROMPT_SAFE_ROOTS == {"agent", "tenant", "session", "channel"}


def test_all_roots_superset():
    assert PROMPT_SAFE_ROOTS <= ALL_ROOTS
    assert ALL_ROOTS == {"user", "agent", "tenant", "session", "channel", "params"}


def test_lookup_returns_none_for_unknown_root():
    ctx = PlaceholderContext(user={"id": "u1"})
    assert ctx.lookup("user", "id") == "u1"
    assert ctx.lookup("user", "missing") is None
    assert ctx.lookup("nonexistent", "x") is None
