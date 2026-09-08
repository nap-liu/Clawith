import pytest
from app.services.placeholder_engine import (
    PlaceholderContext,
    PROMPT_SAFE_ROOTS,
    render,
    DisallowedPlaceholderError,
    UnknownPlaceholderError,
)


def test_lookup_returns_none_for_unknown_root():
    ctx = PlaceholderContext(user={"id": "u1"})
    assert ctx.lookup("user", "id") == "u1"
    assert ctx.lookup("user", "missing") is None
    assert ctx.lookup("nonexistent", "x") is None


@pytest.mark.parametrize("root", ["user", "agent", "tenant", "session", "channel", "params"])
def test_render_substitutes_known_token(root):
    ctx = PlaceholderContext(**{root: {"id": "value-123"}})
    template = "hi ${" + root + ".id}!"
    assert render(template, ctx) == "hi value-123!"
    if root not in {"user", "params"}:
        assert render(template, ctx, allowed_roots=PROMPT_SAFE_ROOTS) == "hi value-123!"


def test_render_no_tokens_returns_input():
    assert render("plain text", PlaceholderContext()) == "plain text"
    assert render("", PlaceholderContext()) == ""


@pytest.mark.parametrize("root", ["user", "params"])
def test_render_rejects_disallowed_root(root):
    ctx = PlaceholderContext(**{root: {"id": "private-value"}})
    with pytest.raises(DisallowedPlaceholderError) as ei:
        render("${" + root + ".id}", ctx, allowed_roots=PROMPT_SAFE_ROOTS)
    assert root in str(ei.value)


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


def test_render_dict_handles_nested_strings():
    from app.services.placeholder_engine import render_dict
    ctx = PlaceholderContext(user={"id": "u1"}, agent={"name": "Alice"})
    assert render("${agent.name}/${user.id}", ctx) == "Alice/u1"
    out = render_dict(
        {"X-User": "${user.id}", "X-Agent": "${agent.name}", "static": "x"},
        ctx,
    )
    assert out == {"X-User": "u1", "X-Agent": "Alice", "static": "x"}


def test_render_dict_passes_through_non_strings():
    from app.services.placeholder_engine import render_dict
    out = render_dict({"k": 1, "b": True, "n": None}, PlaceholderContext())
    assert out == {"k": 1, "b": True, "n": None}


def test_detect_used_roots_finds_distinct_roots():
    from app.services.placeholder_engine import detect_used_roots
    text = "X ${user.id} ${agent.name} ${user.email}"
    assert detect_used_roots(text) == {"user", "agent"}
    assert detect_used_roots("none here") == set()
