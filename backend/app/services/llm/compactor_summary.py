from app.services.llm.compactor_shared import *  # noqa: F401,F403

_UUID_LIKE_RE = re.compile(
    r"\b[a-f0-9]{32}\b|\b[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}\b"
)
# Path regex: stops on whitespace, quotes, brackets, **and** common
# trailing sentence punctuation (.,;:!?) when followed by whitespace or
# end-of-string — `workspace/draft.md,` should match `workspace/draft.md`,
# not include the comma. Otherwise recall comparison breaks because
# the summary writes the path without the trailing punctuation.
_PATH_RE = re.compile(r"(?:^|\s)((?:/|\./|\.\./|[A-Za-z]:\\|workspace/|memory/|skills/)[^\s'\"<>]*?)(?=[\s,;:!?]|$)")
_ATTACHMENT_PATH_RE = re.compile(r"^路径：(.+)$", re.MULTILINE)
_URL_RE = re.compile(r"\bhttps?://[^\s'\"<>]*?(?=[\s,;!?]|$)", re.IGNORECASE)
_SLASH_COMMAND_RE = re.compile(
    r"(?<!\S)/(?:new|reset|help|commands|stop|thinking|think)(?:\s+(?:on|off|status))?(?=[\s,;:!?]|$)",
    re.IGNORECASE,
)
_KNOWN_SLASH_COMMANDS = {
    "/new", "/reset", "/help", "/commands", "/stop", "/thinking", "/think",
}
_HEADING_RE = re.compile(r"^#{2,3}\s", re.MULTILINE)
_CURRENT_OBJECTIVE_HEADING_RE = re.compile(
    r"^###\s+Current objective and progress\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_OPEN_ITEMS_HEADING_RE = re.compile(r"^###\s+Open items\s*$", re.MULTILINE | re.IGNORECASE)
_GOAL_LEDGER_HEADING_RE = re.compile(r"^###\s+Goal ledger\s*$", re.MULTILINE | re.IGNORECASE)
_GOAL_LEDGER_ACTIVE_RE = re.compile(r"^\s*-\s*Active\s*:", re.MULTILINE | re.IGNORECASE)
_GOAL_LEDGER_ACHIEVED_RE = re.compile(r"^\s*-\s*Achieved\s*:", re.MULTILINE | re.IGNORECASE)
_GOAL_LEDGER_UNFINISHED_RE = re.compile(
    r"^\s*-\s*Not achieved\s*/\s*blocked\s*:",
    re.MULTILINE | re.IGNORECASE,
)
_RELATED_TASK_HEADING_RE = re.compile(
    r"^###\s+Related task handoff\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_RELATED_TASK_SECTION_RE = re.compile(
    r"^###\s+Related task handoff\s*$\n(?P<body>.*?)(?=^###\s+|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_RELATED_TASK_REQUIRED_FIELDS = (
    ("task/project/focus item", re.compile(r"\b(?:task|project|focus item)(?:\s*/\s*(?:project|focus item))*\s*:", re.IGNORECASE)),
    ("owner", re.compile(r"\bowner\s*:", re.IGNORECASE)),
    ("status", re.compile(r"\bstatus\s*:", re.IGNORECASE)),
    ("completed evidence", re.compile(r"\bcompleted evidence\s*:", re.IGNORECASE)),
    ("remaining steps", re.compile(r"\bremaining steps?\s*:", re.IGNORECASE)),
    ("blockers", re.compile(r"\bblockers?\s*:", re.IGNORECASE)),
    ("next action", re.compile(r"\bnext action\s*:", re.IGNORECASE)),
    ("archive/file paths", re.compile(r"\barchive\s*/\s*file paths?\s*:", re.IGNORECASE)),
)
_ROLE_BLOCK_RE = re.compile(
    r"^###\s+\[(?P<role>user|assistant|tool_call|system)\]\s+@[^\n]*\n"
    r"(?P<body>.*?)(?=^###\s+\[(?:user|assistant|tool_call|system)\]\s+@|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_OBJECTIVE_SECTION_RE = re.compile(
    r"(^###\s+Current objective and (?:progress|constraints)\s*$)(?P<body>.*?)(?=^###\s+|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_CONTINUITY_SECTIONS = (
    "Current objective and constraints",
    "Progress and key context",
    "Next actions and blockers",
    "Necessary references",
)


def _objective_evidence_items(original_text: str) -> list[str]:
    source = str(original_text or "")
    user_blocks = [
        match.group("body").strip()
        for match in _ROLE_BLOCK_RE.finditer(source)
        if match.group("role").lower() == "user" and match.group("body").strip()
    ]
    sections = [m.group("body").strip() for m in _OBJECTIVE_SECTION_RE.finditer(source)]
    candidates: list[str] = []
    if sections:
        prior = re.sub(
            r"^\s*-\s*Verbatim latest objective evidence:\s*",
            "",
            sections[-1],
            flags=re.IGNORECASE,
        ).strip()
        if prior:
            # Canonical evidence from a prior epoch is a list of E<n> lines.
            # Parse those values instead of wrapping the whole block in another
            # P label; repeated compaction is therefore idempotent rather than
            # growing ``P: P: P: ...`` until the original goal is truncated.
            prior_items = [
                match.group("body").strip()
                for match in re.finditer(
                    r"(?:^|\n)E\d+:\s*(?P<body>.*?)(?=(?:\nE\d+:)|\Z)",
                    prior,
                    re.DOTALL,
                )
            ]
            candidates.extend(prior_items or [prior])
    candidates.extend(user_blocks[-OBJECTIVE_EVIDENCE_USER_INPUTS:])
    if not candidates and source.strip():
        candidates = [source.strip()]

    normalized: list[str] = []
    for item in candidates:
        value = re.sub(r"\s+", " ", item).strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _selected_objective_evidence_items(
    original_text: str,
    evidence_items: list[str] | None = None,
) -> tuple[list[str], bool]:
    if evidence_items is None:
        normalized = _objective_evidence_items(original_text)
    else:
        normalized = []
        for item in evidence_items:
            value = re.sub(r"\s+", " ", str(item or "")).strip()
            if value and value not in normalized:
                normalized.append(value)
    item_overflow = len(normalized) > OBJECTIVE_EVIDENCE_MAX_ITEMS
    if item_overflow:
        keep_each_side = OBJECTIVE_EVIDENCE_MAX_ITEMS // 2
        normalized = normalized[:keep_each_side] + normalized[-keep_each_side:]
    return normalized, item_overflow


def extract_objective_evidence(
    original_text: str,
    *,
    max_chars: int = OBJECTIVE_EVIDENCE_MAX_CHARS,
    evidence_items: list[str] | None = None,
) -> str:
    """Return bounded verbatim evidence from recent user inputs and prior goal.

    This extraction is deliberately content-agnostic: no intent keywords,
    language rules, or semantic guesses. The companion truncation predicate
    forces a lossless archive whenever this bounded view cannot prove that the
    complete input survived.
    """
    normalized, _item_overflow = _selected_objective_evidence_items(
        original_text,
        evidence_items,
    )
    if not normalized:
        return ""
    labels = [f"E{index + 1}: " for index in range(len(normalized))]
    usable = max(1, max_chars - sum(len(label) for label in labels) - max(0, len(labels) - 1))
    per_item = max(1, usable // len(normalized))
    rendered: list[str] = []
    for rendered_label, item in zip(labels, normalized, strict=True):
        if len(item) > per_item:
            if per_item <= len(" … ") + 2:
                item = item[:per_item]
            else:
                half = (per_item - len(" … ")) // 2
                item = f"{item[:half]} … {item[-half:]}"
        rendered.append(rendered_label + item)
    return "\n".join(rendered)[:max_chars]


def objective_evidence_is_truncated(
    original_text: str,
    *,
    max_chars: int,
    evidence_items: list[str] | None = None,
) -> bool:
    normalized, item_overflow = _selected_objective_evidence_items(
        original_text,
        evidence_items,
    )
    if item_overflow:
        return True
    if not normalized:
        return False
    labels = [f"E{index + 1}: " for index in range(len(normalized))]
    usable = max(1, max_chars - sum(len(label) for label in labels) - max(0, len(labels) - 1))
    per_item = max(1, usable // len(normalized))
    return any(len(item) > per_item for item in normalized)


def pin_summary_objective(
    *,
    summary: str,
    original_text: str,
    max_tokens: int = 2_000,
    objective_evidence_items: list[str] | None = None,
) -> str:
    """Replace model-written objective prose with bounded verbatim evidence."""
    rendered = (summary or "").strip()
    evidence = extract_objective_evidence(
        original_text,
        max_chars=objective_evidence_limit(max_tokens),
        evidence_items=objective_evidence_items,
    )
    if not rendered or not evidence:
        return rendered
    replacement = (
        "### Current objective and progress\n"
        f"- Verbatim latest objective evidence: {evidence}\n\n"
    )
    if _OBJECTIVE_SECTION_RE.search(rendered):
        return _OBJECTIVE_SECTION_RE.sub(replacement, rendered, count=1).strip()
    return rendered


def extract_preserved_identifiers(text: str) -> set[str]:
    """Extract identifiers that must survive a conversation compaction."""
    source = text or ""
    identifiers = set(_UUID_LIKE_RE.findall(source))
    identifiers.update(_URL_RE.findall(source))
    identifiers.update(match.group(1) for match in _PERSISTED_SAVED_PATH_RE.finditer(source))
    identifiers.update(match.group(0) for match in _SLASH_COMMAND_RE.finditer(source))
    identifiers.update(
        match.group(1).strip() for match in _ATTACHMENT_PATH_RE.finditer(source) if match.group(1).strip()
    )

    for match in _PATH_RE.finditer(source):
        candidate = match.group(1)
        normalized = candidate.lower()
        # Slash commands have their own semantic class.  Also discard regex
        # artifacts such as a bare slash or JSON's escaped-newline prefix.
        if normalized in _KNOWN_SLASH_COMMANDS or candidate == "/" or candidate.startswith("/\\"):
            continue
        identifiers.add(candidate)
    return identifiers


def append_missing_identifiers(*, summary: str, original_text: str) -> tuple[str, list[str]]:
    """Mechanically preserve exact identifiers the summary model omitted."""
    rendered = (summary or "").strip()
    missing = sorted(
        identifier for identifier in extract_preserved_identifiers(original_text) if identifier not in rendered
    )
    if not missing:
        return rendered, []

    appendix = "\n".join(["### Preserved identifiers", *(f"- {identifier}" for identifier in missing)])
    return f"{rendered}\n\n{appendix}".strip(), missing


def validate_summary(
    *,
    summary: str,
    original_text: str,
    max_tokens: int,
    objective_text: str | None = None,
    objective_evidence_max_chars: int | None = None,
    objective_evidence_items: list[str] | None = None,
    objective_archived: bool = False,
) -> tuple[bool, str | None, float]:
    """Validate that a generated handoff is loadable and structurally complete.

    Semantic truth cannot be proven by substring or identifier recall. The
    third return value remains for compatibility and is always ``1.0`` after
    structural success.
    """
    s = (summary or "").strip()

    # Markdown emphasis does not change a section/field's meaning. Normalize
    # only the structural view; exact evidence and identifiers use the original.
    structure = s.replace("**", "").replace("__", "")
    if len(_HEADING_RE.findall(structure)) < len(_CONTINUITY_SECTIONS):
        return False, "missing_section_headings", 0.0
    for title in _CONTINUITY_SECTIONS:
        section = re.search(
            rf"^###\s+{re.escape(title)}\s*$\n(?P<body>.*?)(?=^###\s+|\Z)",
            structure,
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        if section is None:
            return False, f"missing_section:{title.lower().replace(' ', '_')}", 0.0
        if not section.group("body").strip():
            return False, f"empty_section:{title.lower().replace(' ', '_')}", 0.0

    return True, None, 1.0


def build_deterministic_summary(
    *,
    source_text: str,
    max_tokens: int,
    archive_envelope: str | None = None,
    objective_evidence_items: list[str] | None = None,
) -> str:
    """Build bounded recovery material after generation cannot be trusted."""
    saved_path = (
        _PERSISTED_SAVED_PATH_RE.search(archive_envelope or "")
        if archive_envelope
        else None
    )
    path = saved_path.group(1) if saved_path else "unavailable"
    archive_reference = (
        "<persisted-output>\n"
        f"Full output saved to: {path}\n"
        "</persisted-output>"
        if saved_path
        else "- Original compaction history: unavailable."
    )
    objective_evidence = extract_objective_evidence(
        source_text,
        max_chars=2_400,
        evidence_items=objective_evidence_items,
    )
    objective_candidates = (
        "- Unverified original instruction candidates follow; reconcile later changes in the archive:\n"
        + objective_evidence
        if objective_evidence
        else "- No bounded objective candidate was available; consult the archive."
    )
    header = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and constraints\n"
        "- Degraded recovery: the current objective could not be verified from a semantic summary. "
        "Consult the archived original history before acting.\n"
        f"{objective_candidates}\n\n"
        "### Progress and key context\n"
        "- The excerpts below are original source material, not a verified completion record.\n"
    )
    footer = (
        "\n\n### Next actions and blockers\n"
        "- Reconcile the current objective, authorization, and execution status from the archived "
        "history before continuing or repeating any external action.\n\n"
        "### Necessary references\n"
        f"{archive_reference}\n"
        "- This recovery text is bounded and may omit context; the archive is authoritative."
    )
    available = max(0, DETERMINISTIC_SUMMARY_MAX_CHARS - len(header) - len(footer))
    lines = [line.strip() for line in (source_text or "").splitlines() if line.strip()]
    excerpts: list[str] = []
    used = 0
    for line in reversed(lines):
        rendered = f"- {line[:600]}"
        if used + len(rendered) + 1 > available:
            continue
        excerpts.append(rendered)
        used += len(rendered) + 1
    excerpts.reverse()
    if not excerpts:
        excerpts = ["- No inline excerpt fits; use the archived original history."]
    return header + "\n".join(excerpts) + footer


def append_lossless_archive_reference(*, summary: str, relative_path: str) -> str:
    """Attach the exact archive created for this compaction attempt.

    Keep this reference intentionally small: the archive contains the full
    source, while copying its preview back into the summary would spend the
    context we just recovered.  The path is agent-readable and the note makes
    the continuity contract explicit to the next turn.
    """
    section = (
        "### Lossless continuity archive\n"
        "- The complete source replaced by this compaction is retained here. "
        "Consult it when exact requirements or omitted execution details are needed.\n"
        "<persisted-output>\n"
        f"Full output saved to: {relative_path}\n"
        "</persisted-output>"
    )
    return f"{(summary or '').strip()}\n\n{section}".strip()

__all__ = (
    '_UUID_LIKE_RE',
    '_PATH_RE',
    '_ATTACHMENT_PATH_RE',
    '_URL_RE',
    '_SLASH_COMMAND_RE',
    '_KNOWN_SLASH_COMMANDS',
    '_HEADING_RE',
    '_CURRENT_OBJECTIVE_HEADING_RE',
    '_OPEN_ITEMS_HEADING_RE',
    '_GOAL_LEDGER_HEADING_RE',
    '_GOAL_LEDGER_ACTIVE_RE',
    '_GOAL_LEDGER_ACHIEVED_RE',
    '_GOAL_LEDGER_UNFINISHED_RE',
    '_RELATED_TASK_HEADING_RE',
    '_RELATED_TASK_SECTION_RE',
    '_RELATED_TASK_REQUIRED_FIELDS',
    '_ROLE_BLOCK_RE',
    '_OBJECTIVE_SECTION_RE',
    '_objective_evidence_items',
    '_selected_objective_evidence_items',
    'extract_objective_evidence',
    'objective_evidence_is_truncated',
    'pin_summary_objective',
    'extract_preserved_identifiers',
    'append_missing_identifiers',
    'validate_summary',
    'build_deterministic_summary',
    'append_lossless_archive_reference',
)
