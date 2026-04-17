"""Cascading GC tests for CLI tool binary + per-user state cleanup.

These exercise the storage-layer primitives only (BinaryStorage.delete_tool/
delete_tenant and StateStorage.delete_tool/delete_tenant/delete_user). The
API wiring in delete_cli_tool / delete_company is covered indirectly via the
existing integration suites; here we prove the underlying data operations
are correct and idempotent.

Also see test_cli_tools_gc.py for the periodic-sweep (age-based) behaviour —
the two files intentionally do not overlap.
"""

from __future__ import annotations

import io
import uuid

import pytest

from app.services.cli_tools.state_storage import StateStorage
from app.services.cli_tools.storage import BinaryStorage


_SHEBANG = b"#!/bin/sh\necho hi\n"


def _seed_user_home(storage: StateStorage, tenant, tool, user, content: bytes = b"token") -> None:
    home = storage.ensure_home(tenant_id=tenant, tool_id=tool, user_id=user)
    (home / "login.json").write_bytes(content)


@pytest.mark.asyncio
async def test_delete_tool_removes_binary_and_state(tmp_path):
    """Tool-scoped cascade: binary subtree + state subtree both gone."""
    bin_root = tmp_path / "bin"
    state_root = tmp_path / "state"
    bin_root.mkdir()
    state_root.mkdir()

    binary = BinaryStorage(root=bin_root)
    state = StateStorage(root=state_root)

    tenant = uuid.uuid4()
    tool = uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()

    # Seed one binary blob for the tool
    sha, size = await binary.write(
        tenant_key=str(tenant),
        tool_id=str(tool),
        stream=io.BytesIO(_SHEBANG),
        max_bytes=1_000_000,
    )
    assert binary.resolve(str(tenant), str(tool), sha).exists()

    # Seed per-user state for two users under this (tenant, tool)
    _seed_user_home(state, tenant, tool, user_a, b"a-token")
    _seed_user_home(state, tenant, tool, user_b, b"b-token")
    assert (state_root / str(tenant) / str(tool) / str(user_a) / "login.json").exists()

    # Cascade delete
    bin_freed = binary.delete_tool(str(tenant), str(tool))
    state_freed = state.delete_tool(tenant, tool)

    # Binary subtree gone, reported bytes match original
    assert bin_freed >= size
    assert not (bin_root / str(tenant) / str(tool)).exists()

    # State subtree gone (both users), reported bytes > 0
    assert state_freed >= len(b"a-token") + len(b"b-token")
    assert not (state_root / str(tenant) / str(tool)).exists()


@pytest.mark.asyncio
async def test_delete_tool_tolerates_missing_dirs(tmp_path):
    """Deleting a never-used tool must not raise and must return 0 bytes."""
    bin_root = tmp_path / "bin"
    state_root = tmp_path / "state"
    bin_root.mkdir()
    state_root.mkdir()

    binary = BinaryStorage(root=bin_root)
    state = StateStorage(root=state_root)

    # Neither subtree exists for this tenant/tool.
    assert binary.delete_tool("tenant-x", "tool-x") == 0
    assert state.delete_tool("tenant-x", "tool-x") == 0
    # delete_user on an empty root also safe.
    assert state.delete_user(uuid.uuid4()) == 0
    # delete_tenant safe too.
    assert binary.delete_tenant("tenant-x") == 0
    assert state.delete_tenant("tenant-x") == 0


def test_delete_user_only_affects_that_user(tmp_path):
    """Removing user A's state must not touch user B's state in the same tool."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    state = StateStorage(root=state_root)

    tenant = uuid.uuid4()
    tool = uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()

    _seed_user_home(state, tenant, tool, user_a, b"a-secret")
    _seed_user_home(state, tenant, tool, user_b, b"b-secret")

    freed = state.delete_user(user_a)

    # A's bytes reported; A's dir gone; B untouched.
    assert freed >= len(b"a-secret")
    assert not (state_root / str(tenant) / str(tool) / str(user_a)).exists()
    assert (state_root / str(tenant) / str(tool) / str(user_b) / "login.json").read_bytes() == b"b-secret"


def test_delete_user_spans_tenants_and_tools(tmp_path):
    """A user's state leaf lives under every (tenant, tool) they've used."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    state = StateStorage(root=state_root)

    user = uuid.uuid4()
    tenant_1, tenant_2 = uuid.uuid4(), uuid.uuid4()
    tool_1, tool_2 = uuid.uuid4(), uuid.uuid4()

    # Same user across two tenants × two tools.
    _seed_user_home(state, tenant_1, tool_1, user, b"t1-tool1")
    _seed_user_home(state, tenant_1, tool_2, user, b"t1-tool2")
    _seed_user_home(state, tenant_2, tool_1, user, b"t2-tool1")
    # And a control: a different user in one of those tools.
    other_user = uuid.uuid4()
    _seed_user_home(state, tenant_1, tool_1, other_user, b"other")

    freed = state.delete_user(user)

    assert freed >= (len(b"t1-tool1") + len(b"t1-tool2") + len(b"t2-tool1"))
    assert not (state_root / str(tenant_1) / str(tool_1) / str(user)).exists()
    assert not (state_root / str(tenant_1) / str(tool_2) / str(user)).exists()
    assert not (state_root / str(tenant_2) / str(tool_1) / str(user)).exists()
    # Control user's state survives.
    assert (state_root / str(tenant_1) / str(tool_1) / str(other_user) / "login.json").exists()


@pytest.mark.asyncio
async def test_delete_tenant_removes_all_tools_and_users(tmp_path):
    """Tenant-level cascade wipes every tool + every user's state under it."""
    bin_root = tmp_path / "bin"
    state_root = tmp_path / "state"
    bin_root.mkdir()
    state_root.mkdir()

    binary = BinaryStorage(root=bin_root)
    state = StateStorage(root=state_root)

    victim_tenant = uuid.uuid4()
    bystander_tenant = uuid.uuid4()
    tool_1, tool_2 = uuid.uuid4(), uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()

    # Two tools under victim tenant, each with binary + per-user state.
    sha1, _ = await binary.write(
        tenant_key=str(victim_tenant), tool_id=str(tool_1),
        stream=io.BytesIO(_SHEBANG), max_bytes=1_000_000,
    )
    sha2, _ = await binary.write(
        tenant_key=str(victim_tenant), tool_id=str(tool_2),
        stream=io.BytesIO(_SHEBANG + b"\n"), max_bytes=1_000_000,
    )
    _seed_user_home(state, victim_tenant, tool_1, user_a)
    _seed_user_home(state, victim_tenant, tool_1, user_b)
    _seed_user_home(state, victim_tenant, tool_2, user_a)

    # Bystander tenant must survive untouched.
    sha3, _ = await binary.write(
        tenant_key=str(bystander_tenant), tool_id=str(tool_1),
        stream=io.BytesIO(b"#!/bin/sh\n# bystander\n"), max_bytes=1_000_000,
    )
    _seed_user_home(state, bystander_tenant, tool_1, user_a, b"bystander-secret")

    bin_freed = binary.delete_tenant(str(victim_tenant))
    state_freed = state.delete_tenant(victim_tenant)

    assert bin_freed > 0
    assert state_freed > 0
    # Victim tenant root is gone.
    assert not (bin_root / str(victim_tenant)).exists()
    assert not (state_root / str(victim_tenant)).exists()
    # Bystander tenant fully intact.
    assert binary.resolve(str(bystander_tenant), str(tool_1), sha3).exists()
    assert (state_root / str(bystander_tenant) / str(tool_1) / str(user_a) / "login.json").exists()
