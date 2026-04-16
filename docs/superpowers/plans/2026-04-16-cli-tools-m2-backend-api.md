# CLI Tools — M2: Backend API + Execution Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Full CRUD + upload + test-run API for CLI tools, rewired execution path using the M1 `BinaryRunner`, with tenant checks, encryption, schema validation, audit logging, and explicit error classes.

**Architecture:** A thin FastAPI router (`app/api/cli_tools.py`) sits on top of three pure services — `BinaryStorage` (filesystem content-addressed blobs), `CliToolConfig` (Pydantic schema + encryption codec for `Tool.config`), and the rewritten `cli_tool_executor` which now delegates the actual process invocation to `BinaryRunner` from M1. Permission checks are centralised in a `_require_tool_access` helper.

**Tech Stack:** FastAPI · Pydantic v2 · SQLAlchemy async · `jsonschema` · existing `encrypt_data` / `decrypt_data` from `app.core.security` · `BinaryRunner` from M1.

**Spec:** `docs/superpowers/specs/2026-04-16-cli-tools-management-design.md` — §5.1 data model, §5.4 API, §7 security, §9 errors, §12 M2.

**Depends on:** M1 merged (plan `2026-04-16-cli-tools-m1-sandbox-base.md`).

**Field-name note:** "cli_config" in the spec maps to the existing `Tool.config` JSON column when `Tool.type == "cli"`.

---

## File structure

| Path | Purpose |
|---|---|
| `backend/app/services/cli_tools/__init__.py` | Package init, re-exports |
| `backend/app/services/cli_tools/schema.py` | Pydantic v2 models for `Tool.config` shape |
| `backend/app/services/cli_tools/crypto.py` | Symmetric env-value encrypt/decrypt wrappers |
| `backend/app/services/cli_tools/placeholders.py` | Whitelist placeholder renderer for args and env values |
| `backend/app/services/cli_tools/storage.py` | `BinaryStorage` service — filesystem content-addressed blobs, magic-number validation |
| `backend/app/services/cli_tools/errors.py` | `CliToolErrorClass` enum + `CliToolError` exception |
| `backend/app/services/cli_tool_executor.py` | Rewrite — delegates to `BinaryRunner`, adds tenant check + jsonschema |
| `backend/app/api/cli_tools.py` | Router with 7 endpoints |
| `backend/app/main.py` | Wire the new router in |
| `backend/tests/test_cli_tools_schema.py` | Pydantic model tests |
| `backend/tests/test_cli_tools_crypto.py` | Encrypt/decrypt round-trip |
| `backend/tests/test_cli_tools_placeholders.py` | Whitelist + substitution tests |
| `backend/tests/test_cli_tools_storage.py` | BinaryStorage + magic-number tests |
| `backend/tests/test_cli_tool_executor_v2.py` | Rewritten executor: tenant check, schema, execution paths |
| `backend/tests/test_cli_tools_api.py` | API: permission matrix, upload, test-run |

Total: 10 new, 2 modified.

---

## Task 1: Pydantic schema for `Tool.config`

**Files:**
- Create: `backend/app/services/cli_tools/__init__.py`
- Create: `backend/app/services/cli_tools/schema.py`
- Create: `backend/tests/test_cli_tools_schema.py`

- [ ] **Step 1: Write the failing test**

```python
"""Schema tests for Tool.config (CLI-tool shape)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.cli_tools.schema import CliToolConfig, SandboxConfig


def test_cli_tool_config_minimal():
    cfg = CliToolConfig()
    assert cfg.binary_sha256 is None
    assert cfg.args_template == []
    assert cfg.env_inject == {}
    assert cfg.timeout_seconds == 30
    assert cfg.sandbox.cpu_limit == "1.0"
    assert cfg.sandbox.memory_limit == "512m"
    assert cfg.sandbox.network is False
    assert cfg.sandbox.image is None  # follow :stable


def test_cli_tool_config_round_trip_via_dict():
    raw = {
        "binary_sha256": "a" * 64,
        "binary_size": 1024,
        "binary_original_name": "my-tool",
        "args_template": ["--user", "{user.id}"],
        "env_inject": {"KEY": "value"},
        "timeout_seconds": 60,
        "sandbox": {"cpu_limit": "0.5", "memory_limit": "256m", "network": True, "image": "pinned-tag"},
    }
    cfg = CliToolConfig.model_validate(raw)
    assert cfg.model_dump(mode="json") == {**raw, "binary_uploaded_at": None, "sandbox": {**raw["sandbox"], "readonly_fs": True}}


def test_cli_tool_config_rejects_bad_sha():
    with pytest.raises(ValidationError):
        CliToolConfig.model_validate({"binary_sha256": "not-hex"})


def test_cli_tool_config_rejects_negative_timeout():
    with pytest.raises(ValidationError):
        CliToolConfig.model_validate({"timeout_seconds": 0})
```

- [ ] **Step 2: Run test to verify it fails**

```
cd backend
python -m pytest tests/test_cli_tools_schema.py -v
```

Expected: ModuleNotFoundError for `app.services.cli_tools.schema`.

- [ ] **Step 3: Write the module**

Create `backend/app/services/cli_tools/__init__.py`:

```python
"""CLI Tools management subsystem (M2 on)."""
```

Create `backend/app/services/cli_tools/schema.py`:

```python
"""Pydantic schema for the CLI-tool shape stored in Tool.config.

Add-only evolution rule (see spec §11.1): never rename or remove fields.
New fields must have a default that preserves pre-upgrade behaviour.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SandboxConfig(BaseModel):
    """Per-tool sandbox overrides. `image=None` means follow the platform :stable alias."""

    model_config = ConfigDict(extra="forbid")

    cpu_limit: str = "1.0"
    memory_limit: str = "512m"
    network: bool = False
    readonly_fs: bool = True
    image: Optional[str] = None


class CliToolConfig(BaseModel):
    """Shape of Tool.config when Tool.type == 'cli'."""

    model_config = ConfigDict(extra="forbid")

    binary_sha256: Optional[str] = None
    binary_size: Optional[int] = Field(default=None, ge=0)
    binary_original_name: Optional[str] = None
    binary_uploaded_at: Optional[datetime] = None

    args_template: list[str] = Field(default_factory=list)
    env_inject: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, gt=0, le=600)

    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    @field_validator("binary_sha256")
    @classmethod
    def _check_sha(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _SHA256_RE.match(v):
            raise ValueError("binary_sha256 must be 64 lower-case hex chars")
        return v
```

- [ ] **Step 4: Run test to verify it passes**

```
cd backend
python -m pytest tests/test_cli_tools_schema.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```
git add backend/app/services/cli_tools/__init__.py backend/app/services/cli_tools/schema.py backend/tests/test_cli_tools_schema.py
git commit -m "feat(cli-tools): Pydantic schema for Tool.config cli shape"
```

---

## Task 2: Env-value encryption codec

**Files:**
- Create: `backend/app/services/cli_tools/crypto.py`
- Create: `backend/tests/test_cli_tools_crypto.py`

- [ ] **Step 1: Write the failing test**

```python
"""Round-trip tests for env-value encryption."""

from __future__ import annotations

from app.services.cli_tools.crypto import encrypt_env, decrypt_env, mask_env


def test_encrypt_env_round_trip():
    plaintext = {"API_KEY": "secret-abc", "PUBLIC_URL": "https://example.com"}
    encrypted = encrypt_env(plaintext)
    assert encrypted != plaintext
    for v in encrypted.values():
        assert v.startswith("enc:v1:")
    assert decrypt_env(encrypted) == plaintext


def test_decrypt_passes_through_unencrypted_values():
    # Older records may predate encryption; the codec must tolerate plaintext.
    mixed = {"API_KEY": "enc:v1:" + "0" * 16, "OLD": "plain-value"}
    # We can't decrypt the fake ciphertext, but plaintext passthrough must work.
    assert decrypt_env({"OLD": "plain-value"}) == {"OLD": "plain-value"}


def test_mask_env_hides_values():
    plain = {"API_KEY": "secret", "URL": "https://x"}
    assert mask_env(plain) == {"API_KEY": "***", "URL": "***"}
```

- [ ] **Step 2: Run test to verify it fails**

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

```python
"""Symmetric encryption for Tool.config env_inject values.

Reuses the project-wide `encrypt_data` / `decrypt_data` primitives that
already back `LLMModel.api_key_encrypted`. Values are tagged with an
"enc:v1:" prefix so a mixed plaintext/ciphertext dict (older records,
partial updates) can be decoded without a schema bump.
"""

from __future__ import annotations

from typing import Mapping

from app.core.security import decrypt_data, encrypt_data

_PREFIX = "enc:v1:"


def encrypt_env(values: Mapping[str, str]) -> dict[str, str]:
    """Encrypt every value; keys are left as-is."""
    return {k: _PREFIX + encrypt_data(v) for k, v in values.items()}


def decrypt_env(values: Mapping[str, str]) -> dict[str, str]:
    """Decrypt prefixed values; passthrough for legacy plaintext."""
    out: dict[str, str] = {}
    for k, v in values.items():
        if isinstance(v, str) and v.startswith(_PREFIX):
            out[k] = decrypt_data(v[len(_PREFIX):])
        else:
            out[k] = v
    return out


def mask_env(values: Mapping[str, str]) -> dict[str, str]:
    """Return a redacted view for API responses."""
    return {k: "***" for k in values}
```

- [ ] **Step 4: Run test**

```
cd backend
python -m pytest tests/test_cli_tools_crypto.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```
git add backend/app/services/cli_tools/crypto.py backend/tests/test_cli_tools_crypto.py
git commit -m "feat(cli-tools): env-value encryption codec"
```

---

## Task 3: Placeholder whitelist renderer

**Files:**
- Create: `backend/app/services/cli_tools/placeholders.py`
- Create: `backend/tests/test_cli_tools_placeholders.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

```python
"""Whitelist-based placeholder substitution for CLI-tool args and env values.

Allowed placeholders (all single-dotted):
  {user.id}, {user.phone}, {user.email}
  {agent.id}
  {tenant.id}
  {params.<name>}  — where <name> is a key present in the caller params

A literal `{{` renders as `{`, `}}` as `}` (doubled-brace escape).
Anything else in braces raises InvalidPlaceholderError.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WHITELIST: dict[str, set[str]] = {
    "user": {"id", "phone", "email"},
    "agent": {"id"},
    "tenant": {"id"},
}

# Matches one placeholder `{root.key}` — doubled braces are pre-escaped before.
_PLACEHOLDER_RE = re.compile(r"\{([a-z]+)\.([a-z_][a-z0-9_]*)\}")


class InvalidPlaceholderError(ValueError):
    """Raised when a template uses a placeholder outside the whitelist."""


@dataclass(frozen=True)
class PlaceholderContext:
    user: dict[str, str] = field(default_factory=dict)
    agent: dict[str, str] = field(default_factory=dict)
    tenant: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)


def render(template: str, ctx: PlaceholderContext) -> str:
    """Substitute whitelisted placeholders in `template`."""
    # Protect doubled braces, then substitute, then unprotect.
    protected = template.replace("{{", "\x00OPEN\x00").replace("}}", "\x00CLOSE\x00")

    def _sub(match: re.Match[str]) -> str:
        root, key = match.group(1), match.group(2)
        if root == "params":
            if key not in ctx.params:
                raise InvalidPlaceholderError(f"params.{key} not provided")
            return ctx.params[key]
        allowed = _WHITELIST.get(root)
        if allowed is None or key not in allowed:
            raise InvalidPlaceholderError(f"{root}.{key} is not a recognised placeholder")
        values = getattr(ctx, root)
        if key not in values:
            raise InvalidPlaceholderError(f"{root}.{key} not provided in context")
        return values[key]

    substituted = _PLACEHOLDER_RE.sub(_sub, protected)
    return substituted.replace("\x00OPEN\x00", "{").replace("\x00CLOSE\x00", "}")
```

- [ ] **Step 4: Run test**

```
cd backend
python -m pytest tests/test_cli_tools_placeholders.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```
git add backend/app/services/cli_tools/placeholders.py backend/tests/test_cli_tools_placeholders.py
git commit -m "feat(cli-tools): whitelist placeholder renderer"
```

---

## Task 4: Error-class enum and exception

**Files:**
- Create: `backend/app/services/cli_tools/errors.py`

- [ ] **Step 1: Write the module (no dedicated test file — exercised via executor + API tests later)**

```python
"""Explicit error classes for CLI-tool execution.

Per spec §9. Strings used here are also the values returned to the caller
and surfaced in structured logs; do not rename without updating the spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CliToolErrorClass(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    BINARY_FAILED = "BINARY_FAILED"
    SANDBOX_FAILED = "SANDBOX_FAILED"


@dataclass
class CliToolError(Exception):
    error_class: CliToolErrorClass
    message: str

    def __str__(self) -> str:
        return f"[{self.error_class.value}] {self.message}"
```

- [ ] **Step 2: Verify it imports cleanly**

```
cd backend
python -c "from app.services.cli_tools.errors import CliToolError, CliToolErrorClass; print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```
git add backend/app/services/cli_tools/errors.py
git commit -m "feat(cli-tools): explicit error-class taxonomy"
```

---

## Task 5: `BinaryStorage` — write / read / path resolution

**Files:**
- Create: `backend/app/services/cli_tools/storage.py`
- Create: `backend/tests/test_cli_tools_storage.py`

- [ ] **Step 1: Write the failing test**

```python
"""BinaryStorage tests — filesystem-backed, content-addressed."""

from __future__ import annotations

import hashlib
import io

import pytest

from app.services.cli_tools.storage import (
    BinaryStorage,
    MagicNumberError,
    SizeLimitExceededError,
)

_ELF = b"\x7fELF" + b"\x00" * 60 + b"rest-of-elf-header"
_SHEBANG = b"#!/bin/sh\necho hi\n"


@pytest.mark.asyncio
async def test_write_and_resolve_roundtrip(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    sha, size = await storage.write(
        tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_ELF), max_bytes=1_000_000
    )
    assert sha == hashlib.sha256(_ELF).hexdigest()
    assert size == len(_ELF)

    path = storage.resolve(tenant_key="t1", tool_id="tool1", sha=sha)
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o555
    assert path.read_bytes() == _ELF


@pytest.mark.asyncio
async def test_write_rejects_unknown_magic(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    with pytest.raises(MagicNumberError):
        await storage.write(
            tenant_key="t1", tool_id="tool1", stream=io.BytesIO(b"\xff\xff\xff\xff not a binary"), max_bytes=1_000
        )


@pytest.mark.asyncio
async def test_write_accepts_shebang_script(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    sha, size = await storage.write(
        tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG), max_bytes=1_000_000
    )
    assert size == len(_SHEBANG)
    assert storage.resolve("t1", "tool1", sha).read_bytes() == _SHEBANG


@pytest.mark.asyncio
async def test_write_rejects_oversize(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    with pytest.raises(SizeLimitExceededError):
        await storage.write(
            tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_ELF + b"x" * 10_000), max_bytes=1_000
        )


@pytest.mark.asyncio
async def test_list_shas_for_tool(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    a, _ = await storage.write("t1", "tool1", io.BytesIO(_ELF), max_bytes=1_000_000)
    b, _ = await storage.write("t1", "tool1", io.BytesIO(_SHEBANG), max_bytes=1_000_000)
    assert set(storage.list_shas("t1", "tool1")) == {a, b}


@pytest.mark.asyncio
async def test_unreferenced_shas_scan(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    a, _ = await storage.write("t1", "tool1", io.BytesIO(_ELF), max_bytes=1_000_000)
    b, _ = await storage.write("t1", "tool1", io.BytesIO(_SHEBANG), max_bytes=1_000_000)
    # Only `a` is still referenced.
    orphans = list(storage.iter_orphans(referenced_shas={a}))
    assert len(orphans) == 1
    assert orphans[0].name == f"{b}.bin"
```

- [ ] **Step 2: Run test to verify it fails**

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

```python
"""Filesystem-backed content-addressed binary storage.

Layout (see spec §5.2):
    <root>/<tenant_key>/<tool_id>/<sha256>.bin

`tenant_key` is either a stringified UUID for tenant-scoped tools or the
literal "_global" for platform-scoped tools.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator


_ACCEPTED_MAGICS: tuple[bytes, ...] = (
    b"\x7fELF",           # ELF (Linux)
    b"\xfe\xed\xfa\xce",  # Mach-O 32
    b"\xfe\xed\xfa\xcf",  # Mach-O 64
    b"\xce\xfa\xed\xfe",  # Mach-O 32 LE
    b"\xcf\xfa\xed\xfe",  # Mach-O 64 LE
    b"\xca\xfe\xba\xbe",  # Mach-O universal
    b"#!",                # shebang script
)


class MagicNumberError(ValueError):
    """Uploaded bytes do not start with a recognised executable magic number."""


class SizeLimitExceededError(ValueError):
    """Uploaded bytes exceeded the per-call max."""


class BinaryStorage:
    """Write / resolve / list content-addressed binaries under `root`."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    async def write(
        self,
        *,
        tenant_key: str,
        tool_id: str,
        stream: BinaryIO,
        max_bytes: int,
        chunk_size: int = 65536,
    ) -> tuple[str, int]:
        """Stream-read `stream`, validate, write. Returns (sha256, size)."""
        target_dir = self.root / tenant_key / tool_id
        target_dir.mkdir(parents=True, exist_ok=True)

        hasher = hashlib.sha256()
        size = 0
        magic_seen = False
        magic_buffer = b""

        fd, tmp_path_str = tempfile.mkstemp(dir=target_dir, suffix=".partial")
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = stream.read(chunk_size)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise SizeLimitExceededError(f"binary exceeds {max_bytes} bytes")
                    hasher.update(chunk)
                    out.write(chunk)

                    if not magic_seen:
                        magic_buffer = (magic_buffer + chunk)[:8]
                        if len(magic_buffer) >= 4:
                            if not any(magic_buffer.startswith(m) for m in _ACCEPTED_MAGICS):
                                raise MagicNumberError(f"magic bytes {magic_buffer[:4]!r} not accepted")
                            magic_seen = True

            if not magic_seen:
                raise MagicNumberError("file too short to identify magic")

            sha = hasher.hexdigest()
            final = target_dir / f"{sha}.bin"
            if final.exists():
                # Content-addressed: identical content already stored; keep perms strict.
                tmp_path.unlink()
            else:
                tmp_path.replace(final)
            final.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            return sha, size
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    def resolve(self, tenant_key: str, tool_id: str, sha: str) -> Path:
        return self.root / tenant_key / tool_id / f"{sha}.bin"

    def list_shas(self, tenant_key: str, tool_id: str) -> Iterator[str]:
        d = self.root / tenant_key / tool_id
        if not d.is_dir():
            return
        for entry in d.iterdir():
            if entry.suffix == ".bin" and len(entry.stem) == 64:
                yield entry.stem

    def iter_orphans(self, referenced_shas: set[str]) -> Iterator[Path]:
        for tenant_dir in self.root.iterdir():
            if not tenant_dir.is_dir():
                continue
            for tool_dir in tenant_dir.iterdir():
                if not tool_dir.is_dir():
                    continue
                for entry in tool_dir.iterdir():
                    if entry.suffix == ".bin" and entry.stem not in referenced_shas:
                        yield entry

    def delete_orphans(self, orphans: Iterable[Path]) -> int:
        count = 0
        for path in orphans:
            try:
                path.unlink()
                count += 1
            except FileNotFoundError:
                pass
        return count
```

- [ ] **Step 4: Run test**

```
cd backend
python -m pytest tests/test_cli_tools_storage.py -v
```

Expected: 6 PASS.

- [ ] **Step 5: Commit**

```
git add backend/app/services/cli_tools/storage.py backend/tests/test_cli_tools_storage.py
git commit -m "feat(cli-tools): content-addressed binary storage + magic-number validation"
```

---

## Task 6: Rewrite `cli_tool_executor` to use `BinaryRunner` + tenant check + jsonschema

**Files:**
- Modify: `backend/app/services/cli_tool_executor.py` (complete rewrite)
- Create: `backend/tests/test_cli_tool_executor_v2.py`

- [ ] **Step 1: Write the failing test**

```python
"""Rewritten cli_tool_executor: binary runner, tenant check, schema validation."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.cli_tool_executor import execute_cli_tool
from app.services.cli_tools.errors import CliToolError, CliToolErrorClass
from app.services.sandbox.local.binary_runner import BinaryRunResult


def _tool(*, tenant_id, config, parameters_schema=None, is_active=True):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.tenant_id = tenant_id
    t.is_active = is_active
    t.config = config
    t.parameters_schema = parameters_schema or {}
    return t


def _agent(tenant_id):
    a = MagicMock()
    a.id = uuid.uuid4()
    a.tenant_id = tenant_id
    return a


@pytest.mark.asyncio
async def test_executor_rejects_tenant_mismatch():
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    tool = _tool(tenant_id=tenant_a, config={"binary_sha256": "a" * 64})
    agent = _agent(tenant_b)
    storage = MagicMock()
    runner = MagicMock()

    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool,
            agent=agent,
            params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=storage,
            runner=runner,
        )
    assert exc_info.value.error_class is CliToolErrorClass.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_executor_allows_global_tool_cross_tenant():
    tool = _tool(tenant_id=None, config={"binary_sha256": "a" * 64})
    agent = _agent(uuid.uuid4())
    storage = MagicMock()
    storage.resolve.return_value.is_file.return_value = True
    runner = MagicMock()
    runner.run = AsyncMock(return_value=BinaryRunResult(exit_code=0, stdout="ok", stderr="", duration_ms=12))

    result = await execute_cli_tool(
        tool=tool,
        agent=agent,
        params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=storage,
        runner=runner,
    )
    assert result.exit_code == 0
    assert result.stdout == "ok"


@pytest.mark.asyncio
async def test_executor_rejects_disabled_tool():
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, config={"binary_sha256": "a" * 64}, is_active=False)
    agent = _agent(tenant)
    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool,
            agent=agent,
            params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=MagicMock(),
            runner=MagicMock(),
        )
    assert exc_info.value.error_class is CliToolErrorClass.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_executor_validates_params_against_schema():
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={"binary_sha256": "a" * 64},
        parameters_schema={
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
    )
    agent = _agent(tenant)
    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool,
            agent=agent,
            params={"n": "not-int"},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=MagicMock(),
            runner=MagicMock(),
        )
    assert exc_info.value.error_class is CliToolErrorClass.VALIDATION_ERROR


@pytest.mark.asyncio
async def test_executor_maps_timeout_to_error_class():
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, config={"binary_sha256": "a" * 64, "timeout_seconds": 2})
    agent = _agent(tenant)
    storage = MagicMock()
    storage.resolve.return_value.is_file.return_value = True
    runner = MagicMock()
    runner.run = AsyncMock(return_value=BinaryRunResult(exit_code=-1, stdout="", stderr="", duration_ms=2100, timed_out=True))

    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool,
            agent=agent,
            params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=storage,
            runner=runner,
        )
    assert exc_info.value.error_class is CliToolErrorClass.TIMEOUT


@pytest.mark.asyncio
async def test_executor_renders_args_and_env_placeholders():
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={
            "binary_sha256": "a" * 64,
            "args_template": ["--user={user.id}", "--n={params.n}"],
            "env_inject": {"PHONE": "{user.phone}"},
        },
    )
    agent = _agent(tenant)
    storage = MagicMock()
    storage.resolve.return_value.is_file.return_value = True
    runner = MagicMock()
    runner.run = AsyncMock(return_value=BinaryRunResult(exit_code=0, stdout="", stderr="", duration_ms=1))

    await execute_cli_tool(
        tool=tool,
        agent=agent,
        params={"n": "42"},
        user_context={"id": "u1", "phone": "13800000000", "email": "u@example.com"},
        storage=storage,
        runner=runner,
    )

    kwargs = runner.run.call_args.kwargs
    assert kwargs["args"] == ["--user=u1", "--n=42"]
    assert kwargs["env"] == {"PHONE": "13800000000"}
```

- [ ] **Step 2: Run test — all fail (module rewrite pending)**

Expected: 6 failures — either `ImportError` for `execute_cli_tool` new signature or assertion errors.

- [ ] **Step 3: Write the new executor**

Replace `backend/app/services/cli_tool_executor.py` entirely:

```python
"""Execute a CLI tool: tenant check -> schema -> placeholders -> binary runner.

This replaces the pre-M2 executor. The call site in `agent_tools.py` is
updated in a later task (Task 8) to pass DB objects rather than raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import jsonschema

from app.services.cli_tools.crypto import decrypt_env
from app.services.cli_tools.errors import CliToolError, CliToolErrorClass
from app.services.cli_tools.placeholders import (
    InvalidPlaceholderError,
    PlaceholderContext,
    render,
)
from app.services.cli_tools.schema import CliToolConfig
from app.services.cli_tools.storage import BinaryStorage
from app.services.sandbox.local.binary_runner import BinaryRunner, BinaryRunResult


@dataclass
class CliExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


async def execute_cli_tool(
    *,
    tool: Any,
    agent: Any,
    params: Mapping[str, Any],
    user_context: Mapping[str, str],
    storage: BinaryStorage,
    runner: BinaryRunner,
) -> CliExecutionResult:
    """Execute `tool` (a Tool ORM row) against `agent` (an Agent ORM row).

    Raises CliToolError with an explicit error_class on any validation,
    permission, or execution failure.
    """
    if not tool.is_active:
        raise CliToolError(CliToolErrorClass.PERMISSION_DENIED, "tool is disabled")

    if tool.tenant_id is not None and tool.tenant_id != agent.tenant_id:
        raise CliToolError(CliToolErrorClass.PERMISSION_DENIED, "tool not available to this tenant")

    # Parse config — add-only schema means older records tolerate default-filled.
    config = CliToolConfig.model_validate(tool.config or {})

    if not config.binary_sha256:
        raise CliToolError(CliToolErrorClass.NOT_FOUND, "tool has no binary uploaded yet")

    schema = dict(tool.parameters_schema or {})
    if schema:
        try:
            jsonschema.validate(instance=dict(params), schema=schema)
        except jsonschema.ValidationError as exc:
            raise CliToolError(CliToolErrorClass.VALIDATION_ERROR, exc.message) from exc

    ctx = PlaceholderContext(
        user=dict(user_context),
        agent={"id": str(agent.id)},
        tenant={"id": str(agent.tenant_id) if agent.tenant_id else ""},
        params={k: str(v) for k, v in params.items()},
    )

    try:
        rendered_args = [render(a, ctx) for a in config.args_template]
        decrypted_env = decrypt_env(dict(config.env_inject))
        rendered_env = {k: render(v, ctx) for k, v in decrypted_env.items()}
    except InvalidPlaceholderError as exc:
        raise CliToolError(CliToolErrorClass.VALIDATION_ERROR, str(exc)) from exc

    tenant_key = str(tool.tenant_id) if tool.tenant_id is not None else "_global"
    binary_path = storage.resolve(tenant_key, str(tool.id), config.binary_sha256)
    if not binary_path.is_file():
        raise CliToolError(CliToolErrorClass.NOT_FOUND, f"binary {config.binary_sha256[:12]}... missing on disk")

    # The runner is constructed per-tool so sandbox settings apply.
    configured_runner = runner.__class__(
        image=config.sandbox.image or runner.image,
        cpu_limit=config.sandbox.cpu_limit,
        memory_limit=config.sandbox.memory_limit,
        network=config.sandbox.network,
    )

    result: BinaryRunResult = await configured_runner.run(
        binary_host_path=str(binary_path),
        args=rendered_args,
        env=rendered_env,
        timeout_seconds=config.timeout_seconds,
    )

    if result.sandbox_failed:
        raise CliToolError(CliToolErrorClass.SANDBOX_FAILED, result.error)
    if result.timed_out:
        raise CliToolError(CliToolErrorClass.TIMEOUT, "binary exceeded timeout_seconds")
    if result.exit_code != 0:
        tail = result.stderr[-200:] if result.stderr else ""
        raise CliToolError(CliToolErrorClass.BINARY_FAILED, f"exit={result.exit_code}; stderr={tail}")

    return CliExecutionResult(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
    )
```

- [ ] **Step 4: Run tests**

```
cd backend
python -m pytest tests/test_cli_tool_executor_v2.py -v
```

Expected: 6 PASS.

- [ ] **Step 5: Commit**

```
git add backend/app/services/cli_tool_executor.py backend/tests/test_cli_tool_executor_v2.py
git commit -m "feat(cli-tools): rewrite executor — tenant check, schema, BinaryRunner"
```

---

## Task 7: Update `agent_tools.py` call site to pass ORM objects

**Files:**
- Modify: `backend/app/services/agent_tools.py` — the `_try_execute_cli_tool` function only

- [ ] **Step 1: Read the current call site**

```
grep -n "_try_execute_cli_tool\|execute_cli_tool" backend/app/services/agent_tools.py | head -10
```

Locate the single call to `execute_cli_tool(...)`. Capture its current surrounding code (usually ~30 lines).

- [ ] **Step 2: Rewrite the call**

Inside `_try_execute_cli_tool`, replace the body that builds `tool_config` dict and calls the old signature with:

```python
from app.services.cli_tools.errors import CliToolError, CliToolErrorClass
from app.services.cli_tools.storage import BinaryStorage
from app.services.sandbox.local.binary_runner import BinaryRunner
from app.services.cli_tool_executor import execute_cli_tool, CliExecutionResult
from pathlib import Path
from app.config import get_settings

settings = get_settings()
storage = BinaryStorage(root=Path("/data/cli_binaries"))
runner = BinaryRunner(image="clawith-cli-sandbox:stable")

try:
    exec_result: CliExecutionResult = await execute_cli_tool(
        tool=tool_row,                   # existing local: the Tool ORM row
        agent=agent,                     # existing local
        params=arguments,                # existing local: dict from LLM
        user_context=await _user_context(user_id),   # existing helper or inline dict
        storage=storage,
        runner=runner,
    )
    return exec_result.stdout or "(no output)"
except CliToolError as exc:
    return f"❌ [{exc.error_class.value}] {exc.message}"
```

Keep the surrounding `try/except` and return-on-no-binary behaviour.

- [ ] **Step 3: Run existing agent-tool tests**

```
cd backend
python -m pytest tests/ -k "agent_tools or cli" -v
```

Expected: all pass. Any pre-existing test that relied on the old `execute_cli_tool(tool_config, arguments, user_id)` shape is covered by the rewrite in Task 6; if a test still uses the old signature, update it to the new shape.

- [ ] **Step 4: Commit**

```
git add backend/app/services/agent_tools.py
git commit -m "refactor(cli-tools): agent_tools uses new execute_cli_tool signature"
```

---

## Task 8: API router — skeleton + list + create (no binary)

**Files:**
- Create: `backend/app/api/cli_tools.py`
- Modify: `backend/app/main.py` — register router

- [ ] **Step 1: Write the router**

```python
"""CLI tools management API.

Endpoints (see spec §5.4):
    GET    /api/tools?type=cli                 list
    POST   /api/tools/cli                      create metadata
    POST   /api/tools/{id}/binary              upload binary
    GET    /api/tools/{id}                     detail (env masked)
    PATCH  /api/tools/{id}/cli                 update metadata
    DELETE /api/tools/{id}                     delete
    POST   /api/tools/{id}/test-run            test-run
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.tool import Tool
from app.models.user import User
from app.services.cli_tools.crypto import encrypt_env, mask_env
from app.services.cli_tools.errors import CliToolError, CliToolErrorClass
from app.services.cli_tools.schema import CliToolConfig
from app.services.cli_tools.storage import (
    BinaryStorage,
    MagicNumberError,
    SizeLimitExceededError,
)

router = APIRouter(prefix="/api/tools", tags=["cli-tools"])

_BINARY_MAX_BYTES = 100 * 1024 * 1024
_STORAGE_ROOT = Path("/data/cli_binaries")


def _require_manage(user: User, tool: Optional[Tool] = None) -> None:
    """org_admin of the tool's tenant, or platform_admin anywhere."""
    if user.role == "platform_admin":
        return
    if user.role != "org_admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "org_admin required")
    if tool is not None and tool.tenant_id is not None and tool.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tool belongs to another tenant")
    if tool is not None and tool.tenant_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only platform_admin may manage global tools")


def _visible_to(user: User):
    """Scope filter: user's own tenant + global."""
    if user.role == "platform_admin":
        return lambda row: True
    return lambda row: row.tenant_id is None or row.tenant_id == user.tenant_id


class CliToolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    description: str = ""
    parameters_schema: dict = Field(default_factory=dict)
    config: CliToolConfig = Field(default_factory=CliToolConfig)
    tenant_id: Optional[uuid.UUID] = None


class CliToolUpdate(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    parameters_schema: Optional[dict] = None
    config: Optional[CliToolConfig] = None
    is_active: Optional[bool] = None


class CliToolOut(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str
    description: str
    type: str
    tenant_id: Optional[uuid.UUID]
    is_active: bool
    parameters_schema: dict
    config: dict  # env_inject is masked before returning


@router.get("", response_model=list[CliToolOut])
async def list_cli_tools(
    type: str = Query("cli"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(select(Tool).where(Tool.type == type))).scalars().all()
    visible = list(filter(_visible_to(user), rows))
    out = []
    for t in visible:
        cfg = dict(t.config or {})
        cfg["env_inject"] = mask_env(cfg.get("env_inject", {}))
        out.append(CliToolOut(
            id=t.id,
            name=t.name,
            display_name=t.display_name,
            description=t.description,
            type=t.type,
            tenant_id=t.tenant_id,
            is_active=t.enabled,
            parameters_schema=t.parameters_schema,
            config=cfg,
        ))
    return out


@router.post("/cli", response_model=CliToolOut, status_code=status.HTTP_201_CREATED)
async def create_cli_tool(
    body: CliToolCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    effective_tenant = body.tenant_id
    if user.role == "org_admin":
        if effective_tenant not in (None, user.tenant_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "may only create tools in your own tenant")
        effective_tenant = user.tenant_id
    elif user.role != "platform_admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "org_admin required")

    cfg_with_encrypted_env = body.config.model_copy(
        update={"env_inject": encrypt_env(body.config.env_inject)}
    )

    tool = Tool(
        id=uuid.uuid4(),
        name=body.name,
        display_name=body.display_name,
        description=body.description,
        type="cli",
        source="admin",
        parameters_schema=body.parameters_schema,
        config=cfg_with_encrypted_env.model_dump(mode="json"),
        tenant_id=effective_tenant,
        enabled=True,
    )
    db.add(tool)
    await db.commit()
    await db.refresh(tool)

    out_cfg = dict(tool.config)
    out_cfg["env_inject"] = mask_env(out_cfg.get("env_inject", {}))
    return CliToolOut(
        id=tool.id,
        name=tool.name,
        display_name=tool.display_name,
        description=tool.description,
        type=tool.type,
        tenant_id=tool.tenant_id,
        is_active=tool.enabled,
        parameters_schema=tool.parameters_schema,
        config=out_cfg,
    )
```

- [ ] **Step 2: Register the router in main.py**

In `backend/app/main.py`, find the section where other routers are included (`app.include_router(...)`) and add:

```python
from app.api import cli_tools as cli_tools_api
# ...
app.include_router(cli_tools_api.router)
```

- [ ] **Step 3: Smoke test — startup imports cleanly**

```
cd backend
python -c "from app.main import app; print([r.path for r in app.routes if '/api/tools' in r.path])"
```

Expected: output includes `/api/tools` and `/api/tools/cli`.

- [ ] **Step 4: Commit**

```
git add backend/app/api/cli_tools.py backend/app/main.py
git commit -m "feat(cli-tools): API skeleton — list and create endpoints"
```

---

## Task 9: API — binary upload endpoint

**Files:**
- Modify: `backend/app/api/cli_tools.py`

- [ ] **Step 1: Append the endpoint**

```python
@router.post("/{tool_id}/binary", response_model=CliToolOut)
async def upload_binary(
    tool_id: uuid.UUID,
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    _require_manage(user, tool)

    tenant_key = str(tool.tenant_id) if tool.tenant_id is not None else "_global"
    storage = BinaryStorage(root=_STORAGE_ROOT)

    try:
        sha, size = await storage.write(
            tenant_key=tenant_key,
            tool_id=str(tool.id),
            stream=file.file,
            max_bytes=_BINARY_MAX_BYTES,
        )
    except MagicNumberError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unrecognised binary format: {exc}") from exc
    except SizeLimitExceededError as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc

    cfg = dict(tool.config or {})
    cfg["binary_sha256"] = sha
    cfg["binary_size"] = size
    cfg["binary_original_name"] = file.filename or "uploaded.bin"
    from datetime import datetime, timezone as _tz
    cfg["binary_uploaded_at"] = datetime.now(_tz.utc).isoformat()
    tool.config = cfg
    await db.commit()
    await db.refresh(tool)

    out_cfg = dict(tool.config)
    out_cfg["env_inject"] = mask_env(out_cfg.get("env_inject", {}))
    return CliToolOut(
        id=tool.id,
        name=tool.name,
        display_name=tool.display_name,
        description=tool.description,
        type=tool.type,
        tenant_id=tool.tenant_id,
        is_active=tool.enabled,
        parameters_schema=tool.parameters_schema,
        config=out_cfg,
    )
```

- [ ] **Step 2: Manual smoke test**

Run inside the backend container:

```
BASE=http://localhost:8000
TOKEN=<platform_admin JWT>

# Create tool
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -X POST "$BASE/api/tools/cli" \
  -d '{"name":"smoke","display_name":"Smoke","config":{}}'

# Upload a shebang script
echo -e '#!/bin/sh\necho hi' > /tmp/smoke.sh && chmod +x /tmp/smoke.sh
curl -s -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/smoke.sh" \
  -X POST "$BASE/api/tools/<TOOL_ID>/binary"
```

Expected: second call returns 200 with `config.binary_sha256` populated.

- [ ] **Step 3: Commit**

```
git add backend/app/api/cli_tools.py
git commit -m "feat(cli-tools): binary upload endpoint"
```

---

## Task 10: API — detail, update, delete

**Files:**
- Modify: `backend/app/api/cli_tools.py`

- [ ] **Step 1: Append the endpoints**

```python
@router.get("/{tool_id}", response_model=CliToolOut)
async def get_cli_tool(
    tool_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    if not _visible_to(user)(tool):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not visible")
    cfg = dict(tool.config or {})
    cfg["env_inject"] = mask_env(cfg.get("env_inject", {}))
    return CliToolOut(
        id=tool.id, name=tool.name, display_name=tool.display_name,
        description=tool.description, type=tool.type, tenant_id=tool.tenant_id,
        is_active=tool.enabled, parameters_schema=tool.parameters_schema, config=cfg,
    )


@router.patch("/{tool_id}/cli", response_model=CliToolOut)
async def update_cli_tool(
    tool_id: uuid.UUID,
    body: CliToolUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    _require_manage(user, tool)

    if body.display_name is not None:
        tool.display_name = body.display_name
    if body.description is not None:
        tool.description = body.description
    if body.parameters_schema is not None:
        tool.parameters_schema = body.parameters_schema
    if body.is_active is not None:
        tool.enabled = body.is_active
    if body.config is not None:
        merged = CliToolConfig.model_validate(tool.config or {}).model_dump()
        incoming = body.config.model_dump()
        # Re-encrypt env on update (body arrives as plaintext).
        incoming["env_inject"] = encrypt_env(incoming["env_inject"])
        # Preserve binary metadata unless the incoming body explicitly overrides.
        for preserved in ("binary_sha256", "binary_size", "binary_original_name", "binary_uploaded_at"):
            if incoming.get(preserved) is None:
                incoming[preserved] = merged.get(preserved)
        tool.config = incoming

    await db.commit()
    await db.refresh(tool)
    cfg = dict(tool.config or {})
    cfg["env_inject"] = mask_env(cfg.get("env_inject", {}))
    return CliToolOut(
        id=tool.id, name=tool.name, display_name=tool.display_name,
        description=tool.description, type=tool.type, tenant_id=tool.tenant_id,
        is_active=tool.enabled, parameters_schema=tool.parameters_schema, config=cfg,
    )


@router.delete("/{tool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cli_tool(
    tool_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    _require_manage(user, tool)
    await db.delete(tool)
    await db.commit()
    return None
```

- [ ] **Step 2: Commit**

```
git add backend/app/api/cli_tools.py
git commit -m "feat(cli-tools): detail / update / delete endpoints"
```

---

## Task 11: API — test-run endpoint

**Files:**
- Modify: `backend/app/api/cli_tools.py`

- [ ] **Step 1: Append the endpoint**

```python
class TestRunRequest(BaseModel):
    params: dict = Field(default_factory=dict)
    mock_env: Optional[dict[str, str]] = None  # keys to override with plaintext


class TestRunResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    error_class: Optional[str] = None
    error_message: Optional[str] = None


@router.post("/{tool_id}/test-run", response_model=TestRunResponse)
async def test_run_cli_tool(
    tool_id: uuid.UUID,
    body: TestRunRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    _require_manage(user, tool)

    from app.services.cli_tool_executor import execute_cli_tool
    from app.services.sandbox.local.binary_runner import BinaryRunner

    storage = BinaryStorage(root=_STORAGE_ROOT)
    runner = BinaryRunner(image="clawith-cli-sandbox:stable")

    # Build a synthetic "agent" matching tenant for the tenant check.
    class _SyntheticAgent:
        id = uuid.uuid4()
        tenant_id = tool.tenant_id if tool.tenant_id is not None else user.tenant_id

    user_context = {
        "id": str(user.id),
        "phone": getattr(user, "phone", "") or "",
        "email": getattr(user, "email", "") or "",
    }

    # If mock_env supplied, temporarily replace those env keys in the config.
    original_config = dict(tool.config or {})
    if body.mock_env:
        patched_env = dict(encrypt_env(body.mock_env))
        tool.config = {**original_config, "env_inject": {**original_config.get("env_inject", {}), **patched_env}}

    try:
        result = await execute_cli_tool(
            tool=tool,
            agent=_SyntheticAgent(),
            params=body.params,
            user_context=user_context,
            storage=storage,
            runner=runner,
        )
        return TestRunResponse(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
        )
    except CliToolError as exc:
        return TestRunResponse(
            exit_code=-1, stdout="", stderr="",
            duration_ms=0,
            error_class=exc.error_class.value,
            error_message=exc.message,
        )
    finally:
        # Never commit the mock-env patch back to the DB.
        tool.config = original_config
```

- [ ] **Step 2: Commit**

```
git add backend/app/api/cli_tools.py
git commit -m "feat(cli-tools): test-run endpoint with mock-env support"
```

---

## Task 12: Audit-log integration

**Files:**
- Modify: `backend/app/api/cli_tools.py`

- [ ] **Step 1: Find the audit helper**

```
grep -rn "def write_audit\|AuditLog(" backend/app | head -5
```

Identify how existing CRUD endpoints write `audit_logs` (commonly a helper like `write_audit_log(db, user, action, resource, detail)`).

- [ ] **Step 2: Add audit calls to create / upload / update / delete**

At the tail of `create_cli_tool`, `upload_binary`, `update_cli_tool`, and `delete_cli_tool` (before returning), call the helper:

```python
from app.models.audit import AuditLog  # or the project's helper

db.add(AuditLog(
    user_id=user.id,
    action="cli_tool.create",  # or .upload_binary / .update / .delete
    resource_type="tool",
    resource_id=str(tool.id),
    detail={"name": tool.name, "tenant_id": str(tool.tenant_id) if tool.tenant_id else None},
))
await db.commit()
```

Adjust the concrete call to match what the project already uses — keep the pattern identical.

- [ ] **Step 3: Commit**

```
git add backend/app/api/cli_tools.py
git commit -m "feat(cli-tools): audit-log entries for CRUD + upload"
```

---

## Task 13: API permission matrix integration test

**Files:**
- Create: `backend/tests/test_cli_tools_api.py`

- [ ] **Step 1: Write the failing test**

```python
"""End-to-end permission matrix for CLI-tool management."""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient


def _jwt_for(role: str, tenant_id: uuid.UUID | None = None) -> dict[str, str]:
    """Produce an Authorization header using the project's existing token helper.

    Replace with the real helper used by other tests (usually
    `tests.utils.jwt_for(user)` or similar)."""
    from tests.utils import make_test_user_token
    return {"Authorization": f"Bearer {make_test_user_token(role=role, tenant_id=tenant_id)}"}


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def test_member_cannot_create(client):
    r = client.post("/api/tools/cli", json={"name": "x", "display_name": "x", "config": {}}, headers=_jwt_for("member", uuid.uuid4()))
    assert r.status_code == 403


def test_org_admin_creates_in_own_tenant(client):
    tenant = uuid.uuid4()
    r = client.post("/api/tools/cli", json={"name": "mine", "display_name": "Mine", "config": {}}, headers=_jwt_for("org_admin", tenant))
    assert r.status_code == 201
    assert r.json()["tenant_id"] == str(tenant)


def test_org_admin_cannot_create_global(client):
    r = client.post(
        "/api/tools/cli",
        json={"name": "g", "display_name": "g", "config": {}, "tenant_id": None},
        headers=_jwt_for("org_admin", uuid.uuid4()),
    )
    # Either 403 or forced to own tenant — assert tenant_id is not None.
    if r.status_code == 201:
        assert r.json()["tenant_id"] is not None
    else:
        assert r.status_code == 403


def test_cross_tenant_read_rejects(client):
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    created = client.post("/api/tools/cli", json={"name": "a", "display_name": "a", "config": {}}, headers=_jwt_for("org_admin", tenant_a)).json()

    r = client.get(f"/api/tools/{created['id']}", headers=_jwt_for("org_admin", tenant_b))
    assert r.status_code in (403, 404)


def test_upload_rejects_bad_magic(client):
    tenant = uuid.uuid4()
    created = client.post("/api/tools/cli", json={"name": "b", "display_name": "b", "config": {}}, headers=_jwt_for("org_admin", tenant)).json()
    r = client.post(
        f"/api/tools/{created['id']}/binary",
        files={"file": ("bad.bin", io.BytesIO(b"not an elf"), "application/octet-stream")},
        headers=_jwt_for("org_admin", tenant),
    )
    assert r.status_code == 400


def test_delete_visible_in_list(client):
    tenant = uuid.uuid4()
    created = client.post("/api/tools/cli", json={"name": "d", "display_name": "d", "config": {}}, headers=_jwt_for("org_admin", tenant)).json()
    r = client.delete(f"/api/tools/{created['id']}", headers=_jwt_for("org_admin", tenant))
    assert r.status_code == 204
    lst = client.get("/api/tools?type=cli", headers=_jwt_for("org_admin", tenant)).json()
    assert all(row["id"] != created["id"] for row in lst)
```

- [ ] **Step 2: Run test, fix integration friction**

```
cd backend
python -m pytest tests/test_cli_tools_api.py -v
```

If `tests.utils.make_test_user_token` does not exist, replace `_jwt_for` with the project's equivalent (find by `grep -rn "make_test_user_token\|_test_token\|login_as" backend/tests`). The point of this task is the *permission matrix*, not the token helper — adapt to what's available.

- [ ] **Step 3: Commit**

```
git add backend/tests/test_cli_tools_api.py
git commit -m "test(cli-tools): permission matrix + upload validation"
```

---

## M2 Exit Criteria

- [ ] All 7 API endpoints respond with correct status codes per §5.4
- [ ] `backend/tests/test_cli_tools_schema.py`, `test_cli_tools_crypto.py`, `test_cli_tools_placeholders.py`, `test_cli_tools_storage.py`, `test_cli_tool_executor_v2.py`, `test_cli_tools_api.py` all pass
- [ ] Manual curl smoke test (create → upload shebang → test-run) returns `exit_code=0`
- [ ] Existing `agent_tools` regression tests pass after the Task 7 refactor
- [ ] Audit-log rows appear in DB for each mutating call

## Handoff to M3

M3 (frontend) consumes the API contract locked down here. If any response shape needs to change, the change is made in this milestone (before M3 starts), not retro-fitted.
