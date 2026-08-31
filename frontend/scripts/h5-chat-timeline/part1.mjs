import assert from 'node:assert/strict';

export function runH5ChatTimelinePart1(ctx) {
    const {
        foldConversationTimelineEvent,
        reduceConversationTurnEvent,
        IDLE_CONVERSATION_TURN,
        conversationTurnEventClosesStream,
        beginConversationTurnRecovery,
        mapHistoryMessage,
        normalizeChatTimelineMessages,
        conversationTurnIsWaiting,
        projectConversationTurnProgress,
        applyUserMessageCommitted,
        applyConfirmationRequiredEvent,
        applyAssistantMessageCommitted,
        toolCallMessageFromEvent,
        upsertToolCallMessage,
        buildH5ConversationEntries,
    } = ctx;

{
    const initial = [{ id: 'u1', role: 'user', content: 'keep' }];
    const draft = foldConversationTimelineEvent(initial, {
        type: 'workspace_draft',
        name: 'write_file',
        arguments: '{"path":"a.txt"}',
    });
    assert.equal(draft.handled, false);
    assert.equal(draft.messages, initial);

    const rejected = foldConversationTimelineEvent([
        ...initial,
        { id: 'optimistic-c2', role: 'user', content: 'reject me' },
    ], {
        type: 'error',
        rejected_message_id: 'optimistic-c2',
        content: 'busy',
    });
    assert.deepEqual(rejected.messages.map((message) => message.id), ['u1']);

    const active = {
        snapshot: {
            turnAnchorId: 'turn-a',
            generation: 7,
            revision: 3,
            status: 'running',
            phase: 'active',
        },
        presentation: 'streaming',
        canonical: true,
    };
    const concurrentRejection = {
        type: 'error',
        event_kind: 'turn_rejected',
        rejected_message_id: 'optimistic-c2',
        content: 'quota reached',
        turn: {
            turn_anchor_id: 'turn-a',
            generation: 7,
            revision: 3,
            status: 'running',
            phase: 'active',
        },
    };
    const reduced = reduceConversationTurnEvent(active, concurrentRejection);
    assert.equal(reduced.runtime, active, 'rejection must not terminate or replace the active runtime');
    assert.equal(reduced.controlsLifecycle, false);
    assert.equal(reduced.deliversTimeline, false);
    assert.equal(reduced.deliversTransport, true);
    const folded = foldConversationTimelineEvent([
        { id: 'turn-a-stream', role: 'assistant', content: 'A1', streaming: true, turnAnchorId: 'turn-a' },
        { id: 'optimistic-c2', role: 'user', content: 'reject me' },
    ], concurrentRejection);
    assert.deepEqual(folded.messages.map((message) => message.id), ['turn-a-stream']);
    assert.equal(folded.messages[0].content, 'A1');

    const recovered = reduceConversationTurnEvent(IDLE_CONVERSATION_TURN, concurrentRejection);
    assert.equal(recovered.controlsLifecycle, true);
    assert.equal(recovered.runtime.snapshot.turnAnchorId, 'turn-a');
    assert.equal(recovered.runtime.presentation, 'waiting');
    assert.equal(conversationTurnEventClosesStream(recovered, concurrentRejection), false);
}

{
    const activeStreaming = {
        snapshot: { turnAnchorId: 'resume-a', generation: 2, revision: 4, status: 'running', phase: 'active' },
        presentation: 'streaming',
        canonical: true,
    };
    const connected = reduceConversationTurnEvent(beginConversationTurnRecovery(activeStreaming), {
        type: 'connected',
        turn: { turn_anchor_id: 'resume-a', generation: 2, revision: 4, status: 'running', phase: 'active' },
    });
    assert.equal(connected.runtime.presentation, 'waiting', 'a new connection has no live stream row yet');
    const racedChunk = reduceConversationTurnEvent(beginConversationTurnRecovery(activeStreaming), {
        type: 'chunk',
        content: 'arrived after registration',
        turn: { turn_anchor_id: 'resume-a', generation: 2, revision: 4, status: 'running', phase: 'active' },
    });
    const racedConnected = reduceConversationTurnEvent(racedChunk.runtime, {
        type: 'connected',
        turn: { turn_anchor_id: 'resume-a', generation: 2, revision: 4, status: 'running', phase: 'active' },
    });
    assert.equal(racedConnected.runtime.presentation, 'streaming', 'connected must not override a frame already seen on the new transport');
}

{
    const lifecycleTurn = { turn_anchor_id: 'group-a', generation: 1, revision: 2, status: 'running', phase: 'active' };
    let messages = [
        { id: 'group-a', role: 'user', content: 'A' },
        { id: 'group-b', role: 'user', content: 'B' },
    ];
    messages = foldConversationTimelineEvent(messages, {
        type: 'chunk',
        content: 'B stream',
        message_id: 'b-stream',
        producer_scope: 'project:child-b:anchor-b',
        timeline_anchor_id: 'group-b',
        turn: lifecycleTurn,
    }).messages;
    messages = foldConversationTimelineEvent(messages, {
        type: 'done',
        content: 'B final',
        message_id: 'b-final',
        transient_message_id: 'b-stream',
        producer_scope: 'project:child-b:anchor-b',
        timeline_anchor_id: 'group-b',
        turn: lifecycleTurn,
    }).messages;
    assert.equal(messages.map((message) => message.id).join(','), 'group-a,group-b,b-final');

    const pending = { id: 'confirm-b', role: 'tool_call', content: '', toolCallId: 'confirm-b', _toolCallIdExplicit: true, toolName: 'request_confirmation', toolStatus: 'running', turnAnchorId: 'group-b', producerScope: 'project:child-b:anchor-b' };
    const resolvedEvent = {
        type: 'tool_call',
        name: 'request_confirmation',
        call_id: 'confirm-b',
        status: 'done',
        result: 'approved',
        producer_scope: 'project:child-b:anchor-b',
        timeline_anchor_id: 'group-b',
        turn: lifecycleTurn,
    };
    for (const viewer of [[pending], [{ ...pending }]]) {
        const resolved = foldConversationTimelineEvent(viewer, resolvedEvent).messages;
        assert.equal(resolved[0].toolStatus, 'done');
        assert.equal(resolved[0].toolResult, 'approved');
    }

    let reloaded = [mapHistoryMessage({
        id: 'a-final',
        role: 'assistant',
        content: 'A final',
        turnAnchorId: 'group-a',
        producerScope: 'project:child-a:anchor-a',
        canonicalDone: true,
    })];
    reloaded = foldConversationTimelineEvent(reloaded, {
        type: 'chunk',
        content: 'late A',
        message_id: 'a-late',
        producer_scope: 'project:child-a:anchor-a',
        timeline_anchor_id: 'group-a',
        turn: lifecycleTurn,
    }).messages;
    assert.equal(reloaded.map((message) => message.id).join(','), 'a-final');
    reloaded = foldConversationTimelineEvent(reloaded, {
        type: 'chunk',
        content: 'live B',
        message_id: 'b-live',
        producer_scope: 'project:child-b:anchor-b',
        timeline_anchor_id: 'group-b',
        turn: lifecycleTurn,
    }).messages;
    assert.equal(reloaded.map((message) => message.id).join(','), 'a-final,b-live');
}

{
    const turn = { turn_anchor_id: 'turn-tool', generation: 1, revision: 1, status: 'running', phase: 'active' };
    let messages = [{ id: 'turn-tool', role: 'user', content: 'run tools' }];
    messages = foldConversationTimelineEvent(messages, { type: 'chunk', content: 'before', turn }).messages;
    messages = foldConversationTimelineEvent(messages, { type: 'tool_call', name: 'read_file', call_id: 'tool-1', status: 'done', result: 'ok', turn }).messages;
    messages = foldConversationTimelineEvent(messages, { type: 'chunk', content: 'after', turn }).messages;
    assert.equal(messages.map((message) => message.role).join(','), 'user,assistant,tool_call,assistant');
    assert.equal(messages[1].content, 'before');
    assert.equal(messages[3].content, 'after');
    messages = foldConversationTimelineEvent(messages, { type: 'done', content: 'final answer', message_id: 'turn-tool-final', turn }).messages;
    assert.equal(messages.map((message) => `${message.role}:${message.content}`).join('|'), 'user:run tools|tool_call:|assistant:final answer');
}

{
    const lifecycleTurn = { turn_anchor_id: 'group-b', generation: 4, revision: 8, status: 'running', phase: 'active' };
    const producer = 'project:child-b:anchor-b';
    let messages = [{ id: 'group-b', role: 'user', content: 'B' }];
    messages = foldConversationTimelineEvent(messages, { type: 'chunk', content: 'B before', producer_scope: producer, timeline_anchor_id: 'group-b', turn: lifecycleTurn }).messages;
    messages.push({ id: 'group-c', role: 'user', content: 'C' });
    messages = foldConversationTimelineEvent(messages, { type: 'tool_call', name: 'read_file', call_id: 'b-tool', status: 'done', result: 'ok', producer_scope: producer, timeline_anchor_id: 'group-b', turn: lifecycleTurn }).messages;
    messages = foldConversationTimelineEvent(messages, { type: 'chunk', content: 'B after', producer_scope: producer, timeline_anchor_id: 'group-b', turn: lifecycleTurn }).messages;
    assert.equal(messages.map((message) => message.role).join(','), 'user,assistant,tool_call,assistant,user');
    assert.equal(messages.at(-1).id, 'group-c');
    messages = foldConversationTimelineEvent(messages, { type: 'done', content: 'B final', message_id: 'b-final-partition', producer_scope: producer, timeline_anchor_id: 'group-b', turn: lifecycleTurn }).messages;
    assert.equal(messages.map((message) => message.role).join(','), 'user,tool_call,assistant,user');
    assert.equal(messages[1].toolCallId, 'b-tool');
    assert.equal(messages[2].id, 'b-final-partition');
}

{
    const paged = normalizeChatTimelineMessages([
        { id: 'old-row', role: 'tool_call', content: '', toolName: 'read_file', toolCallId: 'reused-call', _toolCallIdExplicit: true, toolStatus: 'done', turnAnchorId: 'turn-old' },
        { id: 'new-row', role: 'tool_call', content: '', toolName: 'read_file', toolCallId: 'reused-call', _toolCallIdExplicit: true, toolStatus: 'done', turnAnchorId: 'turn-new' },
    ]);
    assert.equal(paged.length, 2, 'pagination must scope reused call ids by turn');
}

{
    let messages = [{ id: 'group-anchor', role: 'user', content: 'parallel' }];
    const stream = (type, producer, messageId, content, transientMessageId) => {
        messages = foldConversationTimelineEvent(messages, {
            type,
            content,
            message_id: messageId,
            transient_message_id: transientMessageId,
            producer_scope: producer,
            turn: { turn_anchor_id: 'group-anchor', generation: 1 },
        }).messages;
    };
    stream('chunk', 'producer-a', 'stream-a', 'A1');
    stream('chunk', 'producer-b', 'stream-b', 'B1');
    stream('chunk', 'producer-a', 'stream-a', 'A2');
    assert.equal(messages.find((message) => message.id === 'stream-a').content, 'A1A2');
    assert.equal(messages.find((message) => message.id === 'stream-b').content, 'B1');

    stream('done', 'producer-a', 'durable-a', 'A final', 'stream-a');
    stream('chunk', 'producer-a', 'stream-a', ' late ghost');
    stream('chunk', 'producer-b', 'stream-b', 'B2');
    assert.equal(messages.some((message) => message.id === 'stream-a'), false);
    assert.equal(messages.find((message) => message.id === 'durable-a').content, 'A final');
    assert.equal(messages.find((message) => message.id === 'stream-b').content, 'B1B2');
}

{
    const active = reduceConversationTurnEvent(IDLE_CONVERSATION_TURN, {
        type: 'chunk',
        content: 'B',
        turn: {
            turn_anchor_id: 'turn-b', generation: 2, revision: 1,
            status: 'running', phase: 'active',
        },
    });
    const receipt = reduceConversationTurnEvent(active.runtime, {
        type: 'turn_receipt',
        status: 'delegated',
        turn: {
            turn_anchor_id: 'turn-b', generation: 2, revision: 1,
            status: 'running', phase: 'active',
        },
    });
    assert.equal(receipt.deliversTimeline, false);
    assert.equal(receipt.deliversTransport, true);
    assert.equal(receipt.runtime.presentation, 'streaming');

    const newerCohort = reduceConversationTurnEvent(active.runtime, {
        type: 'turn_state',
        turn: {
            turn_anchor_id: 'turn-b', generation: 2, revision: 3,
            status: 'running', phase: 'active', active_agent_ids: ['producer-b'],
        },
    });
    const olderProducerChunk = reduceConversationTurnEvent(newerCohort.runtime, {
        type: 'chunk', content: 'late from producer A', producer_scope: 'producer-a',
        turn: {
            turn_anchor_id: 'turn-b', generation: 2, revision: 2,
            status: 'running', phase: 'active',
        },
    });
    assert.equal(olderProducerChunk.deliversTimeline, true);
    assert.equal(olderProducerChunk.controlsLifecycle, false);
    const unscopedOldChunk = reduceConversationTurnEvent(newerCohort.runtime, {
        type: 'chunk', content: 'pre-confirmation late chunk',
        turn: {
            turn_anchor_id: 'turn-b', generation: 2, revision: 2,
            status: 'running', phase: 'active',
        },
    });
    assert.equal(unscopedOldChunk.deliversTimeline, false);

    const terminal = reduceConversationTurnEvent(newerCohort.runtime, {
        type: 'turn_state',
        turn: {
            turn_anchor_id: 'turn-b', generation: 2, revision: 4,
            status: 'completed', phase: 'idle',
        },
    });
    const afterTerminal = reduceConversationTurnEvent(terminal.runtime, {
        type: 'chunk', content: 'too late', producer_scope: 'producer-a',
        turn: {
            turn_anchor_id: 'turn-b', generation: 2, revision: 2,
            status: 'running', phase: 'active',
        },
    });
    assert.equal(afterTerminal.deliversTimeline, false);
}

{
    const runningTool = reduceConversationTurnEvent(IDLE_CONVERSATION_TURN, {
        type: 'tool_call',
        status: 'running',
        turn: {
            turn_anchor_id: 'tool-gap-turn',
            generation: 1,
            revision: 1,
            status: 'running',
            phase: 'active',
        },
    });
    const completedTool = reduceConversationTurnEvent(runningTool.runtime, {
        type: 'tool_call',
        status: 'done',
        turn: {
            turn_anchor_id: 'tool-gap-turn',
            generation: 1,
            revision: 1,
            status: 'running',
            phase: 'active',
        },
    });
    const projected = projectConversationTurnProgress([
        { id: 'tool-gap-turn', role: 'user', content: 'run tool' },
        { id: 'tool-result', role: 'tool_call', content: '', toolName: 'search', toolCallId: 'call-1', toolStatus: 'done' },
    ], conversationTurnIsWaiting(completedTool.runtime));
    assert.equal(conversationTurnIsWaiting(completedTool.runtime), true);
    assert.equal(projected.filter((message) => message._conversationTurnProgress).length, 1);
    assert.equal(projected.at(-1)._conversationTurnProgress, true);
}

{
    const committedPayload = {
        type: 'user_message_committed',
        client_message_id: 'client-user',
        message_id: 'durable-user',
        content: 'new turn',
        display_content: 'new turn',
        attachments: [],
        sender_user_id: 'human-1',
    };
    const driver = applyUserMessageCommitted([
        { id: 'client-user', role: 'user', content: 'new turn' },
    ], committedPayload);
    assert.equal(driver.length, 1);
    assert.equal(driver[0].id, 'durable-user');

    const viewer = applyUserMessageCommitted([], committedPayload);
    assert.equal(viewer.length, 1);
    assert.equal(viewer[0].id, 'durable-user');
    assert.equal(viewer[0].role, 'user');

    const blocked = applyConfirmationRequiredEvent([
        { id: 'blocked-client-user', role: 'user', content: 'must not persist' },
    ], {
        type: 'confirmation_required',
        message_id: 'blocked-client-user',
        name: 'request_confirmation',
        call_id: 'pending-confirmation',
        args: { force_confirmation: true },
        turn: {
            turn_anchor_id: 'suspended-anchor',
            generation: 1,
        },
    });
    assert.equal(blocked.some((message) => message.id === 'blocked-client-user'), false);
    assert.equal(blocked.length, 1);
    assert.equal(blocked[0].role, 'tool_call');
    assert.equal(blocked[0].turnAnchorId, 'suspended-anchor');
}

{
    let messages = [
        { id: 'group-user', role: 'user', content: 'parallel', turnAnchorId: 'group-user' },
        { id: 'stream-a', role: 'assistant', content: 'draft A', streaming: true, _streaming: true, turnAnchorId: 'group-user', producerScope: 'producer-a' },
        { id: 'stream-b', role: 'assistant', content: 'draft B', streaming: true, _streaming: true, turnAnchorId: 'group-user', producerScope: 'producer-b' },
    ];
    messages = applyAssistantMessageCommitted(messages, {
        type: 'assistant_message_committed',
        id: 'durable-a',
        content: 'corrected A',
        transient_message_id: 'stream-a',
        producer_scope: 'producer-a',
        turn: { turn_anchor_id: 'group-user', generation: 1 },
    });
    assert.equal(messages.some((message) => message.id === 'stream-a'), false);
    assert.equal(messages.find((message) => message.id === 'durable-a').content, 'corrected A');
    assert.equal(messages.find((message) => message.id === 'stream-b').streaming, true);

    const late = applyAssistantMessageCommitted([
        { id: 'turn-a', role: 'user', content: 'A' },
        { id: 'stream-late-a', role: 'assistant', content: 'draft A', streaming: true, _streaming: true, turnAnchorId: 'turn-a', producerScope: 'producer-late-a' },
        { id: 'turn-b', role: 'user', content: 'B' },
        { id: 'stream-current-b', role: 'assistant', content: 'draft B', streaming: true, _streaming: true, turnAnchorId: 'turn-b', producerScope: 'producer-current-b' },
    ], {
        type: 'assistant_message_committed',
        id: 'durable-late-a',
        content: 'final A',
        transient_message_id: 'stream-late-a',
        producer_scope: 'producer-late-a',
        turn: { turn_anchor_id: 'turn-a', generation: 1 },
    });
    assert.deepEqual(
        Array.from(late.map((message) => message.id)),
        ['turn-a', 'durable-late-a', 'turn-b', 'stream-current-b'],
    );

    const toolA = toolCallMessageFromEvent({
        type: 'tool_call', call_id: 'shared-call', name: 'search', status: 'running',
        producer_scope: 'producer-a', turn: { turn_anchor_id: 'group-user', generation: 1 },
    });
    const toolB = toolCallMessageFromEvent({
        type: 'tool_call', call_id: 'shared-call', name: 'search', status: 'running',
        producer_scope: 'producer-b', turn: { turn_anchor_id: 'group-user', generation: 1 },
    });
    const tools = upsertToolCallMessage(upsertToolCallMessage([], toolA), toolB);
    assert.equal(tools.length, 2);
    assert.notEqual(toolA.id, toolB.id);

    const reusedAcrossTurns = [
        { id: 'call-turn-a', role: 'user', content: 'A' },
        toolCallMessageFromEvent({ type: 'tool_call', call_id: 'same-call', name: 'search', status: 'done', turn: { turn_anchor_id: 'call-turn-a', generation: 1 } }),
        { id: 'call-turn-b', role: 'user', content: 'B' },
        toolCallMessageFromEvent({ type: 'tool_call', call_id: 'same-call', name: 'search', status: 'done', turn: { turn_anchor_id: 'call-turn-b', generation: 2 } }),
    ];
    assert.notEqual(reusedAcrossTurns[1].id, reusedAcrossTurns[3].id);
    const entryKeys = buildH5ConversationEntries(reusedAcrossTurns).map((entry) => entry.key);
    assert.equal(new Set(entryKeys).size, entryKeys.length);

    const legacyAndCanonical = upsertToolCallMessage([
        { id: 'turn-a', role: 'user', content: 'A' },
        { id: 'legacy-tool', role: 'tool_call', content: '', toolName: 'search', toolCallId: 'reused-call', toolStatus: 'done' },
        { id: 'turn-b', role: 'user', content: 'B' },
    ], toolCallMessageFromEvent({
        type: 'tool_call', call_id: 'reused-call', name: 'search', status: 'running',
        turn: { turn_anchor_id: 'turn-b', generation: 2 },
    }));
    assert.equal(legacyAndCanonical.filter((message) => message.role === 'tool_call').length, 2);
    assert.equal(legacyAndCanonical.find((message) => message.id === 'legacy-tool').toolStatus, 'done');
}
}
