"""Placeholder renderer: strict whitelist, no code-path fallthrough."""

from __future__ import annotations

import pytest

from app.services.cli_tools.placeholders import (
    InvalidPlaceholderError,
    PlaceholderContext,
    render,
)


def _ctx() -> PlaceholderContext:
    return PlaceholderContext(
        user={"id": "u1", "phone": "13800000000", "email": "u@example.com"},
        agent={"id": "a1"},
        tenant={"id": "t1"},
        params={"action": "ping", "n": "3"},
    )


def test_render_substitutes_whitelisted_placeholders():
    assert render("--user={user.id}", _ctx()) == "--user=u1"
    assert render("{params.action}", _ctx()) == "ping"
    assert render("{agent.id}:{tenant.id}", _ctx()) == "a1:t1"


def test_render_leaves_bare_text_alone():
    assert render("literal", _ctx()) == "literal"
    assert render("brace{{escape}}", _ctx()) == "brace{escape}"


def test_render_rejects_unknown_placeholder():
    with pytest.raises(InvalidPlaceholderError, match="user.secret"):
        render("{user.secret}", _ctx())


def test_render_rejects_non_whitelisted_root():
    with pytest.raises(InvalidPlaceholderError, match="system.path"):
        render("{system.path}", _ctx())


def test_render_missing_param_is_error():
    with pytest.raises(InvalidPlaceholderError, match="params.missing"):
        render("{params.missing}", _ctx())
