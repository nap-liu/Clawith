"""Public scope-predicate entry on session_query.

The MCP channel (always a human viewer) and future REST consume this instead of
importing the private _*_where helpers across modules.
asyncio_mode = "auto" (pyproject); this module is pure/sync — no async fixtures.
"""

from __future__ import annotations

import uuid

from app.services import session_query as sq


def test_scope_predicate_for_all_returns_predicate():
    p = sq.scope_predicate_for(sq.SCOPE_ALL, uuid.uuid4(), uuid.uuid4())
    assert p is not None


def test_scope_predicate_for_own_returns_predicate():
    p = sq.scope_predicate_for(sq.SCOPE_OWN, uuid.uuid4(), uuid.uuid4())
    assert p is not None


def test_scope_predicate_for_deny_returns_none():
    assert sq.scope_predicate_for(sq.SCOPE_DENY, uuid.uuid4(), uuid.uuid4()) is None


def test_scope_predicate_for_all_matches_private_all_sessions_where():
    """SCOPE_ALL must delegate to the same predicate as _all_sessions_where."""
    aid = uuid.uuid4()
    expected = sq._all_sessions_where(aid)
    got = sq.scope_predicate_for(sq.SCOPE_ALL, aid, uuid.uuid4())
    # Compiled SQL text equality is a robust structural check for SQLAlchemy clauses.
    assert str(got) == str(expected)
