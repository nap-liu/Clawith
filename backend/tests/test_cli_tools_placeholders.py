"""Placeholder resolver: project's $var.field convention."""

from __future__ import annotations

from app.services.cli_tools.placeholders import PlaceholderContext, resolve


def _ctx() -> PlaceholderContext:
    return PlaceholderContext(
        user={"id": "u1", "phone": "13800000000", "email": "u@example.com"},
        agent={"id": "a1"},
        tenant={"id": "t1"},
        params={"action": "ping"},
    )


def test_resolve_user_fields():
    ctx = _ctx()
    assert resolve("$user.id", ctx) == "u1"
    assert resolve("$user.phone", ctx) == "13800000000"
    assert resolve("$user.email", ctx) == "u@example.com"


def test_resolve_agent_tenant():
    ctx = _ctx()
    assert resolve("$agent.id", ctx) == "a1"
    assert resolve("$tenant.id", ctx) == "t1"


def test_resolve_params():
    ctx = _ctx()
    assert resolve("$params.action", ctx) == "ping"


def test_unknown_token_is_returned_verbatim():
    # Convention: if the token doesn't resolve, the original string is
    # used as-is. This matches the pre-M2 `_resolve_placeholder`.
    ctx = _ctx()
    assert resolve("$user.unknown", ctx) == "$user.unknown"
    assert resolve("$something.else", ctx) == "$something.else"
    assert resolve("not-a-placeholder", ctx) == "not-a-placeholder"
    assert resolve("", ctx) == ""


def test_non_string_passthrough():
    ctx = _ctx()
    # resolve() is type-annotated for str but should tolerate other input
    assert resolve(42, ctx) == 42  # type: ignore[arg-type]
