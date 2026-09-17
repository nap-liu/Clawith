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
    r"(^###\s+Current objective and progress\s*$)(?P<body>.*?)(?=^###\s+|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
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
    """Return ``(passed, fail_reason, uuid_recall_ratio)``.

    Structure is a hard gate; the UUID/path recall metric is
    a quality signal.
    """
    s = (summary or "").strip()

    # Markdown emphasis does not change a section/field's meaning. Normalize
    # only the structural view; exact evidence and identifiers use the original.
    structure = s.replace("**", "").replace("__", "")
    if len(_HEADING_RE.findall(structure)) < 2:
        return False, "missing_section_headings", 0.0
    if not _CURRENT_OBJECTIVE_HEADING_RE.search(structure):
        return False, "missing_current_objective_section", 0.0
    if not _GOAL_LEDGER_HEADING_RE.search(structure):
        return False, "missing_goal_ledger_section", 0.0
    if not _GOAL_LEDGER_ACTIVE_RE.search(structure):
        return False, "missing_active_goal", 0.0
    if not _GOAL_LEDGER_ACHIEVED_RE.search(structure):
        return False, "missing_achieved_goals", 0.0
    if not _GOAL_LEDGER_UNFINISHED_RE.search(structure):
        return False, "missing_unfinished_goals", 0.0
    if not _RELATED_TASK_HEADING_RE.search(structure):
        return False, "missing_related_task_handoff", 0.0
    related_match = _RELATED_TASK_SECTION_RE.search(structure)
    related_body = related_match.group("body").strip() if related_match else ""
    if not re.search(r"^\s*-\s*None evidenced[.!。]?\s*$", related_body, re.MULTILINE | re.IGNORECASE):
        missing_fields = [
            label
            for label, pattern in _RELATED_TASK_REQUIRED_FIELDS
            if not pattern.search(related_body)
        ]
        if missing_fields:
            return False, "incomplete_related_task_handoff:" + ",".join(missing_fields), 0.0
    if not _OPEN_ITEMS_HEADING_RE.search(structure):
        return False, "missing_open_items_section", 0.0

    # UUID + path recall: how much of what was in the original made it
    # into the summary, as a proxy for "didn't lose critical IDs".
    original_ids = extract_preserved_identifiers(original_text)
    ratio = 1.0
    if original_ids:
        recalled = sum(1 for ident in original_ids if ident in s)
        ratio = recalled / len(original_ids)
        if ratio < UUID_RECALL_THRESHOLD:
            return (
                False,
                f"low_id_recall ({recalled}/{len(original_ids)} = {ratio:.2f} < {UUID_RECALL_THRESHOLD})",
                ratio,
            )

    evidence = extract_objective_evidence(
        objective_text if objective_text is not None else original_text,
        max_chars=(
            objective_evidence_max_chars
            if objective_evidence_max_chars is not None
            else objective_evidence_limit(max_tokens)
        ),
        evidence_items=objective_evidence_items,
    )
    objective_match = _OBJECTIVE_SECTION_RE.search(s)
    objective_body = objective_match.group("body") if objective_match else ""
    if evidence and evidence not in objective_body and not objective_archived:
        return False, "objective_evidence_missing", ratio

    return True, None, ratio


def build_deterministic_summary(
    *,
    source_text: str,
    max_tokens: int,
    archive_envelope: str | None = None,
    objective_evidence_items: list[str] | None = None,
) -> str:
    """Build a valid, bounded summary without depending on an LLM response.

    This is a last-resort availability path, not the normal summarizer. The
    complete source is materialized first and its readable path is embedded in
    the result; excerpts improve immediate continuity but are never the only
    surviving copy of the compacted facts.
    """
    max_chars = DETERMINISTIC_SUMMARY_MAX_CHARS
    archive_reference = ""
    if archive_envelope:
        saved_path = _PERSISTED_SAVED_PATH_RE.search(archive_envelope)
        if saved_path:
            archive_reference = (
                "<persisted-output>\n"
                f"Full output saved to: {saved_path.group(1)}\n"
                "</persisted-output>"
            )
    objective_evidence = extract_objective_evidence(
        source_text,
        max_chars=objective_evidence_limit(max_tokens),
        evidence_items=objective_evidence_items,
    )
    def _fixed(evidence: str) -> str:
        rendered = (
            "## Summary of earlier conversation\n\n"
            "### Current objective and progress\n"
            f"- {evidence}\n\n"
            "### Goal ledger\n"
            f"- Active: {evidence}\n"
            "- Achieved: Only goals explicitly evidenced as completed in the excerpts/archive; "
            "no additional completion is inferred.\n"
            "- Not achieved / blocked: Preserve every unresolved or blocked goal from the "
            "excerpts/archive for continuation.\n\n"
            "### Related task handoff\n"
            "- Task/project/focus item: Preserve exact evidenced name/ID, or none evidenced.\n"
            "- Owner: Preserve evidenced owner, or not evidenced.\n"
            "- Status: Active unless the source explicitly proves another status.\n"
            "- Completed evidence: Only completion explicitly present in the excerpts/archive.\n"
            "- Remaining steps: Preserve unresolved steps from the excerpts/archive.\n"
            "- Blockers: Preserve evidenced blockers, or none evidenced.\n"
            "- Next action: Continue the active objective using the excerpts/archive.\n"
            f"- Archive/file paths: {saved_path.group(1) if archive_envelope and saved_path else 'none evidenced'}.\n\n"
            "### Key facts and chronology\n"
        )
        if archive_reference:
            rendered += (
                "- Archive below.\n\n"
                "### Lossless continuity archive\n"
                f"{archive_reference}\n"
            )
        return rendered

    fixed = _fixed(
        objective_evidence or "Continue from recent turns."
    )
    closing = (
        "\n\n### Open items\n"
        "- Continue; use the archive if needed."
    )
    if len(fixed + closing) > max_chars:
        fixed = _fixed("Continue from recent turns.")
    available = max(0, max_chars - len(fixed) - len(closing))
    cleaned_lines = [line.strip() for line in (source_text or "").splitlines() if line.strip()]
    selected: list[str] = []
    used = 0
    # Alternate old and new material so a large middle block cannot erase the
    # beginning or the latest state of the compacted span.
    ordered: list[str] = []
    left, right = 0, len(cleaned_lines) - 1
    while left <= right:
        ordered.append(cleaned_lines[left])
        left += 1
        if left <= right:
            ordered.append(cleaned_lines[right])
            right -= 1
    for line in ordered:
        excerpt = line[:600]
        rendered = f"- {excerpt}"
        candidate_lines = [*selected, rendered]
        candidate_summary = fixed + "\n".join(candidate_lines) + closing
        if used + len(rendered) + 1 > available:
            continue
        selected.append(rendered)
        used += len(rendered) + 1
    if not selected and available:
        placeholder = "- Earlier conversation was compacted; consult the lossless archive when needed."
        candidate = placeholder[:available]
        if len(fixed + candidate + closing) <= max_chars:
            selected.append(candidate)

    summary = fixed + "\n".join(selected) + closing
    missing_identifiers = sorted(
        identifier
        for identifier in extract_preserved_identifiers(source_text)
        if identifier not in summary
    )
    if missing_identifiers:
        appendix = "\n\n### Preserved identifiers"
        for identifier in missing_identifiers:
            candidate = f"{appendix}\n- {identifier}" if appendix else f"\n- {identifier}"
            candidate_summary = summary + candidate
            if len(candidate_summary) > max_chars:
                break
            summary = candidate_summary
            appendix = ""

    return summary


def append_lossless_archive_reference(*, summary: str, relative_path: str) -> str:
    """Attach the exact archive created for this compaction attempt.

    Keep this reference intentionally small: the archive contains the full
    source, while copying its preview back into the summary would spend the
    context we just recovered.  The path is agent-readable and the note makes
    the continuity contract explicit to the next turn.
    """
    section = (
        "### Lossless continuity archive\n"
        "- Some source content exceeded the bounded inline summary view. "
        "The complete, unabridged source for this compaction "
        "must be consulted when exact omitted requirements are needed.\n"
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
