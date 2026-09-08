"""Global pytest safety gates.

Database-backed tests in this repository create and delete real rows. Refuse to
start pytest unless the configured application database is explicitly disposable.
"""

import os

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def _is_safe_test_database_url(raw_url: str) -> bool:
    try:
        url = make_url(raw_url)
    except (ArgumentError, TypeError, ValueError):
        return False

    database = str(url.database or "")
    if url.get_backend_name() == "sqlite":
        return database == ":memory:" or (
            database
            and (
                database.rsplit("/", 1)[-1].startswith("test_")
                or database.rsplit("/", 1)[-1].endswith("_test.db")
            )
        )

    normalized = database.lower()
    return (
        normalized == "test"
        or normalized.startswith("test_")
        or normalized.endswith("_test")
    )


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "bootstrap: verifies real schema entry points using disposable PostgreSQL databases",
    )


def pytest_sessionstart(session) -> None:
    del session
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://clawith:clawith@localhost:5432/clawith",
    )
    if _is_safe_test_database_url(database_url):
        return
    raise pytest.UsageError(
        "Refusing to run database-destructive tests against a non-test database. "
        "Set DATABASE_URL to an isolated database named test, test_*, or *_test "
        "(or use an in-memory/test-named SQLite database)."
    )


@pytest.fixture(autouse=True)
def _reset_storage_backend_between_tests():
    """Keep one test's temporary storage configuration out of later tests."""

    from app.services.storage_runtime import facade

    facade._storage_backend = None
    yield
    facade._storage_backend = None
