from app.services.llm.compactor_shared import *  # noqa: F401,F403
from app.services.llm.compactor_runtime_support import (
    _current_anchor_owns_latest_logical_tail,
    _delete_uncommitted_archive,
    _get_session_lock,
    _is_dryrun,
    _summarize_via_llm,
)
from app.services.llm.compactor_serialization import (
    _load_summary_sender_attribution,
    objective_evidence_items_from_rows,
    serialize_span_for_summary,
)
from app.services.llm.compactor_summary import (
    append_lossless_archive_reference,
    append_missing_identifiers,
    build_deterministic_summary,
    objective_evidence_is_truncated,
    pin_summary_objective,
    validate_summary,
)
from app.services.llm.turn_partition import current_turn_compactable_rows

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
        # turns. If there is no historical prefix, a long-running trigger/A2A
        # turn may compact completed old tool rounds while retaining its user
        # anchor and recent raw tail.
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
        current_turn_span = False
        if not span_rows:
            # The provider count says the protected prompt is already unsafe.
            # At that point protection cannot veto recovery: compact every
            # closed current-turn round, retaining only the durable user anchor
            # and any open/malformed tail.
            span_rows = current_turn_compactable_rows(
                turn_partition.current,
                keep_recent_rounds=0,
            )
            current_turn_span = bool(span_rows)
            if current_turn_span:
                logger.warning(
                    "[compactor] protected current turn exceeds budget; "
                    f"using unprotected closed-round compaction session={session_id}"
                )
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
        def _span_fingerprint(selected_rows):
            return tuple(
                (
                    str(row.id),
                    str(getattr(row, "role", "") or ""),
                    str(getattr(row, "content", "") or ""),
                    json.dumps(
                        getattr(row, "message_meta", None) or {},
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                )
                for row in selected_rows
            )

        expected_span_fingerprint = _span_fingerprint(span_rows)
        summary_rows = (
            [turn_partition.current.user_row, *span_rows]
            if current_turn_span and turn_partition.current is not None
            else span_rows
        )

        # 3.5 Internal span sizing is observability only. It must not veto a
        # required compaction because it is not a provider token count.
        span_text = serialize_span_for_summary(
            summary_rows,
            prefilter=False,
            wrap_user_names=wrap_user_names,
            name_map=sender_name_map,
        )
        # 4. Summarize the exact serialized view used for the futility estimate.
        # Keep an unfiltered source for the lossless continuity decision.  The
        # summary provider sees a bounded head/tail view, but that optimization
        # must never make a large requirement in the middle unverifiable.
        unfiltered_span_text = serialize_span_for_summary(
            summary_rows,
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
            summary_rows,
            prefilter=False,
            wrap_user_names=wrap_user_names,
            name_map=sender_name_map,
            elide_durable_only=True,
        )
        prefilter_omitted_inline_content = span_text != durable_elided_span_text
        objective_evidence_items = objective_evidence_items_from_rows(
            prior_summary=prior_summary,
            rows=summary_rows,
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
        fresh_partition = partition_turns(
            fresh_rows,
            current_anchor_id=str(current_anchor_id) if current_anchor_id else None,
            keep_recent_turns=selected_keep,
            minimum_protected_turns=0,
        )
        fresh_span = fresh_partition.compactable_rows
        if current_turn_span and not fresh_span:
            fresh_span = current_turn_compactable_rows(
                fresh_partition.current,
                keep_recent_rounds=0,
            )
        if _span_fingerprint(fresh_span) != expected_span_fingerprint:
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
    from app.services.message_context_order import order_messages_for_context

    result = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.compacted_into.is_(None),
        )
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .execution_options(populate_existing=True)
    )
    from app.services.llm.queued_media_history import executed_media_rows

    return executed_media_rows(order_messages_for_context([
        row
        for row in result.scalars().all()
        if not (
            isinstance(getattr(row, "message_meta", None), dict)
            and (
                row.message_meta.get("consumed_by_onmessage")
                or row.message_meta.get("turn_inbox_state") in {"pending", "processing", "cancelled"}
            )
        )
    ]))


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

__all__ = (
    'maybe_compact',
    'maybe_precompact_prompt',
    '_do_compact',
    '_load_active_rows',
    '_load_compaction_state',
    '_load_active_marker',
)
