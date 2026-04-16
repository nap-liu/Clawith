"""Placeholder resolver: project's $var.field convention."""

from __future__ import annotations

from app.services.cli_tools.placeholders import PlaceholderContext, resolve, resolve_args


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


# ─── resolve_args: list expansion for multi-segment CLIs ────────────────


def _ctx_with_list_params() -> PlaceholderContext:
    return PlaceholderContext(
        user={"id": "u1", "phone": "13800000000"},
        agent={"id": "a1"},
        tenant={"id": "t1"},
        params={
            "command": ["report", "list"],
            "action": "ping",
            "flags": ["--env", "dev", "--verbose"],
        },
    )


def test_resolve_args_expands_list_params():
    """`svc report list` via a single $params.command list placeholder."""
    ctx = _ctx_with_list_params()
    assert resolve_args(["$params.command"], ctx) == ["report", "list"]


def test_resolve_args_mixes_scalar_and_list():
    """Scalar placeholders and a list placeholder coexist in one template."""
    ctx = _ctx_with_list_params()
    rendered = resolve_args(
        ["$user.id", "$params.action", "$params.flags", "literal-tail"],
        ctx,
    )
    assert rendered == ["u1", "ping", "--env", "dev", "--verbose", "literal-tail"]


def test_resolve_args_scalar_only_unchanged():
    """Templates without list params behave exactly like the scalar path."""
    ctx = _ctx()
    assert resolve_args(["$user.id", "--flag", "$params.action"], ctx) == [
        "u1",
        "--flag",
        "ping",
    ]


def test_resolve_args_unknown_token_passes_through():
    """Unknown tokens keep the original string so misconfig is visible."""
    ctx = _ctx()
    assert resolve_args(["$params.nope", "fixed"], ctx) == ["$params.nope", "fixed"]


def test_resolve_list_in_env_is_json_dumped():
    """env values must be strings — a list param becomes its JSON dump."""
    ctx = _ctx_with_list_params()
    # Not a great config, but it shouldn't crash and the result is observable.
    assert resolve("$params.command", ctx) == '["report", "list"]'
