import pytest
from app.services.placeholder_engine import (
    PlaceholderContext,
    PROMPT_SAFE_ROOTS,
    ALL_ROOTS,
    render,
    DisallowedPlaceholderError,
    UnknownPlaceholderError,
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


def test_render_substitutes_known_token():
    ctx = PlaceholderContext(user={"id": "u123"}, tenant={"id": "t-7"})
    assert render("hi ${user.id}!", ctx) == "hi u123!"
    assert render("${tenant.id}/${user.id}", ctx) == "t-7/u123"


def test_render_no_tokens_returns_input():
    assert render("plain text", PlaceholderContext()) == "plain text"
    assert render("", PlaceholderContext()) == ""


def test_render_rejects_disallowed_root():
    ctx = PlaceholderContext(user={"id": "u1"}, agent={"id": "a1"})
    with pytest.raises(DisallowedPlaceholderError) as ei:
        render("${user.id}", ctx, allowed_roots=PROMPT_SAFE_ROOTS)
    assert "user" in str(ei.value)
    # Allowed root in the same call works
    assert render("${agent.id}", ctx, allowed_roots=PROMPT_SAFE_ROOTS) == "a1"


def test_render_unknown_token_raises_by_default():
    ctx = PlaceholderContext(user={"id": "u1"})
    with pytest.raises(UnknownPlaceholderError):
        render("${user.email}", ctx)


def test_render_unknown_token_keep_literal():
    ctx = PlaceholderContext(user={"id": "u1"})
    assert render("${user.email}/x", ctx, on_unknown="keep_literal") == "${user.email}/x"


def test_render_value_is_stringified():
    ctx = PlaceholderContext(tenant={"id": 42, "items": [1, 2]})
    assert render("${tenant.id}", ctx) == "42"
    # lists become JSON strings (matches CLI convention)
    assert render("${tenant.items}", ctx) == "[1, 2]"
