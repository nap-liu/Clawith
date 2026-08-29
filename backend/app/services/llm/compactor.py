"""Conversation auto-compaction.

When a session's running prompt-token count approaches the model's
``context_window``, this module:

1. Decides whether to fire from the provider's actual input usage returned by
   the preceding LLM round.
2. Acquires a per-session async lock so concurrent channels don't
   compact the same session twice.
3. Selects a span of messages to compact, aligned to round / tool-pair
   boundaries so we never split an assistant→tool→tool_result group.
4. Sends the complete selected span to the summary LLM. The platform does not
   estimate this input locally; an explicit provider rejection falls through
   to the deterministic lossless archive path.
5. Calls the same primary LLM with a structured-summary system
   prompt; caps output via ``LLMModel.compact_summary_max_tokens``.
6. Validates the summary (length / structure / UUID-and-path recall
   ≥ 0.7). A failed model summary gets one repair attempt, then a
   deterministic local summary, so model formatting failures cannot
   disable compaction. The first failure reason is retained for audit.
7. Writes ``chat_compactions`` + flags ``ChatMessage.compacted_into``
   inside a single transaction, supersedes the prior epoch's marker
   for this session, and reports back through ``CompactionResult``.

The companion change in ``chat_history.py`` makes the next history
load skip the flagged rows and inject the summary in their place, so
the new prefix is byte-stable for prompt-cache hits from the next
round onward.

dryrun mode: when ``CLAWITH_COMPACT_DRYRUN=true`` is set in the
environment, every step runs (including the summary LLM call and the
``chat_compactions`` write) **except** the ``ChatMessage.compacted_into``
flag — observe summary quality without disturbing live conversations.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.services.chat_attachments import (
    normalize_chat_message_attachments,
    render_attachment_context,
)
from app.services.chat_history import _batch_load_display_names
from app.services.llm.turn_partition import MIN_PROTECTED_RECENT_TURNS, partition_turns
from app.services.sender_attribution import wrap_with_sender

# ─── Configuration constants ─────────────────────────────────────────

# Summary generation is an availability mechanism on the critical turn path.
# It must not consume the ordinary model timeout twice before the deterministic
# lossless fallback can recover the session.
ARCHIVE_WRITE_TIMEOUT_SECONDS = 60.0

# Per-session compaction lock TTL. Generous enough to cover even a
# slow summary LLM call; the watchdog renews mid-flight if needed.
COMPACT_LOCK_TTL_SECONDS = 120

# Validation gate — reject summaries that don't recall enough of the
# IDs and file paths from the original span. 0.7 is a starting point;
# we'll tune after observing the dryrun distribution for a week.
UUID_RECALL_THRESHOLD = 0.7
MIN_SUMMARY_CHARS = 200
MAX_SUMMARY_STORAGE_CHARS = 24_000
DETERMINISTIC_SUMMARY_MAX_CHARS = 12_000
OBJECTIVE_EVIDENCE_MAX_CHARS = 800
OBJECTIVE_EVIDENCE_USER_INPUTS = 3
OBJECTIVE_EVIDENCE_MAX_ITEMS = 6


def objective_evidence_limit(max_tokens: int) -> int:
    # Storage/display bound only; never derive model tokens from characters.
    return OBJECTIVE_EVIDENCE_MAX_CHARS

# Futility floor: minimum estimated token mass the selected span must
# carry for compaction to be worth running. When the trigger fires but
# the compactable history is tiny, the prompt is dominated by
# non-compressible content (tool schemas, system prompt) that
# compaction cannot touch — running the summary LLM would block the
# turn for tens of seconds every round while never lowering the ratio.
MIN_COMPACTABLE_SPAN_TOKENS = 2000

# Vision payloads are transported as base64, but the encoded bytes are not
# language tokens.  Use one provider-neutral conservative placeholder for both
# legacy string markers and structured multimodal image blocks.

# Summary LLM gets this prompt verbatim. Wording is deliberate — the
# "preserve verbatim" + "drop pleasantries" structure is what keeps
# the gate's UUID-recall metric high.
SUMMARY_SYSTEM_PROMPT = """\
You will receive a chat segment that occurred earlier in this conversation.
Compress it into a structured summary that the same agent can use to
continue the conversation seamlessly.

PRESERVE VERBATIM (no paraphrase, no summary):
- All file paths, URLs, document IDs, agent IDs, session IDs
- All quantitative values: counts, byte sizes, percentages, timestamps
- All proper names: people, products, projects
- All commands or queries that were issued (tool name + key arguments)
- All open action items or commitments
- All errors, exceptions, and failure modes encountered
- The speaker/Agent/role responsible for each material finding or commitment
- Role-specific judgments, challenged assumptions, disagreements and unresolved dissent
- Evidence attribution, decision rationale, trade-offs, next actions and their owners
- All <persisted-output> envelope metadata (path + size) so the agent
  knows it can read_file these resources later

DROP:
- Pleasantries, transitions, restated context
- Redundant repetitions of facts already captured
- Tool result text bodies that are no longer relevant to the trailing
  conversation (the trailing rounds are still in the active window)
- Long content blocks that have already been materialized to disk
  (anything in <persisted-output> envelopes — keep the metadata only)

OUTPUT FORMAT:
## Summary of earlier conversation

### Current objective and progress
- State the latest active user objective, completed progress, current blocker,
  and the exact next action. Do not replace it with an older objective.

### Goal ledger
- Active: the goal currently being advanced and its next action.
- Achieved: every earlier goal explicitly shown as completed, with evidence.
- Not achieved / blocked: every earlier goal still incomplete, abandoned, or
  blocked, with the blocker. Never infer completion without evidence.

### Related task handoff
- For each related item use these exact fields: Task/project/focus item,
  Owner, Status, Completed evidence, Remaining steps, Blockers, Next action,
  Archive/file paths. Preserve exact names/IDs and paths. Write one bullet
  "- None evidenced" only when the source truly contains no related task.

### Key facts
- ...

### Decisions made
- ...

### Files / paths / IDs referenced
- ...

### Tool calls performed
- <tool_name>(<key_args>) → <terse_result>

### Open items
- ...

DO NOT add commentary outside this structure. Stop after the last bullet.
"""


# Process-wide locks per session, holding off concurrent compactions
# from multiple channels (web + IM both connected at once).
_session_locks: dict[str, asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()


# ─── Public types ────────────────────────────────────────────────────


@dataclass
class CompactionResult:
    """Outcome of a ``maybe_compact`` call.

    When ``triggered`` is True the caller should rebuild its in-memory
    ``api_messages`` from the DB via the compaction-aware
    ``chat_history.load_history_for_llm`` — the new prefix will replace
    the compacted span with the summary message and stay byte-stable
    for prompt-cache hits from the next round forward.
    """

    triggered: bool
    # True means the configured threshold was crossed and compaction was
    # required for a safe provider request.  ``required and not triggered`` is
    # therefore a terminal failure, not the ordinary below-threshold no-op.
    required: bool = False
    summary_id: uuid.UUID | None = None
    epoch: int | None = None
    summary_tokens: int | None = None
    trigger_prompt_tokens: int | None = None
    skipped_reason: str | None = None
    progress_notice: str | None = None


COMPACTION_NOT_APPLICABLE_REASONS = frozenset(
    {
        "span_too_small_to_be_worth_compacting",
        "span_mass_too_small_to_matter",
    }
)


class ContextRecoveryMessages(list):
    """Reloaded durable view plus a typed preflight no-op classification."""

    def __init__(self, values=(), *, preflight_not_applicable: bool = False):
        super().__init__(values)
        self.preflight_not_applicable = preflight_not_applicable


# ─── Trigger decision ────────────────────────────────────────────────


def prompt_exceeds_preflight_limit(*, model: LLMModel, prompt_messages: list[dict]) -> bool:
    """Deprecated: no official model counter is available in this sync API."""
    return False


def should_compact(
    *,
    model: LLMModel,
    last_prompt_tokens: int | None,
    pre_flight_estimate: int | None,
) -> tuple[bool, float, str]:
    """Pure decision: returns ``(should_fire, observed_ratio, reason)``.

    ``observed_ratio`` is recorded on the compaction row for later
    analysis; ``reason`` is a short tag — ``post_round`` /
    ``pre_flight`` / ``below_threshold``.
    """
    from app.services.llm.client import get_max_tokens
    from app.services.llm.context_budget import resolve_context_budget

    max_output_tokens = get_max_tokens(
        str(getattr(model, "provider", "") or ""),
        str(getattr(model, "model", "") or ""),
        getattr(model, "max_output_tokens", None),
    )
    budget = resolve_context_budget(model, max_output_tokens=max_output_tokens)
    if not budget.configured or budget.input_capacity <= 0:
        return False, 0.0, "no_context_window_configured"

    if last_prompt_tokens is not None:
        ratio = last_prompt_tokens / budget.input_capacity
        if last_prompt_tokens >= budget.compaction_trigger_limit:
            return True, ratio, "post_round"

    # ``pre_flight_estimate`` is retained only for call-site compatibility.
    # A local heuristic is not a model token count and cannot trigger a
    # capacity transition.
    visible_ratio = 0.0
    if last_prompt_tokens is not None:
        visible_ratio = max(visible_ratio, last_prompt_tokens / budget.input_capacity)
    return False, visible_ratio, "below_threshold"


# ─── Span selection ──────────────────────────────────────────────────


def _is_round_boundary_after(rows: list[ChatMessage], idx: int) -> bool:
    """Whether splitting *after* ``rows[idx]`` lands on a round
    boundary — i.e. ``rows[idx]`` is the final message of a complete
    user/assistant exchange (with all tool sub-messages closed).

    A round ends at:
    * an assistant message with **no** ``tool_call`` role following it
      (the assistant gave a final reply, not a tool request waiting on
      a result), OR
    * the very last row (we are caught up to "now").

    The chat_messages role enum is {user, assistant, system, tool_call}
    where ``tool_call`` rows store JSON capturing the tool call AND its
    result in one row (per websocket.py rehydration logic). So a
    round boundary is right before the next ``user`` message.
    """
    if idx >= len(rows) - 1:
        return True
    next_role = rows[idx + 1].role
    return next_role == "user"


def select_compaction_span(
    rows: list[ChatMessage],
    keep_recent_turns: int,
) -> tuple[int, int] | None:
    """Pick a continuous prefix of complete turns for compaction.

    At least two recent complete turns and the newer tail remain byte-for-byte
    active. Older abandoned/ambiguous runs may enter the lossless archive but
    are never resumed or re-executed. Token mass, not row count, decides whether
    the selected prefix is worth summarising.
    """
    compactable = partition_turns(
        rows,
        keep_recent_turns=keep_recent_turns,
    ).compactable_rows
    if not compactable:
        return None
    return (0, len(compactable) - 1)


# ─── Pre-filtering ───────────────────────────────────────────────────


_PERSISTED_ENVELOPE_RE = re.compile(
    r"<persisted-output[^>]*>.*?</persisted-output>",
    re.DOTALL,
)
_PERSISTED_HEADER_RE = re.compile(r"<persisted-output[^>]*>")
_PERSISTED_SAVED_PATH_RE = re.compile(
    r"Full output saved to:\s*([^\s<>]+)",
    re.IGNORECASE,
)
_PERSISTED_TRUNCATION_LINE_RE = re.compile(r"^TRUNCATED:[^\n]*", re.MULTILINE)
_LARGE_BODY_HEAD_TAIL_THRESHOLD = 4000  # chars
_LARGE_BODY_KEEP_HEAD = 800
_LARGE_BODY_KEEP_TAIL = 800


def _elide_materialized_output_bodies(content: str) -> str:
    """Drop only bodies whose complete source already has a durable path."""
    if not isinstance(content, str):
        return content

    def _replace_envelope(m: re.Match) -> str:
        # The current persisted-output format keeps its path and size inside
        # the envelope body, not as tag attributes. Preserve that metadata
        # explicitly while dropping only the large inline preview.
        envelope = m.group(0)
        head = _PERSISTED_HEADER_RE.search(envelope)
        saved_path = _PERSISTED_SAVED_PATH_RE.search(envelope)
        truncation = _PERSISTED_TRUNCATION_LINE_RE.search(envelope)
        legacy_header_has_path = bool(
            head
            and re.search(r"\bpath=[\"'][^\"']+[\"']", head.group(0))
            and re.search(r"\bsize=[\"'][^\"']+[\"']", head.group(0))
        )
        # A literal tag is ordinary user/model/tool text unless the envelope
        # carries the platform's durable recovery metadata. This helper is
        # also called only for typed tool-call rows by the serializer below.
        if not ((saved_path and truncation) or legacy_header_has_path):
            return envelope
        if head:
            metadata = [head.group(0)]
            if truncation:
                metadata.append(truncation.group(0))
            if saved_path:
                metadata.append(f"Full output saved to: {saved_path.group(1)}")
            metadata.append("…[inline preview omitted; full body materialized to disk]…")
            metadata.append("</persisted-output>")
            return "\n".join(metadata)
        return "<persisted-output>…[materialized]…</persisted-output>"

    return _PERSISTED_ENVELOPE_RE.sub(_replace_envelope, content)


def prefilter_message_content(
    content: str,
    *,
    allow_materialized_elision: bool = False,
) -> str:
    """Shrink one message body while retaining durable recovery metadata."""
    out = (
        _elide_materialized_output_bodies(content)
        if allow_materialized_elision
        else content
    )

    if len(out) > _LARGE_BODY_HEAD_TAIL_THRESHOLD:
        head = out[:_LARGE_BODY_KEEP_HEAD]
        tail = out[-_LARGE_BODY_KEEP_TAIL:]
        out = f"{head}\n\n…[truncated {len(out) - _LARGE_BODY_KEEP_HEAD - _LARGE_BODY_KEEP_TAIL} chars]…\n\n{tail}"

    return out


def serialize_span_for_summary(
    rows: list[ChatMessage],
    *,
    prefilter: bool = True,
    wrap_user_names: bool = False,
    name_map: dict[uuid.UUID, str] | None = None,
    elide_durable_only: bool = False,
) -> str:
    """Render the compaction span as a single text blob for the
    summary LLM. Each row gets a role-tagged block; content is
    pre-filtered to drop bulk that won't help the summary anyway.
    """
    chunks: list[str] = []
    for r in rows:
        body = r.content or ""
        raw_meta = getattr(r, "message_meta", None)
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        delivery = meta.get("delivery") if isinstance(meta.get("delivery"), dict) else {}
        recall = delivery.get("recall") if isinstance(delivery.get("recall"), dict) else {}
        if r.role in {"assistant", "tool_call"} and recall.get("status") == "recalled":
            body = "[该消息已撤回，不应视为仍对用户可见]"
        durable_tool_output = False
        if r.role == "tool_call":
            try:
                tool_payload = json.loads(body or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                tool_payload = None
            if isinstance(tool_payload, dict) and tool_payload.get("status") == "done":
                result = tool_payload.get("result")
                durable_tool_output = bool(
                    isinstance(result, str)
                    and result != _elide_materialized_output_bodies(result)
                )
        if r.role == "user":
            body, attachments = normalize_chat_message_attachments(
                body,
                meta,
                meta.get("source_channel"),
            )
            body = render_attachment_context(
                prefilter_message_content(body) if prefilter else body,
                attachments,
            )
            sender_user_id = getattr(r, "sender_user_id", None) or getattr(r, "user_id", None)
            if wrap_user_names and sender_user_id is not None:
                body = wrap_with_sender(
                    body,
                    sender_user_id,
                    (name_map or {}).get(sender_user_id),
                )
        elif prefilter:
            body = prefilter_message_content(
                body,
                allow_materialized_elision=durable_tool_output,
            )
        elif elide_durable_only and durable_tool_output:
            body = _elide_materialized_output_bodies(body)
        chunks.append(f"### [{r.role}] @ {r.created_at.isoformat()}\n{body}")
    return "\n\n".join(chunks)


async def _load_summary_sender_attribution(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    rows: list[ChatMessage],
) -> tuple[bool, dict[uuid.UUID, str]]:
    """Resolve the same group-only sender tags used by normal LLM history.

    P2P identity remains session-scoped and therefore must not gain per-message
    sender tags during compaction. Group identity is message-scoped; preserve
    the canonical ``sender_user_id`` even when display-name lookup is degraded.
    """
    try:
        session_id = uuid.UUID(str(conversation_id))
        session = await db.get(ChatSession, session_id)
    except (AttributeError, TypeError, ValueError):
        return False, {}

    if session is None or session.agent_id != agent_id or not session.is_group:
        return False, {}

    user_ids = {
        sender_user_id
        for row in rows
        if row.role == "user"
        and (
            sender_user_id := (
                getattr(row, "sender_user_id", None)
                or getattr(row, "user_id", None)
            )
        ) is not None
    }
    try:
        return True, await _batch_load_display_names(db, user_ids)
    except Exception as exc:  # noqa: BLE001 - display names degrade to stable ids
        logger.warning(
            "[compactor] group sender display-name lookup failed "
            f"session={conversation_id}: {type(exc).__name__}: {exc}"
        )
        # ``wrap_with_sender`` renders Unknown while retaining the stable user id.
        return True, {}


def objective_evidence_items_from_rows(
    *,
    prior_summary: str | None,
    rows: list[ChatMessage],
    wrap_user_names: bool = False,
    name_map: dict[uuid.UUID, str] | None = None,
) -> list[str]:
    """Build objective candidates from typed rows, never text sentinels.

    User content may itself contain lines that resemble our summary transcript
    headings. Reading the row role directly prevents such content from being
    reclassified as assistant/tool text by a regex parser.
    """
    prior_items = _objective_evidence_items(prior_summary or "") if prior_summary else []
    user_items: list[str] = []
    for row in rows:
        if _role := getattr(row, "role", ""):
            if str(getattr(_role, "value", _role)) != "user":
                continue
        else:
            continue
        raw_meta = getattr(row, "message_meta", None)
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        body, attachments = normalize_chat_message_attachments(
            row.content or "",
            meta,
            meta.get("source_channel"),
        )
        body = render_attachment_context(body, attachments)
        sender_user_id = getattr(row, "sender_user_id", None) or getattr(row, "user_id", None)
        if wrap_user_names and sender_user_id is not None:
            body = wrap_with_sender(
                body,
                sender_user_id,
                (name_map or {}).get(sender_user_id),
            )
        if str(body).strip():
            user_items.append(str(body).strip())
    # The first user input is the durable root objective for the initial
    # compaction epoch; the latest inputs usually refine it. Preserve both
    # ends so three trailing confirmations cannot displace the actual goal.
    current_items = (
        [user_items[0], *user_items[-OBJECTIVE_EVIDENCE_USER_INPUTS:]]
        if user_items
        else []
    )
    # Allocate the bounded slots by meaning. A generic first/last slice can
    # drop a changed objective in a later epoch when its trailing messages are
    # only confirmations.
    selected: list[str] = []

    def _add(item: str) -> None:
        if item and item not in selected and len(selected) < OBJECTIVE_EVIDENCE_MAX_ITEMS:
            selected.append(item)

    current_unique = list(dict.fromkeys(current_items))
    prior_budget = max(0, OBJECTIVE_EVIDENCE_MAX_ITEMS - len(current_unique))
    if prior_items and prior_budget:
        _add(prior_items[0])
        for item in prior_items[-max(0, prior_budget - 1):]:
            if len(selected) >= prior_budget:
                break
            _add(item)
    # Current-epoch evidence is always last and chronological, so trailing
    # confirmations cannot make an older prior detail look like the latest
    # instruction to the next provider call.
    for item in current_unique:
        _add(item)
    return selected


# ─── Validation gate ─────────────────────────────────────────────────


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
    r"(?<!\S)/(?:new|reset|help|stop|thinking|think)(?:\s+(?:on|off|status))?(?=[\s,;:!?]|$)",
    re.IGNORECASE,
)
_KNOWN_SLASH_COMMANDS = {"/new", "/reset", "/help", "/stop", "/thinking", "/think"}
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

    Length + structure are hard gates; the UUID/path recall metric is
    a quality signal.
    """
    s = (summary or "").strip()

    if len(s) < MIN_SUMMARY_CHARS:
        return False, f"too_short ({len(s)} < {MIN_SUMMARY_CHARS})", 0.0

    # Per the prompt we expect roughly < 4×max_tokens chars (gross
    # English upper bound). Anything wildly over is suspicious.
    if len(s) > MAX_SUMMARY_STORAGE_CHARS:
        return False, f"too_long ({len(s)} > {MAX_SUMMARY_STORAGE_CHARS})", 0.0

    if len(_HEADING_RE.findall(s)) < 2:
        return False, "missing_section_headings", 0.0
    if not _CURRENT_OBJECTIVE_HEADING_RE.search(s):
        return False, "missing_current_objective_section", 0.0
    if not _GOAL_LEDGER_HEADING_RE.search(s):
        return False, "missing_goal_ledger_section", 0.0
    if not _GOAL_LEDGER_ACTIVE_RE.search(s):
        return False, "missing_active_goal", 0.0
    if not _GOAL_LEDGER_ACHIEVED_RE.search(s):
        return False, "missing_achieved_goals", 0.0
    if not _GOAL_LEDGER_UNFINISHED_RE.search(s):
        return False, "missing_unfinished_goals", 0.0
    if not _RELATED_TASK_HEADING_RE.search(s):
        return False, "missing_related_task_handoff", 0.0
    related_match = _RELATED_TASK_SECTION_RE.search(s)
    related_body = related_match.group("body").strip() if related_match else ""
    if not re.search(r"^\s*-\s*None evidenced[.!。]?\s*$", related_body, re.MULTILINE | re.IGNORECASE):
        missing_fields = [
            label
            for label, pattern in _RELATED_TASK_REQUIRED_FIELDS
            if not pattern.search(related_body)
        ]
        if missing_fields:
            return False, "incomplete_related_task_handoff:" + ",".join(missing_fields), 0.0
    if not _OPEN_ITEMS_HEADING_RE.search(s):
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


async def _delete_uncommitted_archive(archive_materialized) -> None:
    """Do not delete a stable content-addressed archive after a failed commit.

    Another process may already have committed a summary referencing the same
    epoch/source object. The bounded orphan case is safe and retry-reusable;
    deleting a possibly shared object would break lossless history.
    """
    return None


# ─── Per-session lock ────────────────────────────────────────────────


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    async with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[session_id] = lock
        return lock


def _current_anchor_owns_latest_logical_tail(
    rows: list[ChatMessage],
    current_anchor_id: uuid.UUID,
) -> bool:
    """Accept an anchored turn after it has accumulated tools/injections."""
    if not rows:
        return False
    partition = partition_turns(rows, current_anchor_id=str(current_anchor_id))
    current = partition.current
    return bool(
        current is not None
        and current.rows
        and str(getattr(current.rows[-1], "id", "")) == str(rows[-1].id)
    )


def _is_dryrun() -> bool:
    return os.environ.get("CLAWITH_COMPACT_DRYRUN", "").lower() in ("1", "true", "yes")


# ─── Summary LLM call ────────────────────────────────────────────────


async def _summarize_via_llm(
    *,
    span_text: str,
    prior_summary: str | None,
    model: LLMModel,
    failed_summary: str | None = None,
    failure_reason: str | None = None,
    on_input_bounded: Callable[[bool], None] | None = None,
) -> tuple[str, dict | None]:
    """Run the summary call. Returns ``(summary_text, raw_usage_dict)``.

    Uses the same primary model. The summary is its own request — no
    tools, no streaming, no agent context — so we instantiate a bare
    LLM client directly.
    """
    from app.services.llm import LLMMessage, create_llm_client, get_model_api_key

    def _messages(candidate_span: str) -> list[LLMMessage]:
        user_payload = []
        if prior_summary:
            user_payload.append("<prior-summary epoch_n_minus_1>\n" + prior_summary + "\n</prior-summary>\n")
        if failure_reason is not None:
            user_payload.append(
                "<repair-request>\n"
                f"The previous draft failed validation: {failure_reason}. "
                "Rewrite it once so it follows every required heading and preservation rule.\n"
                f"<failed-draft>\n{failed_summary or ''}\n</failed-draft>\n"
                "</repair-request>"
            )
        user_payload.append("<chat-segment>\n" + candidate_span + "\n</chat-segment>")
        return [
            LLMMessage(role="system", content=SUMMARY_SYSTEM_PROMPT),
            LLMMessage(role="user", content="\n\n".join(user_payload)),
        ]

    messages = _messages(span_text)
    if on_input_bounded is not None:
        on_input_bounded(False)

    api_key = get_model_api_key(model)
    client = create_llm_client(
        provider=model.provider,
        base_url=model.base_url,
        api_key=api_key,
        model=model.model,
        timeout=float(getattr(model, "request_timeout", None) or 120.0),
        provider_managed_timeout=True,
    )

    try:
        response = await client.complete(
            messages,
            max_tokens=model.compact_summary_max_tokens,
            temperature=0.2,
        )
        return response.content or "", response.usage
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            await close()


# ─── Orchestrator ────────────────────────────────────────────────────


async def maybe_compact(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    model: LLMModel,
    last_prompt_tokens: int | None = None,
    pre_flight_estimate: int | None = None,
    current_anchor_id: uuid.UUID | None = None,
    force_required: bool = False,
    keep_recent_turns_override: int | None = None,
) -> CompactionResult:
    """Main entry point.

    Caller should:
    1. Pass ``last_prompt_tokens=usage.prompt_tokens`` from the round
       that just completed (or ``None`` if this is the first round of
       the session). Character/byte estimates are never capacity authority.
    2. After this returns ``triggered=True``, rebuild ``api_messages``
       by re-loading history through the compaction-aware
       ``chat_history.load_history_for_llm``.
    """
    fire, ratio, reason = should_compact(
        model=model,
        last_prompt_tokens=last_prompt_tokens,
        pre_flight_estimate=pre_flight_estimate,
    )
    if force_required and not fire:
        from app.services.llm.client import get_max_tokens
        from app.services.llm.context_budget import resolve_context_budget

        max_output_tokens = get_max_tokens(
            str(getattr(model, "provider", "") or ""),
            str(getattr(model, "model", "") or ""),
            getattr(model, "max_output_tokens", None),
        )
        input_capacity = resolve_context_budget(
            model,
            max_output_tokens=max_output_tokens,
        ).input_capacity
        ratio = (
            (last_prompt_tokens or 0) / input_capacity
            if input_capacity > 0
            else 0.0
        )
        fire = True
        reason = "provider_hard_limit"
    if not fire:
        return CompactionResult(triggered=False, skipped_reason=reason)

    session_id = conversation_id
    lock = await _get_session_lock(session_id)
    waited_for_concurrent = lock.locked()
    state_before = None
    if waited_for_concurrent:
        state_before = await _load_compaction_state(
            agent_id=agent_id,
            conversation_id=conversation_id,
            session_id=session_id,
        )
    # A concurrent compaction is work in progress, not a failure. Wait for it
    # and inspect fresh DB state before deciding whether another summary is
    # needed.  A changed state means the first request already did the work.
    async with lock:
        if waited_for_concurrent:
            state_after = await _load_compaction_state(
                agent_id=agent_id,
                conversation_id=conversation_id,
                session_id=session_id,
            )
            marker_changed = state_after[1] != state_before[1]
            active_rows_reduced = state_after[0] < state_before[0]
            if marker_changed or active_rows_reduced:
                return CompactionResult(
                    triggered=True,
                    required=True,
                    skipped_reason="completed_by_concurrent_compaction",
                )
        try:
            result = await _do_compact(
                agent_id=agent_id,
                session_id=session_id,
                conversation_id=conversation_id,
                model=model,
                trigger_prompt_tokens=last_prompt_tokens or 0,
                trigger_ratio=ratio,
                trigger_reason=reason,
                current_anchor_id=current_anchor_id,
                keep_recent_turns_override=keep_recent_turns_override,
                exact_keep_recent_turns=(
                    force_required and keep_recent_turns_override is not None
                ),
            )
            if (
                waited_for_concurrent
                and not result.triggered
                and result.skipped_reason
                in {
                    "span_too_small_to_be_worth_compacting",
                    "span_mass_too_small_to_matter",
                }
            ):
                # The waiter started from an old oversized prompt.  If the
                # first request compacted just before our initial state read,
                # the fresh history can legitimately have no useful span.
                # Tell the caller to reload and perform its normal size recheck.
                return CompactionResult(
                    triggered=True,
                    required=True,
                    skipped_reason="concurrent_compaction_requires_recheck",
                )
            result.required = True
            return result
        except Exception as exc:
            logger.error(f"[compactor] unexpected failure for session={session_id}: {type(exc).__name__}: {exc}")
            return CompactionResult(
                triggered=False,
                required=True,
                skipped_reason=f"exception:{type(exc).__name__}",
            )


async def maybe_precompact_prompt(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    model: LLMModel,
    prompt_messages: list[dict],
    current_anchor_id: uuid.UUID | None = None,
) -> CompactionResult:
    """Pre-flight compaction guard for the channel / web entry points.

    Estimates the about-to-be-sent prompt's token count; if it crosses the
    configured compaction threshold, compacts NOW so a
    subsequent history reload returns a slimmer prompt — preventing the current
    request from overflowing the model window. (The post-round hook only helps
    the NEXT turn, so a single oversized prompt — big paste, huge first
    message, accumulated tool output — would otherwise be rejected by the
    provider before compaction ever ran.)

    Returns an explicit result.  ``triggered`` means compaction was applied and
    the caller MUST reload history before sending.  ``required`` distinguishes
    a threshold-crossing failure from the ordinary cheap below-threshold no-op.
    """
    if not (agent_id and conversation_id and model):
        return CompactionResult(triggered=False, skipped_reason="missing_preflight_context")
    return CompactionResult(
        triggered=False,
        skipped_reason="official_preflight_counter_unavailable",
    )


async def _do_compact(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    conversation_id: str,
    model: LLMModel,
    trigger_prompt_tokens: int,
    trigger_ratio: float,
    trigger_reason: str,
    current_anchor_id: uuid.UUID | None = None,
    keep_recent_turns_override: int | None = None,
    exact_keep_recent_turns: bool = False,
) -> CompactionResult:
    async with async_session() as db:
        # 1. Load all currently-active messages (compacted_into IS NULL)
        rows = await _load_active_rows(db, agent_id=agent_id, conversation_id=conversation_id)

        if current_anchor_id is not None and not _current_anchor_owns_latest_logical_tail(
            rows,
            current_anchor_id,
        ):
            return CompactionResult(
                triggered=False,
                skipped_reason="current_anchor_is_not_latest_user",
            )

        # 2. Pull prior epoch's summary (if any) — chained accumulation
        prior_summary, prior_epoch, prior_marker_id = await _load_active_marker(db, session_id=session_id)

        # Group-chat identity is message-scoped. Render the compaction input
        # with the same canonical sender envelope as ordinary history, while
        # keeping P2P input unchanged because its identity is session-scoped.
        wrap_user_names, sender_name_map = await _load_summary_sender_attribution(
            db,
            agent_id=agent_id,
            conversation_id=conversation_id,
            rows=rows,
        )

        # 3. Normal threshold compaction protects the configured suffix. After
        # an explicit provider rejection the caller supplies exactly one lower
        # protection level per retry, eventually reaching zero historical
        # turns. The current anchored turn remains a separate immutable
        # partition and is never summarized here.
        if exact_keep_recent_turns:
            selected_keep = max(0, int(keep_recent_turns_override or 0))
        else:
            selected_keep = max(
                MIN_PROTECTED_RECENT_TURNS,
                int(getattr(model, "keep_recent_turns", 0) or 0),
                int(keep_recent_turns_override or 0),
            )
        turn_partition = partition_turns(
            rows,
            current_anchor_id=str(current_anchor_id) if current_anchor_id else None,
            keep_recent_turns=selected_keep,
            minimum_protected_turns=0,
        )
        span_rows = turn_partition.compactable_rows
        if not span_rows:
            return CompactionResult(
                triggered=False,
                skipped_reason="span_too_small_to_be_worth_compacting",
            )
        if exact_keep_recent_turns:
            logger.warning(
                "[compactor] provider-rejection protection level "
                f"session={session_id} keep_recent_turns={selected_keep}"
            )
        expected_span_ids = tuple(str(row.id) for row in span_rows)

        # 3.5 Internal span sizing is observability only. It must not veto a
        # required compaction because it is not a provider token count.
        span_text = serialize_span_for_summary(
            span_rows,
            prefilter=False,
            wrap_user_names=wrap_user_names,
            name_map=sender_name_map,
        )
        # 4. Summarize the exact serialized view used for the futility estimate.
        # Keep an unfiltered source for the lossless continuity decision.  The
        # summary provider sees a bounded head/tail view, but that optimization
        # must never make a large requirement in the middle unverifiable.
        unfiltered_span_text = serialize_span_for_summary(
            span_rows,
            prefilter=False,
            wrap_user_names=wrap_user_names,
            name_map=sender_name_map,
        )
        unfiltered_validation_source = "\n\n".join(
            part
            for part in (
                prior_summary,
                unfiltered_span_text,
            )
            if part
        )
        # The provider receives a bounded head/tail view. Any bytes omitted
        # from ordinary inline content must remain available through a
        # lossless archive. Persisted-output envelope bodies are the sole
        # exception because their complete source already has a durable path.
        durable_elided_span_text = serialize_span_for_summary(
            span_rows,
            prefilter=False,
            wrap_user_names=wrap_user_names,
            name_map=sender_name_map,
            elide_durable_only=True,
        )
        prefilter_omitted_inline_content = span_text != durable_elided_span_text
        objective_evidence_items = objective_evidence_items_from_rows(
            prior_summary=prior_summary,
            rows=span_rows,
            wrap_user_names=wrap_user_names,
            name_map=sender_name_map,
        )
        evidence_max_chars = objective_evidence_limit(model.compact_summary_max_tokens)
        objective_evidence_truncated = objective_evidence_is_truncated(
            unfiltered_validation_source,
            max_chars=evidence_max_chars,
            evidence_items=objective_evidence_items,
        )
        # Do not occupy a PostgreSQL connection/transaction during one or two
        # potentially slow summary requests. Cross-process serialization is
        # reacquired immediately before the stale-boundary check and writes.
        await db.commit()
        recovery_reasons: list[str] = []
        initial_llm_failed = False
        summary_usage: dict | None = None
        try:
            summary, summary_usage = await _summarize_via_llm(
                span_text=span_text,
                prior_summary=prior_summary,
                model=model,
            )
            from app.services.token_tracker import extract_token_usage, record_token_usage

            normalized = extract_token_usage(summary_usage)
            if normalized is not None and normalized.total_tokens > 0:
                await record_token_usage(agent_id, normalized)
        except Exception as exc:
            initial_llm_failed = True
            first_reason = f"summary_llm_error:{type(exc).__name__}:{str(exc)[:300]}"
            recovery_reasons.append(first_reason)
            logger.error(f"[compactor] summary LLM call failed for session={session_id}: {first_reason}")
            summary = ""

        # 5. Deterministically preserve exact identifiers before validation.
        # A structurally sound summary must not brick a session because the
        # model omitted one opaque UUID, URL, path, or slash command.
        summary, appended_identifiers = append_missing_identifiers(
            summary=summary,
            original_text=unfiltered_validation_source,
        )
        summary = pin_summary_objective(
            summary=summary,
            original_text=unfiltered_validation_source,
            max_tokens=model.compact_summary_max_tokens,
            objective_evidence_items=objective_evidence_items,
        )
        if appended_identifiers:
            logger.info(
                f"[compactor] appended {len(appended_identifiers)} missing identifiers for session={session_id}"
            )
        identifier_appendix_exhausted_budget = bool(
            appended_identifiers
            and len(summary) > MAX_SUMMARY_STORAGE_CHARS
        )

        # 6. Validate
        passed, fail_reason, recall = validate_summary(
            summary=summary,
            original_text=unfiltered_validation_source,
            max_tokens=model.compact_summary_max_tokens,
            objective_text=unfiltered_validation_source,
            objective_evidence_max_chars=evidence_max_chars,
            objective_evidence_items=objective_evidence_items,
        )

        # A structurally invalid model draft receives exactly one repair
        # attempt. Transport failures go directly to the deterministic
        # lossless fallback; repeating the same timed-out request only doubles
        # turn latency and produces no draft that can be repaired.
        if (
            not passed
            and not initial_llm_failed
            and not identifier_appendix_exhausted_budget
        ):
            repair_reason = fail_reason or recovery_reasons[-1]
            if not recovery_reasons or recovery_reasons[-1] != repair_reason:
                recovery_reasons.append(f"validation_failed:{repair_reason}")
            try:
                repaired, repaired_usage = await _summarize_via_llm(
                    span_text=span_text,
                    prior_summary=prior_summary,
                    model=model,
                    failed_summary=summary,
                    failure_reason=repair_reason,
                )
                repaired_normalized = extract_token_usage(repaired_usage)
                if repaired_normalized is not None and repaired_normalized.total_tokens > 0:
                    await record_token_usage(agent_id, repaired_normalized)
                repaired, _ = append_missing_identifiers(
                    summary=repaired,
                    original_text=unfiltered_validation_source,
                )
                repaired = pin_summary_objective(
                    summary=repaired,
                    original_text=unfiltered_validation_source,
                    max_tokens=model.compact_summary_max_tokens,
                    objective_evidence_items=objective_evidence_items,
                )
                repaired_passed, repaired_reason, repaired_recall = validate_summary(
                    summary=repaired,
                    original_text=unfiltered_validation_source,
                    max_tokens=model.compact_summary_max_tokens,
                    objective_text=unfiltered_validation_source,
                    objective_evidence_max_chars=evidence_max_chars,
                    objective_evidence_items=objective_evidence_items,
                )
                if repaired_passed:
                    summary = repaired
                    summary_usage = repaired_usage
                    passed, fail_reason, recall = True, None, repaired_recall
                else:
                    recovery_reasons.append(f"repair_validation_failed:{repaired_reason}")
            except Exception as exc:
                recovery_reasons.append(
                    f"repair_llm_error:{type(exc).__name__}:{str(exc)[:300]}"
                )

        # The deterministic path has no provider dependency and therefore
        # guarantees that summary formatting/model failures do not block a
        # required compaction.
        archives_abandoned_turn = any(
            not turn.closed or turn.unknown for turn in turn_partition.compactable
        )
        if archives_abandoned_turn:
            recovery_reasons.append("lossless_archive:abandoned_incomplete_turn")
        lossless_archive_source: str | None = None
        if (
            not passed
            or objective_evidence_truncated
            or prefilter_omitted_inline_content
            or archives_abandoned_turn
        ):
            lossless_archive_source = unfiltered_validation_source

        # Archive I/O can target S3. Perform it after releasing the read
        # transaction and before acquiring the short PostgreSQL advisory lock,
        # so a slow object store cannot pin one DB pool connection per turn.
        archive_materialized = None
        if lossless_archive_source is not None:
            from app.services.llm.tool_output_store import materialize_tool_output_strict

            archive_materialized = await asyncio.wait_for(
                materialize_tool_output_strict(
                    lossless_archive_source,
                    tool_name="conversation_compaction_archive",
                    agent_id=agent_id,
                    session_id=session_id,
                    tool_call_id=f"epoch-{(prior_epoch or 0) + 1}",
                    max_view_chars=4_000,
                ),
                timeout=ARCHIVE_WRITE_TIMEOUT_SECONDS,
            )
            if passed:
                summary = append_lossless_archive_reference(
                    summary=summary,
                    relative_path=archive_materialized.relative_path,
                )
            else:
                summary = build_deterministic_summary(
                    source_text=unfiltered_validation_source,
                    max_tokens=model.compact_summary_max_tokens,
                    archive_envelope=archive_materialized.llm_view,
                    objective_evidence_items=objective_evidence_items,
                )
            archive_validation_source = (
                "<persisted-output>\n"
                f"Full output saved to: {archive_materialized.relative_path}\n"
                "</persisted-output>"
            )
            passed, fail_reason, recall = validate_summary(
                summary=summary,
                # The bounded result need only preserve the exact newly-created
                # archive path; the referenced object is the lossless source.
                original_text=archive_validation_source,
                max_tokens=model.compact_summary_max_tokens,
                objective_text=unfiltered_validation_source,
                objective_evidence_max_chars=evidence_max_chars,
                objective_evidence_items=objective_evidence_items,
                objective_archived=True,
            )
            if not passed and (
                objective_evidence_truncated
                or prefilter_omitted_inline_content
            ):
                # A near-limit model draft can become too long after the
                # mandatory archive reference. Fall back to the bounded local
                # shell while retaining the same newly-created lossless object.
                summary = build_deterministic_summary(
                    source_text=unfiltered_validation_source,
                    max_tokens=model.compact_summary_max_tokens,
                    archive_envelope=archive_materialized.llm_view,
                    objective_evidence_items=objective_evidence_items,
                )
                passed, fail_reason, recall = validate_summary(
                    summary=summary,
                    original_text=archive_validation_source,
                    max_tokens=model.compact_summary_max_tokens,
                    objective_text=unfiltered_validation_source,
                    objective_evidence_max_chars=evidence_max_chars,
                    objective_evidence_items=objective_evidence_items,
                    objective_archived=True,
                )
            if not passed:
                raise RuntimeError(f"deterministic_summary_invalid:{fail_reason}")

        # Serialize only the short stale-check/write transaction across backend
        # processes. Provider I/O above holds neither this lock nor a DB pool
        # connection.
        try:
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:session_id, 0))"),
                {"session_id": session_id},
            )
            fresh_rows = await _load_active_rows(
                db,
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
        except BaseException:
            await _delete_uncommitted_archive(archive_materialized)
            raise
        if current_anchor_id is not None and not _current_anchor_owns_latest_logical_tail(
            fresh_rows,
            current_anchor_id,
        ):
            await _delete_uncommitted_archive(archive_materialized)
            await db.rollback()
            return CompactionResult(
                triggered=False,
                skipped_reason="conversation_changed_during_compaction",
            )
        fresh_span_ids = tuple(
            str(row.id)
            for row in partition_turns(
                fresh_rows,
                current_anchor_id=str(current_anchor_id) if current_anchor_id else None,
                keep_recent_turns=selected_keep,
                minimum_protected_turns=0,
            ).compactable_rows
        )
        if fresh_span_ids != expected_span_ids:
            await _delete_uncommitted_archive(archive_materialized)
            await db.rollback()
            return CompactionResult(
                triggered=False,
                skipped_reason="compactable_span_changed_during_compaction",
            )

        # 7. Persist the validated model/repair/fallback result atomically.
        new_epoch = (prior_epoch or 0) + 1
        # The persisted text may include local identifier/objective/archive
        # additions or be a deterministic replacement. Without an official
        # counter for that exact final text its token count is unknown.
        summary_tokens = None

        compaction = ChatCompaction(
            id=uuid.uuid4(),
            session_id=session_id,
            agent_id=agent_id,
            epoch=new_epoch,
            compacted_from_message_id=span_rows[0].id,
            compacted_to_message_id=span_rows[-1].id,
            summary_text=summary,
            summary_tokens=summary_tokens,
            trigger_prompt_tokens=trigger_prompt_tokens,
            trigger_ratio=trigger_ratio,
            superseded_by=None,
            summary_validation_passed=True,
            validation_failure_reason="; ".join(recovery_reasons) or None,
            created_at=datetime.now(timezone.utc),
        )

        # SQLAlchemy AsyncSession autobegins a transaction on first
        # query, so an explicit `async with db.begin()` would raise
        # "A transaction is already begun". We rely on the autobegun
        # transaction and commit/rollback explicitly. The whole block
        # of writes lands atomically — any exception propagates up
        # to the caller's try/except in maybe_compact, which doesn't
        # commit, so the session closes with the transaction rolled
        # back.
        try:
            db.add(compaction)
            await db.flush()

            if not _is_dryrun():
                update_result = await db.execute(
                    update(ChatMessage)
                    .where(
                        ChatMessage.id.in_([r.id for r in span_rows]),
                        ChatMessage.compacted_into.is_(None),
                    )
                    .values(compacted_into=compaction.id)
                )
                if update_result.rowcount != len(span_rows):
                    raise RuntimeError(
                        "compaction row count changed concurrently: "
                        f"expected={len(span_rows)} updated={update_result.rowcount}"
                    )
                if prior_marker_id is not None:
                    await db.execute(
                        update(ChatCompaction)
                        .where(ChatCompaction.id == prior_marker_id)
                        .values(superseded_by=compaction.id)
                    )
                # Every provider observation in this session describes the
                # pre-compaction prompt shape. Consume all of them atomically;
                # clearing only the current anchor leaves an older triggering
                # anchor live and can repeatedly compact/cache-bust after a
                # later network/authentication failure.
                from app.services.session_token_usage import SESSION_CONTEXT_META_KEY

                for observed_anchor in fresh_rows:
                    anchor_meta = dict(getattr(observed_anchor, "message_meta", None) or {})
                    context_meta = anchor_meta.get(SESSION_CONTEXT_META_KEY)
                    if not isinstance(context_meta, dict):
                        continue
                    context_meta = dict(context_meta)
                    context_meta["last_input_tokens"] = 0
                    context_meta["consumed_by_compaction_epoch"] = new_epoch
                    context_meta["consumed_at"] = datetime.now(timezone.utc).isoformat()
                    anchor_meta[SESSION_CONTEXT_META_KEY] = context_meta
                    observed_anchor.message_meta = anchor_meta

            await db.commit()
        except BaseException:
            await _delete_uncommitted_archive(archive_materialized)
            await db.rollback()
            raise

        if _is_dryrun():
            logger.info(
                f"[compactor] DRYRUN session={session_id} epoch={new_epoch} "
                f"reason={trigger_reason} ratio={trigger_ratio:.2f} "
                f"summary_tokens={summary_tokens} recall={recall:.2f} — "
                f"chat_compactions row written, ChatMessage flags NOT updated"
            )
            return CompactionResult(
                triggered=False,
                summary_id=compaction.id,
                epoch=new_epoch,
                summary_tokens=summary_tokens,
                trigger_prompt_tokens=trigger_prompt_tokens,
                skipped_reason="dryrun",
                progress_notice=f"🗜 (dryrun) compaction simulated, epoch={new_epoch}",
            )

        logger.info(
            f"[compactor] applied session={session_id} epoch={new_epoch} "
            f"reason={trigger_reason} ratio={trigger_ratio:.2f} "
            f"compacted_rows={len(span_rows)} summary_tokens={summary_tokens} "
            f"recall={recall:.2f}"
        )
        # Savings are known only after the next provider call returns exact
        # usage. Never publish a character-derived token saving estimate.
        notice = f"🗜 已整理 {len(span_rows)} 条历史消息（epoch={new_epoch}）"
        return CompactionResult(
            triggered=True,
            summary_id=compaction.id,
            epoch=new_epoch,
            summary_tokens=summary_tokens,
            trigger_prompt_tokens=trigger_prompt_tokens,
            progress_notice=notice,
        )


# ─── DB helpers ──────────────────────────────────────────────────────


async def _load_active_rows(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> list[ChatMessage]:
    """All non-compacted messages for this session, oldest first."""
    result = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.compacted_into.is_(None),
        )
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
    )
    return [
        row
        for row in result.scalars().all()
        if not (isinstance(getattr(row, "message_meta", None), dict) and row.message_meta.get("consumed_by_onmessage"))
    ]


async def _load_compaction_state(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    session_id: str,
) -> tuple[int, uuid.UUID | None]:
    """Return the minimal persisted state needed to detect concurrent work."""
    async with async_session() as db:
        active_count = len(
            await _load_active_rows(
                db,
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
        )
        _, _, marker_id = await _load_active_marker(db, session_id=session_id)
    return active_count, marker_id


async def _load_active_marker(
    db: AsyncSession,
    *,
    session_id: str,
) -> tuple[str | None, int | None, uuid.UUID | None]:
    """Latest non-superseded ChatCompaction summary for this session
    (or ``(None, None, None)`` if no prior compaction).
    """
    result = await db.execute(
        select(ChatCompaction)
        .where(
            ChatCompaction.session_id == session_id,
            ChatCompaction.superseded_by.is_(None),
            ChatCompaction.summary_validation_passed.is_(True),
        )
        .order_by(ChatCompaction.epoch.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None, None, None
    return row.summary_text, row.epoch, row.id
