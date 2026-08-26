#!/usr/bin/env python3
"""Fail when the legacy product keyword reaches user-visible source text."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_KEYWORD = "cla" "with"
KEYWORD_RE = re.compile(re.escape(LEGACY_KEYWORD), re.IGNORECASE)
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".py", ".sh", ".ts", ".tsx", ".yaml", ".yml"}


@dataclass(frozen=True)
class InternalCompatibilityAllowance:
    path_pattern: re.Pattern[str]
    line_pattern: re.Pattern[str]
    reason: str


def _allow(path: str, line: str, reason: str) -> InternalCompatibilityAllowance:
    return InternalCompatibilityAllowance(
        path_pattern=re.compile(path, re.IGNORECASE),
        line_pattern=re.compile(line, re.IGNORECASE),
        reason=reason,
    )


_legacy = re.escape(LEGACY_KEYWORD)
ALLOWANCES = (
    _allow(
        r"README(?:_[A-Za-z-]+)?\.md",
        rf"(?:https?://\S*{_legacy}|\bcd\s+{_legacy}\b|DATABASE_URL=.*{_legacy}|export\s+{_legacy}_|assets/{_legacy}_QRcode\.png)",
        "Repository URLs, checkout directory names, operator variables, and the legacy QR asset filename remain compatibility references.",
    ),
    _allow(
        r"CONTRIBUTING\.md",
        rf"(?:https?://\S*{_legacy}|docker\s+run\b.*{_legacy})",
        "Contribution links and runnable database/container examples retain repository and deployment identifiers.",
    ),
    _allow(
        r"\.github/ISSUE_TEMPLATE/(?:bug_report|feature_request)\.yml",
        rf"https?://\S*{_legacy}",
        "Issue templates retain canonical repository links.",
    ),
    _allow(
        r"\.env\.example",
        rf"{_legacy}",
        "Example environment variables, persisted paths, storage credentials, and database identifiers are operator contracts.",
    ),
    _allow(
        r"setup\.sh",
        rf"(?:{_legacy}_[A-Z0-9_]+|(?:psql|createuser|createdb|PGPASSWORD|DATABASE_URL|DB_URL|host\s+all).*{_legacy}|{_legacy}.*(?:python|psql))",
        "Setup commands retain environment-variable and database role/name compatibility.",
    ),
    _allow(
        r"restart\.sh",
        rf"(?:DATABASE_URL.*{_legacy}|docker\s+ps.*{_legacy}|PROJECT_NAME=.*{_legacy})",
        "Restart commands retain database, container-filter, and project-name compatibility.",
    ),
    _allow(
        r"helm/(?:QUICKSTART(?:_EN)?\.md|clawith/.*)",
        rf"{_legacy}",
        "Helm examples and templates retain chart/release/namespace, image, service, PVC, secret, database, and repository identifiers.",
    ),
    _allow(
        r"frontend/package(?:-lock)?\.json",
        rf'"name"\s*:\s*"{_legacy}-frontend"',
        "Published package identity is retained for dependency and lockfile compatibility.",
    ),
    _allow(
        r"frontend/src/utils/(?:themeMode|theme|h5AuthSession)\.ts",
        rf"{_legacy}",
        "Browser event, bridge, and storage keys retain existing-client state compatibility.",
    ),
    _allow(
        r"frontend/src/hooks/useSpeechInput\.ts",
        rf"{_legacy}-pcm16",
        "The AudioWorklet registration name is a runtime protocol identifier.",
    ),
    _allow(
        rf"frontend/src/components/atlas/(?:index|{_legacy}Wordmark)\.tsx?",
        rf"{_legacy}Wordmark",
        "The legacy component export remains import-compatible; rendered text and ARIA are neutral.",
    ),
    _allow(
        rf"frontend/public/sdk/{_legacy}\.js",
        rf"(?:data-{_legacy}-wm|window\.{_legacy})",
        "Public SDK DOM attributes and window API are compatibility contracts.",
    ),
    _allow(
        r"frontend/public/audio/pcm16-worklet\.js",
        rf"(?:{_legacy}Pcm16Processor|{_legacy}-pcm16)",
        "AudioWorklet class and registration names must match existing callers.",
    ),
    _allow(
        r"\.agents/architecture/claude-memory-provenance\.md",
        rf"prod_access_via_{_legacy}_ssh\.md",
        "Provenance records an immutable historical filename.",
    ),
    _allow(
        r"backend/app/config\.py",
        rf"(?:\.{_legacy}|postgresql\+asyncpg://{_legacy}|{_legacy}_network)",
        "Filesystem, database credential defaults, and Docker network names are deployment identifiers.",
    ),
    _allow(
        r"backend/app/(?:main\.py|services/llm/(?:compactor|caller|tool_output_store)\.py|services/agent_tools\.py)",
        rf"{_legacy}_[A-Z0-9_]+",
        "Environment variable names are stable operator contracts.",
    ),
    _allow(
        r"backend/app/(?:core/token_cache\.py|services/(?:auth_provider|feishu_service|org_sync_adapter|dingtalk_token|webhook_security|toolscall/runtime)\.py|api/(?:wecom|teams)\.py)",
        rf"{_legacy}[:.-]",
        "Redis and idempotency key namespaces must remain readable across rolling upgrades.",
    ),
    _allow(
        r"backend/app/services/agent_tools\.py",
        rf"(?:{_legacy}-agent-|x-exa-integration.*{_legacy}|/sdk/{_legacy}\.js|window\.{_legacy}|\"/{_legacy}\")",
        "Temporary prefixes, integration IDs, SDK APIs, and the persisted CDN default path are compatibility identifiers.",
    ),
    _allow(
        r"backend/app/services/(?:wecom_stream|wechat_channel|resource_discovery|sandbox_mcp_host|mcp_client|media_playback)\.py",
        rf"{_legacy}",
        "Provider patch markers, client/session IDs, discovery identity, and playback cookie names are protocol state.",
    ),
    _allow(
        r"backend/app/services/(?:media_url_source|dingtalk_stream|document_conversion/(?:chrome_renderer|html_to_pdf))\.py",
        rf"{_legacy}",
        "Temporary file prefixes, renderer DOM markers, and legacy handler class names are internal runtime identifiers.",
    ),
    _allow(
        r"backend/app/services/(?:agent_manager|media_tool_contract)\.py",
        rf"{_legacy}",
        "Container labels/names and stripped transport-header names are operational compatibility contracts.",
    ),
    _allow(
        r"backend/app/(?:core/logging_config\.py|api/upload\.py|services/cli_tools/state_storage\.py)",
        rf"{_legacy}",
        "Log, upload, and runtime-user paths preserve existing deployment ownership and storage.",
    ),
    _allow(
        r"backend/app/api/tenants\.py",
        rf"\\\.{_legacy}\\\.ai",
        "Legacy tenant-domain parsing remains accepted for existing SSO deployments.",
    ),
    _allow(
        r"backend/app/models/agent\.py",
        rf"github\.com/dataelement/{_legacy}/issues/238",
        "The model comment links to immutable issue provenance.",
    ),
    _allow(
        r"backend/app/scripts/(?:backfill_department_paths|cleanup_duplicate_feishu_users)\.py",
        rf"{_legacy}-backend-1",
        "Historical operator examples retain the deployed container name.",
    ),
)


def _source_files() -> list[Path]:
    roots = (
        REPO_ROOT / "frontend" / "src",
        REPO_ROOT / "frontend" / "public",
        REPO_ROOT / "backend" / "app",
        REPO_ROOT / ".agents",
        REPO_ROOT / ".github" / "ISSUE_TEMPLATE",
        REPO_ROOT / "helm",
    )
    files = [
        *REPO_ROOT.glob("README*.md"),
        REPO_ROOT / ".env.example",
        REPO_ROOT / ".github" / "workflows" / "release.yml",
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / "ARCHITECTURE_SPEC_EN.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "frontend" / "package.json",
        REPO_ROOT / "frontend" / "package-lock.json",
        REPO_ROOT / "restart.sh",
        REPO_ROOT / "setup.sh",
    ]
    for root in roots:
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in TEXT_SUFFIXES
            and "backend/app/alembic" not in path.as_posix()
            and "__pycache__" not in path.parts
        )
    return sorted(set(files))


def main() -> int:
    violations: list[str] = []
    allowance_counts = [0] * len(ALLOWANCES)
    for path in _source_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if not KEYWORD_RE.search(line):
                continue
            for index, allowance in enumerate(ALLOWANCES):
                if allowance.path_pattern.fullmatch(relative) and allowance.line_pattern.search(line):
                    allowance_counts[index] += 1
                    break
            else:
                violations.append(f"{relative}:{line_number}: {line.strip()}")

    if violations:
        print("Forbidden user-visible keyword references:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1

    print("User-visible keyword scan passed. Internal compatibility allowances:")
    for allowance, count in zip(ALLOWANCES, allowance_counts, strict=True):
        if count:
            print(f"- {count} reference(s): {allowance.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
