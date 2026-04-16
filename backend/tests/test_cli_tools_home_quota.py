"""StateStorage HOME-quota helpers: usage accounting + clear_home + check_quota."""

from __future__ import annotations

import uuid

import pytest

from app.services.cli_tools.state_storage import StateStorage


@pytest.fixture
def state_root(tmp_path):
    root = tmp_path / "cli_state"
    root.mkdir()
    return root


def test_usage_zero_when_home_missing(state_root):
    """An un-initialised (tool, user) leaf reports 0, not an error.

    The first execute of a persistent_home tool ensures the dir right
    after computing usage, so the "missing dir" case is hot-path.
    """
    s = StateStorage(root=state_root)
    used = s.get_home_usage_bytes(
        tenant_id=uuid.uuid4(),
        tool_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )
    assert used == 0


def test_usage_counts_all_files_recursively(state_root):
    """Sum walks the full tree, not just top-level files.

    Real tool caches nest (e.g. `.config/gh/hosts.yml`,
    `.cache/svc/sessions/<id>`); counting only depth-1 would undercount
    and let a runaway tool slip past the quota.
    """
    s = StateStorage(root=state_root)
    tenant, tool, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    leaf = s.ensure_home(tenant_id=tenant, tool_id=tool, user_id=user)

    (leaf / "top.txt").write_bytes(b"x" * 100)
    sub = leaf / ".cache" / "svc"
    sub.mkdir(parents=True)
    (sub / "session").write_bytes(b"y" * 250)
    (sub / "nested" / "deep").mkdir(parents=True)
    (sub / "nested" / "deep" / "blob").write_bytes(b"z" * 700)

    assert s.get_home_usage_bytes(tenant_id=tenant, tool_id=tool, user_id=user) == 1050


def test_check_quota_true_under_limit(state_root):
    s = StateStorage(root=state_root)
    tenant, tool, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    leaf = s.ensure_home(tenant_id=tenant, tool_id=tool, user_id=user)
    (leaf / "small").write_bytes(b"x" * 1024)  # 1 KiB

    within, current = s.check_quota(tenant, tool, user, limit_mb=1)
    assert within is True
    assert current == 1024


def test_check_quota_false_over_limit(state_root):
    s = StateStorage(root=state_root)
    tenant, tool, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    leaf = s.ensure_home(tenant_id=tenant, tool_id=tool, user_id=user)
    # 2 MiB of payload against a 1 MiB quota.
    (leaf / "big").write_bytes(b"x" * (2 * 1024 * 1024))

    within, current = s.check_quota(tenant, tool, user, limit_mb=1)
    assert within is False
    assert current == 2 * 1024 * 1024


def test_check_quota_zero_always_true(state_root):
    """limit_mb=0 is the opt-out — no disk walk, always within limit."""
    s = StateStorage(root=state_root)
    tenant, tool, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    leaf = s.ensure_home(tenant_id=tenant, tool_id=tool, user_id=user)
    (leaf / "huge").write_bytes(b"x" * (10 * 1024 * 1024))  # 10 MiB

    within, current = s.check_quota(tenant, tool, user, limit_mb=0)
    assert within is True
    # The contract says `(True, 0)` for the short-circuit: the 10 MiB on
    # disk must not be stat'd when the check is disabled.
    assert current == 0


def test_clear_home_removes_everything(state_root):
    """clear_home wipes the leaf including nested trees, returns bytes freed."""
    s = StateStorage(root=state_root)
    tenant, tool, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    leaf = s.ensure_home(tenant_id=tenant, tool_id=tool, user_id=user)

    (leaf / "token").write_bytes(b"abc" * 100)
    nested = leaf / ".cache" / "dir"
    nested.mkdir(parents=True)
    (nested / "blob").write_bytes(b"x" * 500)
    expected_before = 300 + 500

    assert s.get_home_usage_bytes(tenant_id=tenant, tool_id=tool, user_id=user) == expected_before

    freed = s.clear_home(tenant_id=tenant, tool_id=tool, user_id=user)
    assert freed == expected_before
    assert not leaf.exists()
    # Second call is idempotent: nothing left, nothing freed.
    assert s.clear_home(tenant_id=tenant, tool_id=tool, user_id=user) == 0


def test_clear_home_tolerates_missing_dir(state_root):
    """clear_home on an un-initialised leaf must not raise."""
    s = StateStorage(root=state_root)
    freed = s.clear_home(
        tenant_id=uuid.uuid4(),
        tool_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )
    assert freed == 0
