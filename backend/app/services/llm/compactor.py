"""Conversation auto-compaction.

When a session's running prompt-token count approaches the model's
``context_window``, this module:

1. Decides whether to fire (post-round actual ratio vs. pre-flight
   estimate ratio).
2. Acquires a per-session async lock so concurrent channels don't
   compact the same session twice.
3. Selects a span of messages to compact, aligned to round / tool-pair
   boundaries so we never split an assistant→tool→tool_result group.
4. Pre-filters the span to keep the summary-LLM input under that
   model's own context limit (``<persisted-output>`` envelopes get
   truncated to their headers; oversized tool bodies get head/tail
   excerpts; image blocks get placeholders).
5. Calls the same primary LLM with a structured-summary system
   prompt; caps output via ``LLMModel.compact_summary_max_tokens``.
6. Validates the summary (length / structure / UUID-and-path recall
   ≥ 0.7) before persisting; failed validation is recorded but the
   session's history is *not* mutated, so the caller falls back to
   ``ctx_size`` truncation that round.
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
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction
from app.models.llm import LLMModel


# ─── Configuration constants ─────────────────────────────────────────

# Pre-flight safety net: when we don't yet have a usage observation
# (e.g. first request of a freshly loaded session), we fall back to
# a char-based estimate. Higher threshold here than the post-round
# trigger because the estimate has more variance — we only want this
# to fire to AVOID an OOM, not to fine-tune.
PRE_FLIGHT_TRIGGER_RATIO = 0.95

# Rough chars-per-token ratio for prompt-size estimation. 2.5 chosen
# to be conservative on the high side for Chinese-heavy text where
# a single CJK character is often a single token. Tuning this is fine.
ESTIMATE_CHARS_PER_TOKEN = 2.5

# Per-session compaction lock TTL. Generous enough to cover even a
# slow summary LLM call; the watchdog renews mid-flight if needed.
COMPACT_LOCK_TTL_SECONDS = 120

# Validation gate — reject summaries that don't recall enough of the
# IDs and file paths from the original span. 0.7 is a starting point;
# we'll tune after observing the dryrun distribution for a week.
UUID_RECALL_THRESHOLD = 0.7
MIN_SUMMARY_CHARS = 200

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
    summary_id: uuid.UUID | None = None
    epoch: int | None = None
    summary_tokens: int | None = None
    trigger_prompt_tokens: int | None = None
    skipped_reason: str | None = None
    progress_notice: str | None = None


# ─── Trigger decision ────────────────────────────────────────────────


def estimate_prompt_tokens(api_messages: list[dict]) -> int:
    """Char-count-based prompt-token estimate for the pre-flight path.

    Used only when no real usage observation is available yet. The
    real post-round path uses ``response.usage.prompt_tokens`` directly
    (via ``last_prompt_tokens`` argument).
    """
    total_chars = 0
    for msg in api_messages:
        c = msg.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict):
                    t = block.get("text")
                    if isinstance(t, str):
                        total_chars += len(t)
                    elif "image_url" in block or block.get("type") == "image":
                        # Count placeholder cost; vision tokens are
                        # provider-specific and hard to estimate.
                        total_chars += 1024
        for tc in msg.get("tool_calls", []) or []:
            args = tc.get("function", {}).get("arguments", "")
            if isinstance(args, str):
                total_chars += len(args)
            elif isinstance(args, dict):
                import json as _j
                total_chars += len(_j.dumps(args, ensure_ascii=False))
    return int(total_chars / ESTIMATE_CHARS_PER_TOKEN)


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
    cw = model.context_window
    if cw is None or cw <= 0:
        return False, 0.0, "no_context_window_configured"

    if last_prompt_tokens is not None:
        ratio = last_prompt_tokens / cw
        if ratio >= model.compact_trigger_ratio:
            return True, ratio, "post_round"

    if pre_flight_estimate is not None:
        ratio = pre_flight_estimate / cw
        if ratio >= PRE_FLIGHT_TRIGGER_RATIO:
            return True, ratio, "pre_flight"

    # Take the larger of the two for visibility even when not firing.
    visible_ratio = 0.0
    if last_prompt_tokens is not None:
        visible_ratio = max(visible_ratio, last_prompt_tokens / cw)
    if pre_flight_estimate is not None:
        visible_ratio = max(visible_ratio, pre_flight_estimate / cw)
    return False, visible_ratio, "below_threshold"


# ─── Span selection ──────────────────────────────────────────────────


def _is_round_boundary_after(
    rows: list[ChatMessage], idx: int
) -> bool:
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
    """Pick the ``[from_idx, to_idx]`` slice of ``rows`` to compact.

    ``rows`` is the active (``compacted_into IS NULL``) message stream
    in chronological order, excluding any prior compaction's summary.
    Returns ``None`` when there isn't enough history to be worth
    compacting (fewer than ``keep_recent_turns`` complete rounds in
    the trailing window plus at least one round of older content to
    fold into the summary).

    Algorithm:
    1. Walk backward through ``rows``, counting user-message
       boundaries until we've seen ``keep_recent_turns`` of them. The
       message *before* that boundary is the natural cut point.
    2. The compacted span is ``rows[0:cut]``; the active trailing
       window is ``rows[cut:]``.
    3. Refuse to compact a span shorter than 4 rows — too little gain.
    """
    if not rows:
        return None

    user_boundaries_seen = 0
    cut_idx: int | None = None
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].role == "user":
            user_boundaries_seen += 1
            if user_boundaries_seen >= keep_recent_turns:
                cut_idx = i
                break

    if cut_idx is None or cut_idx < 4:
        return None

    # The compacted span ends at the message immediately *before* the
    # trailing keep window's first user message — that's cut_idx - 1.
    # We further enforce that the span itself ends on a round boundary
    # (no orphan tool_call dangling at compacted_to).
    span_to = cut_idx - 1
    while span_to > 0 and rows[span_to].role != "assistant":
        span_to -= 1

    if span_to < 1:
        return None

    return (0, span_to)


# ─── Pre-filtering ───────────────────────────────────────────────────


_PERSISTED_ENVELOPE_RE = re.compile(
    r"<persisted-output[^>]*>.*?</persisted-output>",
    re.DOTALL,
)
_PERSISTED_HEADER_RE = re.compile(r"<persisted-output[^>]*>")
_LARGE_BODY_HEAD_TAIL_THRESHOLD = 4000  # chars
_LARGE_BODY_KEEP_HEAD = 800
_LARGE_BODY_KEEP_TAIL = 800


def prefilter_message_content(content: str) -> str:
    """Shrink a single message body for inclusion in the summary
    LLM input.

    Keeps everything semantically important; drops bulk we know is
    redundant (envelope bodies for materialized outputs, oversized
    raw tool bodies).
    """
    if not isinstance(content, str):
        return content

    def _replace_envelope(m: re.Match) -> str:
        # Keep only the opening tag (which has path + size attrs) plus
        # a brief notice — the body is on disk, the agent can re-read.
        head = _PERSISTED_HEADER_RE.search(m.group(0))
        if head:
            return f"{head.group(0)}…[body materialized to disk]…</persisted-output>"
        return "<persisted-output>…[materialized]…</persisted-output>"

    out = _PERSISTED_ENVELOPE_RE.sub(_replace_envelope, content)

    if len(out) > _LARGE_BODY_HEAD_TAIL_THRESHOLD:
        head = out[:_LARGE_BODY_KEEP_HEAD]
        tail = out[-_LARGE_BODY_KEEP_TAIL:]
        out = (
            f"{head}\n\n…[truncated {len(out) - _LARGE_BODY_KEEP_HEAD - _LARGE_BODY_KEEP_TAIL} chars]…\n\n{tail}"
        )

    return out


def serialize_span_for_summary(rows: list[ChatMessage]) -> str:
    """Render the compaction span as a single text blob for the
    summary LLM. Each row gets a role-tagged block; content is
    pre-filtered to drop bulk that won't help the summary anyway.
    """
    chunks: list[str] = []
    for r in rows:
        body = prefilter_message_content(r.content or "")
        chunks.append(f"### [{r.role}] @ {r.created_at.isoformat()}\n{body}")
    return "\n\n".join(chunks)


# ─── Validation gate ─────────────────────────────────────────────────


_UUID_LIKE_RE = re.compile(r"\b[a-f0-9]{32}\b|\b[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}\b")
# Path regex: stops on whitespace, quotes, brackets, **and** common
# trailing sentence punctuation (.,;:!?) when followed by whitespace or
# end-of-string — `workspace/draft.md,` should match `workspace/draft.md`,
# not include the comma. Otherwise recall comparison breaks because
# the summary writes the path without the trailing punctuation.
_PATH_RE = re.compile(
    r"(?:^|\s)((?:/|\./|\.\./|[A-Za-z]:\\|workspace/|memory/|skills/)[^\s'\"<>]*?)(?=[\s,;:!?]|$)"
)
_HEADING_RE = re.compile(r"^#{2,3}\s", re.MULTILINE)


def validate_summary(
    *,
    summary: str,
    original_text: str,
    max_tokens: int,
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
    if len(s) > max_tokens * 6:
        return False, f"too_long ({len(s)} > {max_tokens * 6})", 0.0

    if len(_HEADING_RE.findall(s)) < 2:
        return False, "missing_section_headings", 0.0

    # UUID + path recall: how much of what was in the original made it
    # into the summary, as a proxy for "didn't lose critical IDs".
    original_ids = set(_UUID_LIKE_RE.findall(original_text or "")) | {
        m.group(1) for m in _PATH_RE.finditer(original_text or "")
    }
    if not original_ids:
        # Span had no IDs to recall — this gate is vacuously satisfied.
        return True, None, 1.0

    recalled = sum(1 for ident in original_ids if ident in s)
    ratio = recalled / len(original_ids)
    if ratio < UUID_RECALL_THRESHOLD:
        return (
            False,
            f"low_id_recall ({recalled}/{len(original_ids)} = {ratio:.2f} < {UUID_RECALL_THRESHOLD})",
            ratio,
        )

    return True, None, ratio


# ─── Per-session lock ────────────────────────────────────────────────


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    async with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[session_id] = lock
        return lock


def _is_dryrun() -> bool:
    return os.environ.get("CLAWITH_COMPACT_DRYRUN", "").lower() in ("1", "true", "yes")


# ─── Summary LLM call ────────────────────────────────────────────────


async def _summarize_via_llm(
    *,
    span_text: str,
    prior_summary: str | None,
    model: LLMModel,
) -> tuple[str, dict | None]:
    """Run the summary call. Returns ``(summary_text, raw_usage_dict)``.

    Uses the same primary model. The summary is its own request — no
    tools, no streaming, no agent context — so we instantiate a bare
    LLM client directly.
    """
    from app.services.llm import create_llm_client, get_model_api_key, LLMMessage

    api_key = get_model_api_key(model)
    client = create_llm_client(
        provider=model.provider,
        base_url=model.base_url,
        api_key=api_key,
        model=model.model,
    )

    user_payload = []
    if prior_summary:
        user_payload.append(
            "<prior-summary epoch_n_minus_1>\n"
            + prior_summary
            + "\n</prior-summary>\n"
        )
    user_payload.append("<chat-segment>\n" + span_text + "\n</chat-segment>")

    messages = [
        LLMMessage(role="system", content=SUMMARY_SYSTEM_PROMPT),
        LLMMessage(role="user", content="\n\n".join(user_payload)),
    ]

    response = await client.complete(
        messages,
        max_tokens=model.compact_summary_max_tokens,
        temperature=0.2,
    )
    return response.content or "", response.usage


# ─── Orchestrator ────────────────────────────────────────────────────


async def maybe_compact(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    model: LLMModel,
    last_prompt_tokens: int | None = None,
    pre_flight_estimate: int | None = None,
) -> CompactionResult:
    """Main entry point.

    Caller should:
    1. Pass ``last_prompt_tokens=usage.prompt_tokens`` from the round
       that just completed (or ``None`` if this is the first round of
       the session).
    2. Optionally pass ``pre_flight_estimate=estimate_prompt_tokens(api_messages)``
       so the safety net can fire even before any real usage is known
       (e.g., user pasted a 100K token document into a fresh session).
    3. After this returns ``triggered=True``, rebuild ``api_messages``
       by re-loading history through the compaction-aware
       ``chat_history.load_history_for_llm``.
    """
    fire, ratio, reason = should_compact(
        model=model,
        last_prompt_tokens=last_prompt_tokens,
        pre_flight_estimate=pre_flight_estimate,
    )
    if not fire:
        return CompactionResult(triggered=False, skipped_reason=reason)

    session_id = conversation_id
    lock = await _get_session_lock(session_id)
    if lock.locked():
        return CompactionResult(triggered=False, skipped_reason="lock_held_by_concurrent_compaction")

    async with lock:
        try:
            return await _do_compact(
                agent_id=agent_id,
                session_id=session_id,
                conversation_id=conversation_id,
                model=model,
                trigger_prompt_tokens=last_prompt_tokens or pre_flight_estimate or 0,
                trigger_ratio=ratio,
                trigger_reason=reason,
            )
        except Exception as exc:
            logger.error(
                f"[compactor] unexpected failure for session={session_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            return CompactionResult(
                triggered=False,
                skipped_reason=f"exception:{type(exc).__name__}",
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
) -> CompactionResult:
    async with async_session() as db:
        # 1. Load all currently-active messages (compacted_into IS NULL)
        rows = await _load_active_rows(db, agent_id=agent_id, conversation_id=conversation_id)

        # 2. Pull prior epoch's summary (if any) — chained accumulation
        prior_summary, prior_epoch, prior_marker_id = await _load_active_marker(
            db, session_id=session_id
        )

        # 3. Pick the span
        span = select_compaction_span(rows, keep_recent_turns=model.keep_recent_turns)
        if span is None:
            return CompactionResult(
                triggered=False,
                skipped_reason="span_too_small_to_be_worth_compacting",
            )
        from_idx, to_idx = span
        span_rows = rows[from_idx:to_idx + 1]

        # 4. Pre-filter and serialize for the summary LLM
        span_text = serialize_span_for_summary(span_rows)

        # 5. Summarize
        try:
            summary, summary_usage = await _summarize_via_llm(
                span_text=span_text,
                prior_summary=prior_summary,
                model=model,
            )
        except Exception as exc:
            logger.error(
                f"[compactor] summary LLM call failed for session={session_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            return CompactionResult(
                triggered=False,
                skipped_reason=f"summary_llm_error:{type(exc).__name__}",
            )

        # 6. Validate
        passed, fail_reason, recall = validate_summary(
            summary=summary,
            original_text=span_text,
            max_tokens=model.compact_summary_max_tokens,
        )

        # 7. Persist (always — failed validations get rows too, for audit)
        new_epoch = (prior_epoch or 0) + 1
        summary_tokens = (summary_usage or {}).get("completion_tokens") or len(summary) // 3

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
            summary_validation_passed=passed,
            created_at=datetime.now(timezone.utc),
        )

        async with db.begin():
            db.add(compaction)
            await db.flush()

            # Only mutate ChatMessage.compacted_into if the summary
            # passed validation AND we are NOT in dryrun mode. Failed-
            # validation rows live in chat_compactions for audit but
            # don't take effect on live conversations.
            if passed and not _is_dryrun():
                await db.execute(
                    update(ChatMessage)
                    .where(ChatMessage.id.in_([r.id for r in span_rows]))
                    .values(compacted_into=compaction.id)
                )
                if prior_marker_id is not None:
                    await db.execute(
                        update(ChatCompaction)
                        .where(ChatCompaction.id == prior_marker_id)
                        .values(superseded_by=compaction.id)
                    )

        if not passed:
            logger.warning(
                f"[compactor] summary FAILED validation for session={session_id} "
                f"epoch={new_epoch}: {fail_reason}; recall={recall:.2f}; "
                f"persisted to chat_compactions for audit but NOT applied"
            )
            return CompactionResult(
                triggered=False,
                summary_id=compaction.id,
                epoch=new_epoch,
                trigger_prompt_tokens=trigger_prompt_tokens,
                skipped_reason=f"validation_failed:{fail_reason}",
            )

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
        return CompactionResult(
            triggered=True,
            summary_id=compaction.id,
            epoch=new_epoch,
            summary_tokens=summary_tokens,
            trigger_prompt_tokens=trigger_prompt_tokens,
            progress_notice=f"🗜 已整理 {len(span_rows)} 条历史消息（epoch={new_epoch}），节省约 {trigger_prompt_tokens - summary_tokens} tokens",
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
        .order_by(ChatMessage.created_at.asc())
    )
    return list(result.scalars().all())


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
