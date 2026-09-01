#!/usr/bin/env python3
"""Enforce the repository-wide 800 physical-line source-file gate."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


MAX_SOURCE_LINES = 800
REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = ("backend", "frontend", "scripts")
SOURCE_SUFFIXES = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".sh",
    ".sql",
    ".html",
}


@dataclass(frozen=True)
class ExplicitExemption:
    path: str
    reason: str


# Exemptions are intentionally exact-path and reviewable. Do not add source
# files here. A new entry must be declarative data/configuration or generated
# output, with a concrete reason that explains why it cannot contain runtime
# implementation logic.
EXPLICIT_EXEMPTIONS = {
    exemption.path: exemption.reason
    for exemption in (
        ExplicitExemption(
            path="frontend/package-lock.json",
            reason="machine-generated npm dependency lockfile",
        ),
        ExplicitExemption(
            path="frontend/src/i18n/en.json",
            reason="declarative English localization data",
        ),
        ExplicitExemption(
            path="frontend/src/i18n/zh.json",
            reason="declarative Chinese localization data",
        ),
    )
}


def _repository_files() -> list[Path]:
    """Return tracked and non-ignored untracked files from the current checkout."""
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={REPO_ROOT}",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [
        REPO_ROOT / relative.decode("utf-8", errors="strict")
        for relative in result.stdout.split(b"\0")
        if relative
    ]


def _text_line_count(path: Path) -> int | None:
    """Return physical lines for UTF-8 text, or None for binary/non-UTF-8 data."""
    content = path.read_bytes()
    if b"\0" in content:
        return None
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not content:
        return 0
    return content.count(b"\n") + (0 if content.endswith(b"\n") else 1)


def _is_handwritten_source(relative: str) -> bool:
    path = Path(relative)
    if not path.parts or path.parts[0] not in SOURCE_ROOTS:
        return False
    suffixes = path.suffixes
    if suffixes[-2:] == [".py", ".mako"]:
        return True
    return path.suffix in SOURCE_SUFFIXES


def _run_self_check() -> None:
    checks = {
        "backend/app/services/agent_tools.py": True,
        "backend/tests/test_gateway_workload_capacity.py": True,
        "backend/alembic/script.py.mako": True,
        "frontend/index.html": True,
        "backend/app/services/skill_creator_files/assets__eval_review.html": True,
        "frontend/scripts/test-h5-chat-timeline.mjs": True,
        "scripts/check_source_line_limit.py": True,
        "AGENTS.md": False,
        ".agents/workflows/read_architecture.md": False,
        ".github/drone.yml": False,
        "frontend/src/i18n/en.json": False,
        "frontend/package-lock.json": False,
        "frontend/nginx.conf.template": False,
        "backend/Dockerfile": False,
    }
    for path, expected in checks.items():
        got = _is_handwritten_source(path)
        if got is not expected:
            raise AssertionError(f"classification mismatch for {path}: got {got}, expected {expected}")


def main() -> int:
    violations: list[tuple[str, int]] = []
    applied_exemptions: list[tuple[str, int, str]] = []
    scanned_source_files = 0

    for path in _repository_files():
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        if not _is_handwritten_source(relative):
            continue
        line_count = _text_line_count(path)
        if line_count is None:
            continue
        scanned_source_files += 1
        if line_count <= MAX_SOURCE_LINES:
            continue

        reason = EXPLICIT_EXEMPTIONS.get(relative)
        if reason is not None:
            applied_exemptions.append((relative, line_count, reason))
            continue
        violations.append((relative, line_count))

    print(
        f"Scanned {scanned_source_files} tracked/untracked hand-written source candidates; "
        f"maximum allowed source length is {MAX_SOURCE_LINES} physical lines."
    )
    if applied_exemptions:
        print("Explicit non-source exemptions over the limit:")
        for relative, line_count, reason in sorted(applied_exemptions):
            print(f"  {line_count:6d}  {relative}  ({reason})")

    if not violations:
        print("PASS: no non-exempt text file exceeds the source line limit.")
        return 0

    print(f"FAIL: {len(violations)} non-exempt source files exceed {MAX_SOURCE_LINES} lines:")
    for relative, line_count in sorted(violations, key=lambda item: (-item[1], item[0])):
        print(f"  {line_count:6d}  {relative}")
    print(
        "Split every listed hand-written source file into architecture-aligned modules. "
        "There is no historical-debt allowlist."
    )
    return 1


if __name__ == "__main__":
    try:
        _run_self_check()
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        print(f"ERROR: source-line gate could not complete: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
