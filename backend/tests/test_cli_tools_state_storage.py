"""StateStorage: per-(tenant, tool, user) persistent HOME directory."""

from __future__ import annotations

import os
import uuid

import pytest

from app.services.cli_tools.state_storage import StateStorage


@pytest.fixture
def state_root(tmp_path):
    root = tmp_path / "cli_state"
    root.mkdir()
    return root


def test_ensure_home_creates_nested_tree(state_root):
    s = StateStorage(root=state_root)
    tenant = uuid.uuid4()
    tool = uuid.uuid4()
    user = uuid.uuid4()

    leaf = s.ensure_home(tenant_id=tenant, tool_id=tool, user_id=user)

    assert leaf == (state_root / str(tenant) / str(tool) / str(user)).resolve()
    assert leaf.is_dir()
    # Every intermediate dir exists too.
    assert (state_root / str(tenant)).is_dir()
    assert (state_root / str(tenant) / str(tool)).is_dir()


def test_ensure_home_is_idempotent(state_root):
    s = StateStorage(root=state_root)
    tool = uuid.uuid4()
    user = uuid.uuid4()

    leaf1 = s.ensure_home(tenant_id=None, tool_id=tool, user_id=user)
    # Drop a file so we can tell the dir isn't recreated.
    marker = leaf1 / "login-token.json"
    marker.write_text("persisted")

    leaf2 = s.ensure_home(tenant_id=None, tool_id=tool, user_id=user)
    assert leaf2 == leaf1
    assert marker.read_text() == "persisted"


def test_ensure_home_uses_global_segment_for_missing_tenant(state_root):
    s = StateStorage(root=state_root)
    leaf = s.ensure_home(tenant_id=None, tool_id=uuid.uuid4(), user_id=uuid.uuid4())
    # `_global` keeps tenantless tools out of the per-tenant namespace.
    assert leaf.parent.parent.name == "_global"


def test_ensure_home_isolates_different_users(state_root):
    """Two users of the same tool must get distinct directories.

    Security: shared HOME would let User A see User B's login token.
    """
    s = StateStorage(root=state_root)
    tenant = uuid.uuid4()
    tool = uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()

    home_a = s.ensure_home(tenant_id=tenant, tool_id=tool, user_id=user_a)
    home_b = s.ensure_home(tenant_id=tenant, tool_id=tool, user_id=user_b)

    assert home_a != home_b
    (home_a / "secret.txt").write_text("a's secret")
    assert not (home_b / "secret.txt").exists()


def test_ensure_home_isolates_different_tools(state_root):
    """Same user using two tools: caches must not leak between tools."""
    s = StateStorage(root=state_root)
    tenant = uuid.uuid4()
    user = uuid.uuid4()
    tool_a, tool_b = uuid.uuid4(), uuid.uuid4()

    home_a = s.ensure_home(tenant_id=tenant, tool_id=tool_a, user_id=user)
    home_b = s.ensure_home(tenant_id=tenant, tool_id=tool_b, user_id=user)

    assert home_a != home_b


def test_ensure_home_respects_env_root(state_root, monkeypatch):
    monkeypatch.setenv("CLI_STATE_ROOT", str(state_root))
    s = StateStorage()  # no explicit root → read from env
    leaf = s.ensure_home(tenant_id=None, tool_id=uuid.uuid4(), user_id=uuid.uuid4())
    assert str(leaf).startswith(str(state_root))


def test_ensure_home_chowns_leaf_to_sandbox_gem(tmp_path, monkeypatch):
    """Leaf must be owned by uid/gid 1000 (aio-sandbox 'gem') so the
    sandbox shell can write token caches inside it."""
    from app.services.cli_tools import state_storage as mod

    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(mod.os, "chown", lambda p, u, g: calls.append((str(p), u, g)))

    store = mod.StateStorage(root=tmp_path)
    leaf = store.ensure_home(tenant_id="t1", tool_id="tool1", user_id="u1")

    assert leaf == (tmp_path / "t1" / "tool1" / "u1").resolve()
    assert (str(leaf), 1000, 1000) in calls
