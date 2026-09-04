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
_compat_token = rf"(?:[A-Za-z0-9_./:-]+{_legacy}[A-Za-z0-9_./:-]*|{_legacy}[A-Za-z0-9_./:-]+)"
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
        rf"(?:postgresql\+asyncpg://{_legacy}:{_legacy}@[^\s]+/{_legacy}|~/\.{_legacy}|MINIO_ROOT_USER={_legacy}\b|MINIO_ROOT_PASSWORD={_legacy}-[A-Za-z0-9-]+|MINIO_BUCKET={_legacy}\b|{_legacy}_[A-Z0-9_]+)",
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
        rf"(?:{_compat_token}|(?:-n|install|upgrade|history|rollback|uninstall|status|values|manifest|template|namespace)\s+{_legacy}\b|(?:database|name|namespace)\s*:\s*\"?{_legacy}\b|^\s*-\s+{_legacy}\b|(?:psql|pg_dump)\s+-U\s+postgres\s+{_legacy}\b)",
        "Helm examples and templates retain chart/release/namespace, image, service, PVC, secret, database, and repository identifiers.",
    ),
    _allow(
        r"frontend/package(?:-lock)?\.json",
        rf'"name"\s*:\s*"{_legacy}-frontend"',
        "Published package identity is retained for dependency and lockfile compatibility.",
    ),
    _allow(
        r"frontend/src/utils/(?:themeMode|theme|h5AuthSession)\.ts",
        rf"(?:{_legacy}[:_-][A-Za-z0-9:-]+|{_legacy}ThemeBridge)",
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
        r"\.agents/runbooks/production_release\.md",
        rf"{_legacy}_project_legacy",
        "The legacy rollback database role is an operator-only compatibility identifier.",
    ),
    _allow(
        r"\.agents/(?:rules/release|runbooks/production_release)\.md",
        rf"{_legacy}_DEPS_IMAGE",
        "The dependency-carrier build argument is an operator-only image-build contract.",
    ),
    _allow(
        r"backend/app/config\.py",
        rf"(?:\.{_legacy}|postgresql\+asyncpg://{_legacy}:{_legacy}@[^\"]+/{_legacy}|{_legacy}_network)",
        "Filesystem, database credential defaults, and Docker network names are deployment identifiers.",
    ),
    _allow(
        r"backend/app/(?:main\.py|services/llm/(?:compactor(?:_shared|_runtime_support)?|caller(?:_shared|_streaming_support)?|tool_output_store)\.py|services/agent_tools(?:_sql_support)?\.py)",
        rf"{_legacy}_[A-Z0-9_]+",
        "Environment variable names are stable operator contracts.",
    ),
    _allow(
        r"backend/app/(?:core/token_cache\.py|services/(?:auth_provider(?:_enterprise)?|feishu_service|org_sync_adapter|org_sync_wecom|dingtalk_token|webhook_security|toolscall/runtime)\.py|api/(?:wecom(?:_support)?|teams)\.py)",
        rf"{_legacy}[:.-]",
        "Redis and idempotency key namespaces must remain readable across rolling upgrades.",
    ),
    _allow(
        r"backend/app/api/websocket(?:_setup_ops)?\.py",
        rf"{_legacy}:project-subagent-web",
        "The project WebSocket Redis namespace is an internal rolling-upgrade contract.",
    ),
    _allow(
        r"backend/app/services/agent_tools(?:_(?:image_ops|temp_workspace|trigger_ops|web_ops|web_support))?\.py",
        rf"(?:{_legacy}-agent-|x-exa-integration.*{_legacy}|/sdk/{_legacy}\.js|window\.{_legacy}|\"/{_legacy}\")",
        "Temporary prefixes, integration IDs, SDK APIs, and the persisted CDN default path are compatibility identifiers.",
    ),
    _allow(
        r"backend/app/services/(?:wecom_stream|wechat_channel|resource_discovery|sandbox_mcp_host|mcp_client|media_playback)\.py",
        rf"(?:__{_legacy}_no_proxy_patch__|{_legacy}-wechat:|{_legacy}-mcp-admin|\"name\"\s*:\s*\"{_legacy}\"|{_legacy}_media_playback)",
        "Provider patch markers, client/session IDs, discovery identity, and playback cookie names are protocol state.",
    ),
    _allow(
        r"backend/app/services/(?:media_url_source|dingtalk_stream(?:_runner)?|document_conversion/(?:chrome_renderer|html_to_pdf))\.py",
        rf"(?:{_legacy}-(?:html-pdf|html-pptx|dingtalk-video|media-delivery|bg-capture-style|item-bg-capture-style)-?|data-{_legacy}-(?:item-id|slide-root)|{_legacy}(?:Chatbot|CardCallback)Handler)",
        "Temporary file prefixes, renderer DOM markers, and legacy handler class names are internal runtime identifiers.",
    ),
    _allow(
        r"backend/app/services/(?:agent_manager|media_tool_contract)\.py",
        rf"(?:{_legacy}-agent-|{_legacy}\.agent_(?:id|name)|x-{_legacy}-?)",
        "Container labels/names and stripped transport-header names are operational compatibility contracts.",
    ),
    _allow(
        r"backend/app/services/project_git_(?:service|core|mutations|repository)\.py",
        rf"(?:frozenset\(\{{\"{_legacy}\", \"{_legacy} project\"\}}\)|\.{_legacy}-(?:clone|write)-|{_legacy}-project-sandboxes|{_legacy}-Milestone-Operation)",
        "Project repositories retain historical author aliases, trailers, and internal staging-path prefixes.",
    ),
    _allow(
        r"backend/app/services/project_template_snapshot\.py",
        rf"{_legacy}-project-template-",
        "Template export uses an internal temporary-directory prefix.",
    ),
    _allow(
        r"backend/app/services/redis_lease_lock\.py",
        rf"{_legacy}:",
        "Distributed lease keys retain their rolling-upgrade namespace.",
    ),
    _allow(
        r"backend/app/services/provider_field_discovery\.py",
        rf"{_legacy}:field-sample:oauth:",
        "Provider discovery samples use an internal cross-process Redis namespace.",
    ),
    _allow(
        r"backend/app/services/sandbox/local/docker_backend\.py",
        rf"{_legacy}_IMAGE_MIRROR",
        "The sandbox registry environment variable is an operator contract.",
    ),
    _allow(
        r"backend/app/services/workload_capacity\.py",
        rf"{_legacy}_workload_capacity_",
        "Prometheus metric names are monitoring compatibility contracts.",
    ),
    _allow(
        r"backend/app/(?:core/logging_config\.py|api/upload\.py|services/cli_tools/state_storage\.py)",
        rf"(?:{_legacy}\.log|/tmp/{_legacy}_uploads|(?:runs as|! -user|chown)\s+{_legacy}\b|{_legacy}\s*==\s*gem)",
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
    _allow(
        r"backend/app/scripts/project_legacy_rollback\.py",
        rf"{_legacy}_(?:project_legacy|legacy_is_project_agent)",
        "The reversible project rollback role, lock, and classifier names are database compatibility contracts.",
    ),
    _allow(
        r"frontend/src/features/projects/(?:ProjectWorkspacePage\.tsx|projectWorkspace/gitPanels\.tsx)",
        rf"normalizedAuthor\s*!==\s*\"{_legacy}(?: project)?\"",
        "Historical Git author aliases are normalized before rendering.",
    ),
    _allow(
        r"frontend/src/features/projects/components/ProjectCodeEditor\.tsx",
        rf"(?:define{_legacy}Theme|{_legacy}-)",
        "Monaco theme IDs are internal editor registration keys.",
    ),
    _allow(
        r"frontend/src/features/projects/projectUserFacingCopy\.ts",
        rf"replace\(/\\b{_legacy}\\b/gi",
        "The dynamic-copy sanitizer matches and replaces the legacy keyword before rendering.",
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


def _matching_allowance(
    relative: str,
    line: str,
    keyword_match: re.Match[str],
) -> int | None:
    """Return the allowance covering this exact keyword occurrence, if any."""
    for index, allowance in enumerate(ALLOWANCES):
        if not allowance.path_pattern.fullmatch(relative):
            continue
        for allowed_match in allowance.line_pattern.finditer(line):
            if allowed_match.start() <= keyword_match.start() and keyword_match.end() <= allowed_match.end():
                return index
    return None


def _verify_occurrence_scoping() -> None:
    """Guard against accidentally restoring whole-line allowance behavior."""
    relative = "backend/app/api/websocket.py"
    line = 'cache_key = "clawith:project-subagent-web:id"; toast("Welcome to Clawith")'
    matches = list(KEYWORD_RE.finditer(line))
    if len(matches) != 2:
        raise RuntimeError("keyword scanner self-check fixture is invalid")
    if _matching_allowance(relative, line, matches[0]) is None:
        raise RuntimeError("internal compatibility identifier was not recognized")
    if _matching_allowance(relative, line, matches[1]) is not None:
        raise RuntimeError("user-visible keyword was incorrectly covered by an internal allowance")


def main() -> int:
    _verify_occurrence_scoping()
    violations: list[str] = []
    allowance_counts = [0] * len(ALLOWANCES)
    for path in _source_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for keyword_match in KEYWORD_RE.finditer(line):
                allowance_index = _matching_allowance(relative, line, keyword_match)
                if allowance_index is not None:
                    allowance_counts[allowance_index] += 1
                    continue
                violations.append(
                    f"{relative}:{line_number}:{keyword_match.start() + 1}: {line.strip()}"
                )

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
