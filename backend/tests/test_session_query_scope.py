"""Public scope-predicate entry on session_query.

The MCP channel (always a human viewer) and future REST consume this instead of
importing the private _*_where helpers across modules.
asyncio_mode = "auto" (pyproject); this module is pure/sync — no async fixtures.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import session_query as sq


@pytest.mark.parametrize("scope", [sq.SCOPE_DENY, sq.SCOPE_AUTONOMOUS, "unknown"])
def test_scope_predicate_for_unsupported_scope_returns_none(scope):
    assert sq.scope_predicate_for(scope, uuid.uuid4(), uuid.uuid4()) is None
