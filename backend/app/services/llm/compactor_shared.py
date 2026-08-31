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

__all__ = [name for name in globals() if not name.startswith("__")]
