import assert from 'node:assert/strict';

export function runH5ChatTimelinePart2(ctx) {
    const {
        reduceConversationTurnEvent,
        IDLE_CONVERSATION_TURN,
        applyAssistantDoneMessage,
        conversationTurnEventShouldBeHandled,
        conversationTurnIsWaiting,
        projectConversationTurnProgress,
        buildH5ConversationEntries,
        isA2AMessageLeft,
        upsertToolCallMessage,
        normalizeChatTimelineMessages,
        mergeHistoryMessages,
        createResumeEventGate,
        bufferResumeEvent,
        shouldScheduleResumeReconnect,
        createOrReuseResumeEventGate,
    } = ctx;

{
    const secondRunning = reduceConversationTurnEvent(IDLE_CONVERSATION_TURN, {
        type: 'turn_state',
        turn: {
            turn_anchor_id: 'anchor-2',
            generation: 2,
            revision: 1,
            status: 'running',
            phase: 'active',
        },
    });
    assert.equal(secondRunning.accepted, true);

    const lateFirstDone = reduceConversationTurnEvent(secondRunning.runtime, {
        type: 'done',
        content: 'late first terminal',
        turn: {
            turn_anchor_id: 'anchor-1',
            generation: 1,
            revision: 2,
            status: 'completed',
            phase: 'idle',
        },
    });
    assert.equal(
        lateFirstDone.accepted,
        true,
        'a late exact-anchor terminal must still reach the message timeline',
    );
    assert.equal(lateFirstDone.controlsLifecycle, false);
    assert.equal(lateFirstDone.deliversTimeline, true);
    assert.deepEqual(lateFirstDone.runtime, secondRunning.runtime);

    const lateFirstChunk = reduceConversationTurnEvent(secondRunning.runtime, {
        type: 'chunk',
        content: 'must never recreate A transport',
        turn: {
            turn_anchor_id: 'anchor-1',
            generation: 1,
            revision: 1,
            status: 'running',
            phase: 'active',
        },
    });
    assert.equal(lateFirstChunk.accepted, false);
    assert.equal(lateFirstChunk.deliversTimeline, false);
    assert.deepEqual(lateFirstChunk.runtime, secondRunning.runtime);

    const lateFirstTimeline = applyAssistantDoneMessage([
        { id: 'anchor-1', role: 'user', content: 'A' },
        {
            id: 'stream-a',
            role: 'assistant',
            content: 'A partial',
            streaming: true,
            _streaming: true,
            turnAnchorId: 'anchor-1',
            turnGeneration: 1,
        },
        { id: 'anchor-2', role: 'user', content: 'B' },
        {
            id: 'stream-b',
            role: 'assistant',
            content: 'B partial',
            streaming: true,
            _streaming: true,
            turnAnchorId: 'anchor-2',
            turnGeneration: 2,
        },
    ], {
        content: 'A final',
        messageId: 'final-a',
        turnAnchorId: 'anchor-1',
        turnGeneration: 1,
    });
    assert.equal(
        Array.from(lateFirstTimeline, (message) => message.id).join(','),
        'anchor-1,final-a,anchor-2,stream-b',
    );
    assert.equal(lateFirstTimeline[1].content, 'A final');
    assert.equal(lateFirstTimeline[3].streaming, true);
    assert.equal(lateFirstTimeline[3].content, 'B partial');

    const firstTerminal = reduceConversationTurnEvent(IDLE_CONVERSATION_TURN, {
        type: 'done',
        turn: {
            turn_anchor_id: 'anchor-terminal',
            generation: 1,
            revision: 2,
            status: 'completed',
            phase: 'idle',
        },
    });
    const lowerRevisionChunk = reduceConversationTurnEvent(firstTerminal.runtime, {
        type: 'chunk',
        content: 'buffered before terminal',
        turn: {
            turn_anchor_id: 'anchor-terminal',
            generation: 1,
            revision: 1,
            status: 'running',
            phase: 'active',
        },
    });
    assert.equal(lowerRevisionChunk.accepted, false);
    assert.equal(lowerRevisionChunk.deliversTimeline, false);
    assert.equal(lowerRevisionChunk.runtime.presentation, 'idle');

    const staleConnected = reduceConversationTurnEvent(secondRunning.runtime, {
        type: 'connected',
        session_id: 'session-under-test',
        read_only: true,
        turn: {
            turn_anchor_id: 'anchor-1',
            generation: 1,
            revision: 2,
            status: 'completed',
            phase: 'idle',
        },
    });
    assert.equal(staleConnected.controlsLifecycle, false);
    assert.equal(staleConnected.deliversTimeline, false);
    assert.equal(staleConnected.deliversTransport, true);
    assert.equal(conversationTurnEventShouldBeHandled(staleConnected), true);
    assert.deepEqual(staleConnected.runtime, secondRunning.runtime);

    const unrelatedLegacyDone = reduceConversationTurnEvent(secondRunning.runtime, {
        type: 'done',
        content: 'external committed assistant message',
    });
    assert.equal(unrelatedLegacyDone.accepted, true);
    assert.equal(
        unrelatedLegacyDone.controlsLifecycle,
        false,
        'legacy mirrors cannot terminate a session after canonical negotiation',
    );
    assert.deepEqual(unrelatedLegacyDone.runtime, secondRunning.runtime);

    const canonicalIdle = reduceConversationTurnEvent(IDLE_CONVERSATION_TURN, {
        type: 'connected',
        turn: {
            turn_anchor_id: null,
            generation: 0,
            revision: 0,
            status: 'idle',
            phase: 'idle',
        },
    });
    const unrelatedToolDone = reduceConversationTurnEvent(canonicalIdle.runtime, {
        type: 'tool_call',
        call_id: 'external-media-delivery',
        status: 'done',
    });
    assert.equal(unrelatedToolDone.controlsLifecycle, false);
    assert.equal(unrelatedToolDone.runtime.snapshot.phase, 'idle');
    assert.equal(unrelatedToolDone.runtime.presentation, 'idle');

    const idleRejection = reduceConversationTurnEvent(canonicalIdle.runtime, {
        type: 'error',
        code: 'turn_capacity_busy',
        turn: {
            turn_anchor_id: null,
            generation: 0,
            revision: 0,
            status: 'idle',
            phase: 'idle',
        },
    });
    assert.equal(idleRejection.controlsLifecycle, true);
    assert.equal(idleRejection.runtime.presentation, 'idle');
    assert.equal(idleRejection.runtime.snapshot.phase, 'idle');

    const mirrored = applyAssistantDoneMessage([
        { id: 'user-b', role: 'user', content: 'B' },
        {
            id: 'stream-b',
            role: 'assistant',
            content: 'B partial',
            streaming: true,
            _streaming: true,
        },
    ], {
        content: 'external committed assistant message',
        messageId: 'external-message',
        preserveTransient: true,
    });
    assert.equal(mirrored.length, 3);
    assert.equal(mirrored[1].id, 'stream-b');
    assert.equal(mirrored[1].streaming, true);
    assert.equal(mirrored[2].id, 'external-message');
    assert.equal(mirrored[2].content, 'external committed assistant message');
}

{
    const projected = projectConversationTurnProgress([
        {
            id: 'thinking-transport-row',
            role: 'assistant',
            content: '',
            thinking: 'reasoning before tool',
            _streaming: true,
        },
        {
            id: 'tool-row',
            role: 'tool_call',
            content: '',
            toolCallId: 'call-1',
            toolName: 'execute',
            toolStatus: 'done',
        },
    ], true, { id: 'conversation-turn-progress:session:7' });
    const entries = buildH5ConversationEntries(projected);
    assert.equal(
        entries.filter((entry) => entry.type === 'message' && entry.msg._conversationTurnProgress).length,
        1,
        'one lifecycle projection must own the only visible progress row',
    );
    assert.equal(entries.at(-1).type, 'message', 'progress must follow all durable/tool entries');
    assert.equal(entries.at(-1).msg._conversationTurnProgress, true);
    assert.equal(entries[0].type, 'analysis_group');
    assert.equal(entries[0].items[0].content, 'reasoning before tool');
}

{
    const currentAgentId = 'agent-engineer';
    assert.equal(
        isA2AMessageLeft({ sender_agent_id: 'agent-architect' }),
        true,
        'the peer Agent must render on the left regardless of its stored LLM role',
    );
    assert.equal(
        isA2AMessageLeft({ sender_agent_id: currentAgentId }),
        true,
        'the Agent whose page is open must also render on the left',
    );
    assert.equal(
        isA2AMessageLeft({ sender_user_id: 'human-owner' }),
        false,
        'a human-authored A2A timeline message must render on the right',
    );
    assert.equal(
        isA2AMessageLeft({}),
        true,
        'legacy actorless A2A rows must not guess ownership from role',
    );
}

{
    const firstTurn = {
        id: 'tool-row-1',
        role: 'tool_call',
        content: '',
        toolCallId: 'reused-call-id',
        toolName: 'read_file',
        toolStatus: 'done',
        turnAnchorId: 'anchor-1',
        turnGeneration: 1,
    };
    const secondTurn = {
        ...firstTurn,
        id: 'tool-row-2',
        toolStatus: 'running',
        turnAnchorId: 'anchor-2',
        turnGeneration: 2,
    };
    const live = upsertToolCallMessage([firstTurn], secondTurn);
    assert.equal(live.length, 2, 'a reused call_id must not overwrite another turn');
    assert.equal(live[1].turnAnchorId, 'anchor-2');
    const normalized = normalizeChatTimelineMessages([firstTurn, secondTurn]);
    assert.equal(normalized.length, 2, 'history normalization must scope tools by turn anchor');
    const merged = mergeHistoryMessages([firstTurn], [firstTurn, secondTurn]);
    assert.equal(merged.filter((message) => message.role === 'tool_call').length, 2);
}

{
    assert.equal(shouldScheduleResumeReconnect({
        pageSuspended: false,
        unmounted: false,
        hidden: false,
        socketReadyState: null,
    }), true, 'a deduped reconnect must be rescheduled after a failed coordinator socket');
    assert.equal(shouldScheduleResumeReconnect({
        pageSuspended: false,
        unmounted: false,
        hidden: false,
        socketReadyState: 3,
    }), true, 'a closed socket must be retried after resume reconciliation');
    assert.equal(shouldScheduleResumeReconnect({
        pageSuspended: false,
        unmounted: false,
        hidden: false,
        socketReadyState: 0,
    }), false, 'a connecting socket already has its own timeout and must not be duplicated');
    assert.equal(shouldScheduleResumeReconnect({
        pageSuspended: false,
        unmounted: false,
        hidden: false,
        socketReadyState: 1,
    }), false, 'an open socket must not schedule another reconnect');
    assert.equal(shouldScheduleResumeReconnect({
        pageSuspended: true,
        unmounted: false,
        hidden: true,
        socketReadyState: 3,
    }), false, 'background pages must defer reconnect until foreground resume');

    const activeGate = createResumeEventGate('h5-session', 2);
    bufferResumeEvent(activeGate, 'h5-session', { type: 'chunk', value: 'must-replay' });
    const reused = createOrReuseResumeEventGate(activeGate, 'h5-session', 3);
    assert.equal(reused.gate, activeGate, 'an in-flight reconcile must reuse its existing event gate');
    assert.equal(reused.displacedEvents.length, 0);
    const replaced = createOrReuseResumeEventGate(activeGate, 'other-session', 3);
    assert.equal(replaced.gate.runtimeKey, 'other-session');
    assert.equal(
        JSON.stringify(replaced.displacedEvents),
        JSON.stringify([{ type: 'chunk', value: 'must-replay' }]),
        'a runtime switch must return buffered events for ordered replay instead of dropping them',
    );
}
}
