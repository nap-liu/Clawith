from conftest import _is_safe_test_database_url


def test_postgres_test_database_names_are_allowed():
    assert _is_safe_test_database_url(
        "postgresql+asyncpg://user:pass@postgres:5432/clawith_test"
    )
    assert _is_safe_test_database_url(
        "postgresql+asyncpg://user:pass@postgres:5432/test_release"
    )


def test_real_database_names_are_rejected():
    assert not _is_safe_test_database_url(
        "postgresql+asyncpg://clawith:clawith@postgres:5432/clawith"
    )
    assert not _is_safe_test_database_url(
        "postgresql+asyncpg://clawith:secret@prod:5432/production"
    )


def test_only_disposable_sqlite_targets_are_allowed():
    assert _is_safe_test_database_url("sqlite+aiosqlite:///:memory:")
    assert _is_safe_test_database_url("sqlite+aiosqlite:///test_chat.db")
    assert not _is_safe_test_database_url("sqlite+aiosqlite:///clawith.db")
