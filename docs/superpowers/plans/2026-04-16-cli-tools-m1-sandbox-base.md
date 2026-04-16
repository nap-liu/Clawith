# CLI Tools — M1: Storage + Sandbox Base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the docker volume + sandbox image + binary runner that later milestones need.

**Architecture:** Add a minimal `debian:bookworm-slim` sandbox image published with a date tag and `:stable` alias, mount a dedicated docker volume for content-addressed binaries, and introduce a `BinaryRunner` service that runs a host-side binary inside a `--rm` docker container with tight capability/network/resource constraints. No API or UI yet — this milestone ends at "we can execute an arbitrary ELF from Python with full isolation and timeouts".

**Tech Stack:** Python 3.12 · docker SDK (already used by `DockerBackend`) · pytest · pytest-asyncio · Debian slim.

**Spec:** `docs/superpowers/specs/2026-04-16-cli-tools-management-design.md` — §5.2 storage, §5.3 sandbox, §12 M1.

**Field-name note:** the spec talks about `cli_config`; in code we reuse the existing `Tool.config` JSON column to avoid a schema migration. "cli_config" in the spec = contents of `Tool.config` when `Tool.type == "cli"`.

---

## File structure

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Add `cli_binaries` named volume mounted at `/data/cli_binaries` on backend |
| `backend/cli_sandbox/Dockerfile` | New. Minimal `debian:bookworm-slim` base, no extra packages |
| `backend/cli_sandbox/Makefile` | New. Build + tag + push helpers |
| `backend/cli_sandbox/README.md` | New. Runbook for upgrading the `:stable` alias |
| `backend/app/services/sandbox/local/binary_runner.py` | New. `BinaryRunner` class that wraps docker SDK to execute a mounted binary |
| `backend/tests/test_binary_runner.py` | New. Unit + integration tests for `BinaryRunner` |

Total: 1 modified, 5 new.

---

## Task 1: Add `cli_binaries` docker volume to compose

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add named volume + mount**

Edit `docker-compose.yml`. In the `volumes:` top-level block (currently has `pgdata:` and `redisdata:`) add `cli_binaries:`:

```yaml
volumes:
  pgdata:
  redisdata:
  cli_binaries:
```

Then under `services.backend.volumes` add the mount after `./backend/agent_data:/data/agents`:

```yaml
    volumes:
      - ./backend:/app
      - ./backend/agent_data:/data/agents
      - cli_binaries:/data/cli_binaries
      - /var/run/docker.sock:/var/run/docker.sock
      - ./ss-nodes.json:/data/ss-nodes.json:ro
```

- [ ] **Step 2: Recreate backend container and verify mount**

Run:

```
docker compose -p clawith up -d --force-recreate backend
docker exec clawith-backend-1 ls -la /data/cli_binaries
```

Expected: directory exists, empty (no errors).

- [ ] **Step 3: Verify docker volume is persisted**

Run:

```
docker volume inspect clawith_cli_binaries
```

Expected: JSON output with `"Name": "clawith_cli_binaries"` and a host Mountpoint path.

- [ ] **Step 4: Commit**

```
git add docker-compose.yml
git commit -m "feat(cli-tools): add cli_binaries volume for uploaded binaries"
```

---

## Task 2: Create minimal sandbox Dockerfile

**Files:**
- Create: `backend/cli_sandbox/Dockerfile`

- [ ] **Step 1: Write the Dockerfile**

```dockerfile
# Minimal sandbox base for running user-uploaded CLI binaries.
# No extra packages. The uploaded binary is bind-mounted at /binary read-only
# and invoked directly. Run as `nobody` with cap-drop ALL and no-new-privileges.
FROM debian:bookworm-slim

# Upgrade packages so we pick up whatever base-image security fixes Debian has
# shipped since the last :stable rebuild.
RUN apt-get update \
  && apt-get upgrade -y --no-install-recommends \
  && rm -rf /var/lib/apt/lists/*

# nobody (65534) is present in debian:slim; assert it exists so the image
# fails fast if Debian ever changes that.
RUN id -u nobody >/dev/null

# Default invocation is overridden by `docker run`; this CMD makes a bare
# `docker run <image>` exit cleanly rather than drop into a shell we don't
# want a sandboxed binary to inherit.
CMD ["/bin/true"]
```

- [ ] **Step 2: Verify build**

```
docker build -t clawith-cli-sandbox:local-test backend/cli_sandbox/
docker run --rm clawith-cli-sandbox:local-test
```

Expected: build succeeds, run exits 0 with no output.

- [ ] **Step 3: Commit**

```
git add backend/cli_sandbox/Dockerfile
git commit -m "feat(cli-tools): minimal debian-slim sandbox image"
```

---

## Task 3: Create Makefile and runbook for sandbox image

**Files:**
- Create: `backend/cli_sandbox/Makefile`
- Create: `backend/cli_sandbox/README.md`

- [ ] **Step 1: Write the Makefile**

```makefile
# Build / tag / publish the clawith-cli-sandbox image.
#
# REGISTRY and IMAGE must be provided by the caller (from a .env file or the
# shell environment). We deliberately do not hard-code a registry hostname.

REGISTRY ?= $(error REGISTRY is not set — export REGISTRY=your-registry-host)
IMAGE    ?= clawith-cli-sandbox
BASE     ?= debian-bookworm-slim
DATE     := $(shell date -u +%Y%m%d)
TAG      := $(BASE)-$(DATE)

.PHONY: build tag push promote-stable all

build:
	docker build -t $(IMAGE):$(TAG) .

tag: build
	docker tag $(IMAGE):$(TAG) $(REGISTRY)/$(IMAGE):$(TAG)

push: tag
	docker push $(REGISTRY)/$(IMAGE):$(TAG)

# Operator: only run once a build has been validated in staging.
promote-stable:
	docker tag $(REGISTRY)/$(IMAGE):$(TAG) $(REGISTRY)/$(IMAGE):stable
	docker push $(REGISTRY)/$(IMAGE):stable

all: push
```

- [ ] **Step 2: Write the runbook**

```markdown
# clawith-cli-sandbox runbook

Publishes the sandbox image used by the CLI tools subsystem to execute
user-uploaded binaries.

## Tag scheme

- `<registry>/clawith-cli-sandbox:debian-bookworm-slim-YYYYMMDD` — immutable, keep forever
- `<registry>/clawith-cli-sandbox:stable` — moving alias the backend resolves by default

Tools without `config.sandbox.image` set follow `:stable`. Tools that pin
a specific dated tag stay on that tag across platform upgrades.

## Publishing a new version

```
cd backend/cli_sandbox
export REGISTRY=<your-registry-host>

# 1. Build + push the dated tag
make push

# 2. Validate in staging (see §2 Validation)

# 3. Promote to stable only after validation
make promote-stable
```

## Validation before promoting to :stable

1. Configure a staging environment to use the new dated tag (pin it on the
   CLI tool via UI or API PATCH).
2. Run `POST /api/tools/{id}/test-run` for every CLI tool against the new
   image.
3. Watch the backend logs for 10 minutes for `SANDBOX_FAILED` or
   `BINARY_FAILED` error classes.
4. If clean: `make promote-stable`. If not: skip promotion; investigate.

## Rollback

To revert `:stable` to a previous dated tag:

```
docker pull <registry>/clawith-cli-sandbox:<previous-date-tag>
docker tag <registry>/clawith-cli-sandbox:<previous-date-tag> <registry>/clawith-cli-sandbox:stable
docker push <registry>/clawith-cli-sandbox:stable
```

Tools that pinned a specific tag are unaffected by rollback.
```

- [ ] **Step 3: Verify Makefile help works**

```
cd backend/cli_sandbox
make build REGISTRY=localhost:5000 2>&1 | tail -5
```

Expected: `docker build` executes (you can cancel after "Sending build context" since we tested the Dockerfile in Task 2).

- [ ] **Step 4: Commit**

```
git add backend/cli_sandbox/Makefile backend/cli_sandbox/README.md
git commit -m "feat(cli-tools): publishing runbook for sandbox image"
```

---

## Task 4: `BinaryRunner` — failing test for the happy path

**Files:**
- Create: `backend/tests/test_binary_runner.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for BinaryRunner — executes a host-side binary inside a docker sandbox."""

from __future__ import annotations

import os
import stat
import tempfile
import textwrap
from pathlib import Path

import pytest

from app.services.sandbox.local.binary_runner import BinaryRunner, BinaryRunResult


def _write_echo_script(tmp_path: Path) -> Path:
    """Create a tiny shebang script that prints its args and one env var."""
    script = tmp_path / "echo.sh"
    script.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "args=$*"
        echo "greeting=$GREETING"
    """))
    script.chmod(0o555)
    return script


@pytest.mark.asyncio
async def test_binary_runner_happy_path(tmp_path):
    """Runs a shebang script inside the sandbox, verifies stdout and exit_code."""
    script = _write_echo_script(tmp_path)

    runner = BinaryRunner(image="clawith-cli-sandbox:local-test")
    result = await runner.run(
        binary_host_path=str(script),
        args=["hello", "world"],
        env={"GREETING": "hi"},
        timeout_seconds=5,
    )

    assert isinstance(result, BinaryRunResult)
    assert result.exit_code == 0
    assert "args=hello world" in result.stdout
    assert "greeting=hi" in result.stdout
    assert result.duration_ms >= 0
    assert result.timed_out is False
```

- [ ] **Step 2: Run test to verify it fails**

```
cd backend
python -m pytest tests/test_binary_runner.py::test_binary_runner_happy_path -v
```

Expected: `ImportError: cannot import name 'BinaryRunner' from 'app.services.sandbox.local.binary_runner'` or `ModuleNotFoundError`.

---

## Task 5: `BinaryRunner` — minimal implementation for the happy path

**Files:**
- Create: `backend/app/services/sandbox/local/binary_runner.py`

- [ ] **Step 1: Write the module**

```python
"""Run a host-side binary inside an ephemeral docker sandbox.

Used by the CLI tools subsystem. The binary is bind-mounted read-only at
`/binary` inside a `clawith-cli-sandbox` container; the container drops all
capabilities, runs as `nobody`, has a read-only rootfs with a tmpfs `/tmp`,
and optionally no network.

This is *not* a replacement for `DockerBackend.execute` (which runs
source code in language-specific images). It is a focused runner for the
narrow "execute this uploaded binary" use case.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import docker
from docker.errors import APIError, ContainerError, ImageNotFound

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BinaryRunResult:
    """Outcome of a single sandboxed binary execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    sandbox_failed: bool = False
    error: str = ""


class BinaryRunner:
    """Execute a mounted binary inside an ephemeral sandbox container."""

    def __init__(
        self,
        image: str,
        *,
        cpu_limit: str = "1.0",
        memory_limit: str = "512m",
        pids_limit: int = 100,
        network: bool = False,
        tmpfs_size: str = "64m",
    ) -> None:
        self.image = image
        self.cpu_limit = cpu_limit
        self.memory_limit = memory_limit
        self.pids_limit = pids_limit
        self.network = network
        self.tmpfs_size = tmpfs_size
        self._client = docker.from_env()

    async def run(
        self,
        binary_host_path: str,
        args: Sequence[str],
        env: Mapping[str, str],
        timeout_seconds: int = 30,
    ) -> BinaryRunResult:
        """Execute `binary_host_path` inside a one-shot sandbox container.

        Args:
            binary_host_path: absolute path on the host to the binary. Must
                be readable + executable. Mounted read-only at /binary.
            args: arguments passed to the binary.
            env: environment variables passed to the container.
            timeout_seconds: kill the container if it runs longer than this.
        """
        host_path = Path(binary_host_path).resolve()
        if not host_path.is_file():
            return BinaryRunResult(
                exit_code=1,
                stdout="",
                stderr="",
                duration_ms=0,
                sandbox_failed=True,
                error=f"binary not found on host: {host_path}",
            )

        start = time.monotonic()
        try:
            result = await asyncio.to_thread(
                self._run_blocking,
                str(host_path),
                list(args),
                dict(env),
                timeout_seconds,
            )
        except ImageNotFound as exc:
            return BinaryRunResult(
                exit_code=1,
                stdout="",
                stderr="",
                duration_ms=int((time.monotonic() - start) * 1000),
                sandbox_failed=True,
                error=f"sandbox image missing: {exc}",
            )
        except APIError as exc:
            return BinaryRunResult(
                exit_code=1,
                stdout="",
                stderr="",
                duration_ms=int((time.monotonic() - start) * 1000),
                sandbox_failed=True,
                error=f"docker api error: {exc}",
            )

        result_with_duration = BinaryRunResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=int((time.monotonic() - start) * 1000),
            timed_out=result.timed_out,
            sandbox_failed=result.sandbox_failed,
            error=result.error,
        )
        return result_with_duration

    def _run_blocking(
        self,
        host_path: str,
        args: list[str],
        env: dict[str, str],
        timeout_seconds: int,
    ) -> BinaryRunResult:
        """Synchronous docker-SDK invocation; called in a thread."""
        try:
            container = self._client.containers.create(
                image=self.image,
                command=["/binary", *args],
                environment=env,
                network_disabled=not self.network,
                read_only=True,
                tmpfs={"/tmp": f"rw,size={self.tmpfs_size},mode=1777"},
                mem_limit=self.memory_limit,
                nano_cpus=int(float(self.cpu_limit) * 1_000_000_000),
                pids_limit=self.pids_limit,
                user="65534:65534",
                security_opt=["no-new-privileges"],
                cap_drop=["ALL"],
                volumes={host_path: {"bind": "/binary", "mode": "ro"}},
            )
        except (APIError, ImageNotFound) as exc:
            raise exc

        timed_out = False
        try:
            container.start()
            try:
                status = container.wait(timeout=timeout_seconds)
                exit_code = int(status.get("StatusCode", -1))
            except Exception:
                container.kill()
                timed_out = True
                exit_code = -1

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
        finally:
            try:
                container.remove(force=True)
            except APIError:
                logger.warning("failed to remove container %s", container.id, exc_info=True)

        return BinaryRunResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=0,  # filled in by the async caller
            timed_out=timed_out,
        )
```

- [ ] **Step 2: Build the local sandbox image (prereq for tests)**

```
docker build -t clawith-cli-sandbox:local-test backend/cli_sandbox/
```

Expected: `Successfully tagged clawith-cli-sandbox:local-test`.

- [ ] **Step 3: Run test to verify it passes**

```
cd backend
python -m pytest tests/test_binary_runner.py::test_binary_runner_happy_path -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```
git add backend/app/services/sandbox/local/binary_runner.py backend/tests/test_binary_runner.py
git commit -m "feat(cli-tools): BinaryRunner — sandboxed binary execution"
```

---

## Task 6: `BinaryRunner` — timeout behaviour

**Files:**
- Modify: `backend/tests/test_binary_runner.py`

- [ ] **Step 1: Add the failing timeout test**

Append to `backend/tests/test_binary_runner.py`:

```python
@pytest.mark.asyncio
async def test_binary_runner_timeout(tmp_path):
    """A binary that sleeps longer than the timeout is killed and reported."""
    script = tmp_path / "sleep.sh"
    script.write_text("#!/bin/sh\nsleep 10\n")
    script.chmod(0o555)

    runner = BinaryRunner(image="clawith-cli-sandbox:local-test")
    result = await runner.run(
        binary_host_path=str(script),
        args=[],
        env={},
        timeout_seconds=2,
    )

    assert result.timed_out is True
    assert result.exit_code == -1
    # Duration is at least the timeout (we waited that long).
    assert result.duration_ms >= 1500
```

- [ ] **Step 2: Run test to verify it passes (implementation already handles timeouts)**

```
cd backend
python -m pytest tests/test_binary_runner.py::test_binary_runner_timeout -v
```

Expected: PASS — the minimal implementation from Task 5 already handles timeouts via `container.wait(timeout=...)`.

If it fails: the exception path in `_run_blocking` didn't kill the container. Inspect stderr and adjust — the fix should be inside the `except` branch around `container.wait`.

- [ ] **Step 3: Commit**

```
git add backend/tests/test_binary_runner.py
git commit -m "test(cli-tools): BinaryRunner kills runaway binaries at timeout"
```

---

## Task 7: `BinaryRunner` — sandbox-failure surface

**Files:**
- Modify: `backend/tests/test_binary_runner.py`

- [ ] **Step 1: Add the failing sandbox-failure test**

Append:

```python
@pytest.mark.asyncio
async def test_binary_runner_missing_binary_surfaces_sandbox_failure(tmp_path):
    """Non-existent host path → sandbox_failed=True, not a crash."""
    runner = BinaryRunner(image="clawith-cli-sandbox:local-test")

    result = await runner.run(
        binary_host_path=str(tmp_path / "does-not-exist"),
        args=[],
        env={},
        timeout_seconds=5,
    )

    assert result.sandbox_failed is True
    assert result.exit_code == 1
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_binary_runner_missing_image_surfaces_sandbox_failure(tmp_path):
    """Non-existent sandbox image → sandbox_failed=True with a helpful error."""
    script = tmp_path / "noop.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o555)

    runner = BinaryRunner(image="clawith-cli-sandbox:does-not-exist-xyz")
    result = await runner.run(
        binary_host_path=str(script),
        args=[],
        env={},
        timeout_seconds=5,
    )

    assert result.sandbox_failed is True
    assert "image" in result.error.lower()
```

- [ ] **Step 2: Run tests to verify they pass**

```
cd backend
python -m pytest tests/test_binary_runner.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 3: Commit**

```
git add backend/tests/test_binary_runner.py
git commit -m "test(cli-tools): BinaryRunner surfaces sandbox failures distinctly"
```

---

## Task 8: `BinaryRunner` — network isolation check

**Files:**
- Modify: `backend/tests/test_binary_runner.py`

- [ ] **Step 1: Add the failing test**

Append:

```python
@pytest.mark.asyncio
async def test_binary_runner_network_disabled_by_default(tmp_path):
    """Default run has no network — any outbound attempt fails inside the binary."""
    script = tmp_path / "netcheck.sh"
    # getent hosts only succeeds with DNS; inside --network=none it fails.
    script.write_text("#!/bin/sh\ngetent hosts github.com && echo HAS_NET || echo NO_NET\n")
    script.chmod(0o555)

    runner = BinaryRunner(image="clawith-cli-sandbox:local-test")
    result = await runner.run(
        binary_host_path=str(script),
        args=[],
        env={},
        timeout_seconds=5,
    )

    assert result.exit_code == 0
    assert "NO_NET" in result.stdout
```

- [ ] **Step 2: Run test**

```
cd backend
python -m pytest tests/test_binary_runner.py::test_binary_runner_network_disabled_by_default -v
```

Expected: PASS (minimal impl already sets `network_disabled=True` when `network=False`).

- [ ] **Step 3: Commit**

```
git add backend/tests/test_binary_runner.py
git commit -m "test(cli-tools): BinaryRunner confirms no-network default"
```

---

## M1 Exit Criteria

- [ ] `docker compose up -d backend` succeeds with the new volume mounted
- [ ] `clawith-cli-sandbox` image builds from `backend/cli_sandbox/Dockerfile`
- [ ] `make build REGISTRY=<host>` publishes a dated tag; `make promote-stable` moves `:stable`
- [ ] All 4 tests in `backend/tests/test_binary_runner.py` pass against a locally-built sandbox image
- [ ] No API, no UI, no changes to existing CLI-tool execution path — strictly infrastructure

## Handoff to M2

M2 depends on `BinaryRunner` and the `cli_binaries` volume. M2 imports `BinaryRunner` from `app.services.sandbox.local.binary_runner` and writes binaries to `/data/cli_binaries/<tenant>/<tool>/<sha>.bin`.

No `BinaryRunner` signature changes should be needed for M2. If M2 finds one is, the change is flagged for review on the feature branch before it is made.
