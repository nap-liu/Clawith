from app.services.llm.compactor_shared import *  # noqa: F401,F403
from app.services.llm.compactor_summary import _objective_evidence_items

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

__all__ = (
    '_elide_materialized_output_bodies',
    'prefilter_message_content',
    'serialize_span_for_summary',
    '_load_summary_sender_attribution',
    'objective_evidence_items_from_rows',
)
