import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const ts = require('typescript');

const __dirname = dirname(fileURLToPath(import.meta.url));
function compileTsModule(sourcePath, requireOverride = require) {
    const source = readFileSync(sourcePath, 'utf8');
    const compiled = ts.transpileModule(source, {
        compilerOptions: {
            module: ts.ModuleKind.CommonJS,
            target: ts.ScriptTarget.ES2020,
            esModuleInterop: true,
        },
    }).outputText;

    const localModule = { exports: {} };
    vm.runInNewContext(compiled, {
        module: localModule,
        exports: localModule.exports,
        require: requireOverride,
        console,
        URL,
    }, { filename: sourcePath });
    return localModule;
}

const fileDeliveryPath = resolve(__dirname, '../src/utils/chatFileDelivery.ts');
const fileDeliveryModule = compileTsModule(fileDeliveryPath);
const { parseFileDeliveryToolResult, parseMediaDeliveryErrorResult } = fileDeliveryModule.exports;
const chatAttachmentsModule = compileTsModule(resolve(
    __dirname,
    '../src/utils/chatAttachments.ts',
));

// Compile the canonical shared conversation core directly. The H5 module is a
// compatibility facade over this file, so these fixtures lock the behaviour H5
// consumes while allowing Web to share the same transformations.
const sourcePath = resolve(__dirname, '../src/features/conversation/core/chatTimeline.ts');
const timelineRequire = (id) => {
    if (id === '../../../utils/chatFileDelivery') return fileDeliveryModule.exports;
    if (id === '../../../utils/chatAttachments') return chatAttachmentsModule.exports;
    if (id === '../../../utils/clientId') return { createClientId: () => 'test-client-id' };
    if (id === '../../../components/ChatToolCallRenderer') {
        return {
            getChatToolRenderType: (message) => (
                message?.role !== 'tool_call'
                    ? null
                    : message?.toolName === 'request_confirmation'
                        ? 'confirmation'
                        : message?.toolName === 'send_channel_file'
                            ? 'file-delivery'
                            : (parseFileDeliveryToolResult(
                                message?.toolName,
                                message?.toolResult,
                                message?.toolArgs,
                                message?.toolCallId,
                            )?.mediaKind || parseMediaDeliveryErrorResult(
                                message?.toolName,
                                message?.toolResult,
                            ))
                                ? 'media-delivery'
                                : null
            ),
            getChatToolRenderIdentity: (message) => {
                if (message?.role !== 'tool_call' || message?.toolName !== 'send_channel_file') return null;
                try {
                    const delivery = JSON.parse(message.toolResult || '{}');
                    return ['file-delivery', delivery.path, delivery.filename, delivery.message || ''].join(':');
                } catch {
                    return null;
                }
            },
        };
    }
    return require(id);
};

const module = compileTsModule(sourcePath, timelineRequire);
const lifecycleModule = compileTsModule(resolve(
    __dirname,
    '../src/features/conversation/core/conversationTurnLifecycle.ts',
));
const resumeRecoveryModule = compileTsModule(resolve(
    __dirname,
    '../src/features/conversation/core/resumeRecovery.ts',
));

const {
    applyAssistantDoneMessage,
    applyAssistantMessageCommitted,
    applyAssistantStreamMessage,
    applyConfirmationRequiredEvent,
    applyUserMessageCommitted,
    foldConversationTimelineEvent,
    buildConversationEntries: buildH5ConversationEntries,
    getConversationScrollAnchor: getH5ScrollAnchor,
    hasPendingConfirmation,
    isA2AMessageLeft,
    isConfirmationToolCall,
    latestHistoryWindowOverlaps,
    mapHistoryMessage,
    mergeHistoryMessages,
    normalizeChatTimelineMessages,
    projectConversationTurnProgress,
    reconcileLatestHistoryWindow,
    shouldProjectConversationTurnProgress,
    toolCallMessageFromEvent,
    upsertToolCallMessage,
} = module.exports;
const {
    bufferResumeEvent,
    createResumeEventGate,
    createOrReuseResumeEventGate,
    drainResumeEventGate,
    prepareMessagesForActiveTurnResume,
    shouldScheduleResumeReconnect,
} = resumeRecoveryModule.exports;
const {
    IDLE_CONVERSATION_TURN,
    beginConversationTurnRecovery,
    conversationTurnIsWaiting,
    conversationTurnEventClosesStream,
    conversationTurnEventShouldBeHandled,
    reduceConversationTurnEvent,
} = lifecycleModule.exports;

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
    const messages = [
        { id: 'failed-user', role: 'user', content: 'old request' },
        { id: 'failed-partial', role: 'assistant', content: 'keep old partial', streaming: true },
        { id: 'active-user', role: 'user', content: 'active request' },
        { id: 'active-partial', role: 'assistant', content: 'remove active partial', streaming: true },
        { id: 'active-tool', role: 'tool_call', toolCallId: 'tool-1' },
        { id: 'active-durable', role: 'assistant', content: 'keep committed output' },
    ];
    assert.deepEqual(
        prepareMessagesForActiveTurnResume(messages).map((message) => message.id),
        ['failed-user', 'failed-partial', 'active-user', 'active-tool', 'active-durable'],
        'H5 resume cleanup must affect only transient assistant rows in the active turn',
    );

    const gate = createResumeEventGate('h5-session', 1);
    assert.equal(bufferResumeEvent(gate, 'other-session', { type: 'chunk', value: 'wrong' }), false);
    assert.equal(bufferResumeEvent(gate, 'h5-session', { type: 'chunk', value: 'A' }), true);
    assert.equal(bufferResumeEvent(gate, 'h5-session', { type: 'tool_call', value: 'tool' }), true);
    assert.equal(bufferResumeEvent(gate, 'h5-session', { type: 'done', value: 'B' }), true);
    assert.equal(
        drainResumeEventGate(gate).map((event) => event.type).join(','),
        'chunk,tool_call,done',
        'H5 resume must replay buffered socket events in arrival order',
    );

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

{
    const loaded = [
        { id: 'older-a', role: 'user', content: 'older' },
        { id: 'overlap-b', role: 'assistant', content: 'old durable value' },
    ];
    const latest = [
        { id: 'overlap-b', role: 'assistant', content: 'new durable value' },
        { id: 'new-c', role: 'assistant', content: 'newest' },
    ];
    assert.equal(latestHistoryWindowOverlaps(loaded, latest), true);
    assert.equal(
        reconcileLatestHistoryWindow(loaded, latest).map((message) => message.id).join(','),
        'older-a,overlap-b,new-c',
    );
    assert.equal(reconcileLatestHistoryWindow(loaded, latest)[1].content, 'new durable value');
}

{
    const before = [
        { id: 'u', role: 'user', content: 'run' },
        { id: 'stream-before', role: 'assistant', content: 'before tool', streaming: true },
        { id: 'tool-row', role: 'tool_call', toolCallId: 'tool-row', toolStatus: 'done' },
        { id: 'stream-after', role: 'assistant', content: 'after tool', streaming: true },
    ];
    const durable = [
        { id: 'u', role: 'user', content: 'run' },
        { id: 'tool-row', role: 'tool_call', toolCallId: 'tool-row', toolStatus: 'done' },
    ];
    assert.equal(
        reconcileLatestHistoryWindow(before, durable).map((message) => message.id).join(','),
        'u,stream-before,tool-row,stream-after',
        'recovery reconciliation must preserve local stream/tool ordering',
    );
}

{
    const before = [
        { id: 'u', role: 'user', content: 'run' },
        { id: 'tool-1', role: 'tool_call', toolCallId: 'tool-1', toolStatus: 'done' },
        { id: 'stream-before-disconnect', role: 'assistant', content: 'visible before disconnect', streaming: true },
    ];
    const durable = [
        { id: 'u', role: 'user', content: 'run' },
        { id: 'tool-1', role: 'tool_call', toolCallId: 'tool-1', toolStatus: 'done' },
        { id: 'tool-2', role: 'tool_call', toolCallId: 'tool-2', toolStatus: 'done' },
    ];
    assert.equal(
        reconcileLatestHistoryWindow(before, durable).map((message) => message.id).join(','),
        'u,tool-1,stream-before-disconnect,tool-2',
        'a pre-disconnect transient tail must stay before durable rows added during recovery',
    );
}

{
    const loaded = [{ id: 'old-window', role: 'user', content: 'old' }];
    const latest = [{ id: 'latest-window', role: 'assistant', content: 'latest' }];
    assert.equal(latestHistoryWindowOverlaps(loaded, latest), false);
    assert.equal(
        reconcileLatestHistoryWindow(loaded, latest).map((message) => message.id).join(','),
        'latest-window',
    );
}

{
    const mapped = mapHistoryMessage({
        id: 'user-with-attachments',
        role: 'user',
        content: '[file:a.jpg]\n正文',
        display_content: '正文',
        attachments: [{ display_name: 'a.jpg', path: 'workspace/uploads/a.jpg', kind: 'image' }],
        created_at: '2026-08-07T00:00:00Z',
    });
    assert.equal(mapped.id, 'user-with-attachments');
    assert.equal(mapped.display_content, '正文');
    assert.equal(mapped.attachments.length, 1);
}

{
    const error = parseMediaDeliveryErrorResult('send_media', JSON.stringify({
        type: 'media_delivery_result',
        status: 'unknown',
        code: 'MEDIA_DELIVERY_STATE_UNKNOWN',
        message: '发送结果不确定，媒体可能已经送达；不要自动重试，以免重复发送。',
        retryable: false,
        media_kind: 'video',
    }));
    assert.equal(error.status, 'unknown');
    assert.equal(error.code, 'MEDIA_DELIVERY_STATE_UNKNOWN');
    assert.equal(error.retryable, false);
}

{
    const delivery = parseFileDeliveryToolResult(
        'send_media',
        JSON.stringify({
            type: 'platform_media_delivery',
            version: 1,
            status: 'sent',
            media_kind: 'video',
            path: 'workspace/media/demo.mp4',
            filename: 'demo.mp4',
            title: '示例媒体标题',
            mime_type: 'video/mp4',
            size: 42,
            message_id: 'tool-message-1',
            allow_download: true,
        }),
        {},
        'media-delivery:call-1',
    );
    assert.equal(delivery.mediaKind, 'video');
    assert.equal(delivery.title, '示例媒体标题');
    assert.equal(delivery.messageId, 'tool-message-1');
    assert.equal(delivery.allowDownload, true);
}

{
    const delivery = parseFileDeliveryToolResult(
        'send_media',
        JSON.stringify({
            type: 'platform_media_delivery',
            version: 1,
            status: 'sent',
            media_kind: 'audio',
            source_mode: 'external_url',
            url: 'https://media.example/voice.mp3?token=temporary',
            filename: 'voice.mp3',
            message_id: 'tool-message-external',
            allow_download: false,
        }),
        {},
        'media-delivery:external',
    );
    assert.equal(delivery.mediaKind, 'audio');
    assert.equal(delivery.path, undefined);
    assert.equal(delivery.url, 'https://media.example/voice.mp3?token=temporary');
    assert.equal(delivery.sourceMode, 'external_url');
    assert.equal(delivery.allowDownload, false);
}

{
    const delivery = parseFileDeliveryToolResult(
        'send_media',
        JSON.stringify({
            type: 'platform_media_delivery',
            status: 'sent',
            media_kind: 'video',
            source_mode: 'external_url',
            url: 'http://media.example/demo.mp4',
            filename: 'demo.mp4',
        }),
    );
    assert.equal(delivery, null);
}

{
    const normalized = normalizeChatTimelineMessages([
        { id: 'confirmation', role: 'tool_call', toolCallId: 'confirmation', toolName: 'request_confirmation', toolStatus: 'done' },
        { id: 'empty-completed', role: 'assistant', content: '   ', streaming: false },
        { id: 'thinking-only', role: 'assistant', content: '', thinking: '仍需展示思考过程' },
        { id: 'streaming-placeholder', role: 'assistant', content: '', streaming: true },
        { id: 'attachment-only', role: 'assistant', content: '', fileName: 'report.pdf' },
        {
            id: 'structured-media-only',
            role: 'assistant',
            content: '',
            attachments: [{ display_name: 'demo.mp4', path: 'workspace/media/demo.mp4', kind: 'video' }],
        },
        { id: 'final', role: 'assistant', content: '确认流程已完成' },
    ]);

    assert.equal(normalized.some((message) => message.id === 'empty-completed'), false);
    assert.equal(normalized.some((message) => message.id === 'thinking-only'), true);
    assert.equal(normalized.some((message) => message.id === 'streaming-placeholder'), true);
    assert.equal(normalized.some((message) => message.id === 'attachment-only'), true);
    assert.equal(normalized.some((message) => message.id === 'structured-media-only'), false);
    assert.equal(normalized.some((message) => message.id === 'final'), true);
}

{
    const entries = buildH5ConversationEntries([{
        id: 'media-only',
        role: 'user',
        content: '',
        attachments: [{ display_name: 'voice.mp3', path: 'workspace/media/voice.mp3', kind: 'audio' }],
    }]);

    assert.equal(entries.length, 1);
    assert.equal(entries[0].type, 'message');
    assert.equal(entries[0].msg.id, 'media-only');
}

{
    const entries = buildH5ConversationEntries([{
        id: 'media-error-tool-row',
        role: 'tool_call',
        content: '',
        toolName: 'send_media',
        toolCallId: 'media-error-call',
        toolStatus: 'done',
        toolResult: JSON.stringify({
            type: 'media_delivery_result',
            status: 'failed',
            code: 'MEDIA_SEND_FAILED',
            message: '目标通道拒绝或未完成媒体发送。',
            retryable: false,
            media_kind: 'video',
        }),
    }]);

    assert.equal(entries.length, 1);
    assert.equal(entries[0].type, 'special_render');
    assert.equal(entries[0].renderType, 'media-delivery');
}

{
    const entries = buildH5ConversationEntries([{
        id: 'media-tool-row',
        role: 'tool_call',
        content: '',
        toolName: 'send_media',
        toolCallId: 'media-delivery:call-1',
        toolStatus: 'done',
        toolResult: JSON.stringify({
            type: 'platform_media_delivery',
            status: 'sent',
            media_kind: 'audio',
            path: 'workspace/media/voice.mp3',
            filename: 'voice.mp3',
            message_id: 'tool-message-audio',
            allow_download: false,
        }),
    }]);

    assert.equal(entries.length, 1);
    assert.equal(entries[0].type, 'special_render');
    assert.equal(entries[0].renderType, 'media-delivery');
}

{
    const tool = toolCallMessageFromEvent({
        type: 'tool_call',
        name: 'search_contacts',
        call_id: 'call-search',
        args: { query: '朱志超' },
        status: 'done',
        result: 'found',
    }, () => 'generated-id', '2026-07-08T00:00:00.000Z');

    assert.equal(tool.role, 'tool_call');
    assert.equal(tool.toolName, 'search_contacts');
    assert.equal(tool.toolCallId, 'call-search');
    assert.equal(tool.content, '');
}

{
    const history = [
        { role: 'user', content: '移除联系人', created_at: '2026-07-10T01:20:00Z' },
        { role: 'tool_call', content: '', toolName: 'search_contacts', toolCallId: 'call-search', toolArgs: { query: '胡云' }, toolStatus: 'done', toolResult: 'found' },
        { role: 'tool_call', content: '', toolName: 'remove_contact', toolCallId: 'call-remove', toolArgs: { target_id: 'human-1' }, toolStatus: 'done', toolResult: 'removed' },
        { role: 'assistant', content: '已完成', created_at: '2026-07-10T01:20:05Z' },
    ].map((row, index) => mapHistoryMessage(row, () => `history-${index}`));

    const entries = buildH5ConversationEntries(history);
    const analysis = entries.find((entry) => entry.type === 'analysis_group');
    assert.ok(analysis);
    assert.equal(analysis.running, false);
    assert.equal(analysis.items.filter((item) => item.type === 'tool').length, 2);
    assert.equal(entries.at(-1).type, 'message');
    assert.equal(entries.at(-1).msg.content, '已完成');
}

{
    const deliveryResult = JSON.stringify({
        type: 'platform_file_delivery',
        path: 'workspace/reports/report.pdf',
        filename: 'report.pdf',
        message: '这是报告',
        mime_type: 'application/pdf',
        size: 123,
    });
    const entries = buildH5ConversationEntries([
        { id: 'u1', role: 'user', content: '发一下报告' },
        {
            id: 't1',
            role: 'tool_call',
            content: '',
            toolName: 'send_channel_file',
            toolCallId: 'tc-file',
            toolArgs: { file_path: 'workspace/reports/report.pdf' },
            toolStatus: 'done',
            toolResult: deliveryResult,
        },
        { id: 'a1', role: 'assistant', content: '已发送' },
    ]);

    assert.equal(JSON.stringify(entries.map((entry) => entry.type)), JSON.stringify(['message', 'special_render', 'message']));
    assert.equal(entries[1].renderType, 'file-delivery');
}

{
    let idSeq = 0;
    const makeSeqId = () => `stream-${++idSeq}`;
    let messages = [
        { id: 'u1', role: 'user', content: '你添加一下，然后发一条测试消息' },
    ];

    messages = applyAssistantStreamMessage(messages, {
        type: 'thinking',
        content: '我先查找联系人。',
        now: '2026-07-08T00:00:00.000Z',
    }, makeSeqId);
    messages = upsertToolCallMessage(messages, {
        id: 'tc1',
        role: 'tool_call',
        content: '',
        toolName: 'search_contacts',
        toolCallId: 'tc1',
        toolArgs: { query: '朱志超' },
        toolStatus: 'done',
        toolResult: 'ok',
    });
    messages = applyAssistantStreamMessage(messages, {
        type: 'done',
        content: '已完成！',
        now: '2026-07-08T00:00:02.000Z',
    }, makeSeqId);

    const entries = buildH5ConversationEntries(messages);
    assert.equal(JSON.stringify(entries.map((entry) => entry.type)), JSON.stringify(['message', 'analysis_group', 'message']));
    assert.equal(entries[2].msg.role, 'assistant');
    assert.equal(entries[2].msg.content, '已完成！');
}

{
    const entries = buildH5ConversationEntries(projectConversationTurnProgress([
        { id: 'u1', role: 'user', content: '连续运行多个工具' },
        { id: 'thinking-1', role: 'assistant', content: '', thinking: '第一轮思考', streaming: true, _streaming: true },
        { id: 'tool-1', role: 'tool_call', toolName: 'search_contacts', toolCallId: 'tool-1', toolStatus: 'done' },
        { id: 'thinking-2', role: 'assistant', content: '', thinking: '第二轮思考', streaming: true, _streaming: true },
        { id: 'tool-2', role: 'tool_call', toolName: 'read_file', toolCallId: 'tool-2', toolStatus: 'done' },
        { id: 'thinking-3', role: 'assistant', content: '', thinking: '当前思考', streaming: true, _streaming: true },
    ], true));

    assert.equal(
        entries.filter((entry) => entry.type === 'message' && entry.msg._streaming && !entry.msg.content).length,
        1,
        'one logical turn must render only one lifecycle progress row',
    );
    assert.equal(entries.at(-1).msg._conversationTurnProgress, true);
    const analysis = entries.find((entry) => entry.type === 'analysis_group');
    assert.equal(analysis.items.filter((item) => item.type === 'tool').length, 2);
}

{
    const messages = [
        { id: 'u1', role: 'user', content: '继续执行' },
        { id: 'thinking-1', role: 'assistant', content: '', thinking: '正在规划', streaming: true, _streaming: true },
        { id: 'tool-1', role: 'tool_call', toolName: 'knowledge_search', toolCallId: 'tool-1', toolStatus: 'running' },
    ];
    const entries = buildH5ConversationEntries(projectConversationTurnProgress(messages, false));
    const latestAnalysis = entries.findLast((entry) => entry.type === 'analysis_group');
    assert.ok(latestAnalysis);
    assert.equal(
        shouldProjectConversationTurnProgress(entries, true, {}),
        true,
        'an active turn keeps its trailing progress row after thinking or tool events',
    );
    assert.equal(
        shouldProjectConversationTurnProgress(entries, true, { [latestAnalysis.key]: true }),
        false,
        'expanding the latest reasoning/tool group hides the redundant progress row',
    );
    assert.equal(
        shouldProjectConversationTurnProgress(entries, false, {}),
        false,
        'a completed or suspended turn does not project progress',
    );
    const nextTurnEntries = buildH5ConversationEntries([
        ...messages,
        { id: 'u2', role: 'user', content: '开始下一轮' },
    ]);
    assert.equal(
        shouldProjectConversationTurnProgress(nextTurnEntries, true, { [latestAnalysis.key]: true }),
        true,
        'an expanded analysis group from an older turn cannot hide new-turn progress',
    );

    const answeringEntries = buildH5ConversationEntries(projectConversationTurnProgress([
        ...messages,
        { id: 'answer-1', role: 'assistant', content: '正式答复已经开始', streaming: true, _streaming: true },
    ], false));
    assert.equal(
        shouldProjectConversationTurnProgress(answeringEntries, true, {}),
        false,
        'the progress row disappears as soon as the active turn starts rendering its answer',
    );

    const answeringThenToolEntries = buildH5ConversationEntries(projectConversationTurnProgress([
        ...messages,
        { id: 'answer-1', role: 'assistant', content: '先给出阶段性正式答复' },
        { id: 'tool-2', role: 'tool_call', toolName: 'read_file', toolCallId: 'tool-2', toolStatus: 'running' },
    ], false));
    assert.equal(
        shouldProjectConversationTurnProgress(answeringThenToolEntries, true, {}),
        false,
        'later tool activity cannot restore progress after the answer has started',
    );

    const resumedAfterConfirmationEntries = buildH5ConversationEntries(projectConversationTurnProgress([
        ...messages,
        { id: 'suspension-intro', role: 'assistant', content: '请先确认后继续' },
        {
            id: 'confirmation-1', role: 'tool_call', toolName: 'request_confirmation',
            toolCallId: 'confirmation-1', toolStatus: 'done', toolResult: 'YES',
        },
    ], false));
    assert.equal(
        shouldProjectConversationTurnProgress(resumedAfterConfirmationEntries, true, {}),
        true,
        'a resolved suspension card restores progress for the resumed phase',
    );

    const resumedAnswerEntries = buildH5ConversationEntries(projectConversationTurnProgress([
        ...messages,
        { id: 'suspension-intro', role: 'assistant', content: '请先确认后继续' },
        {
            id: 'confirmation-1', role: 'tool_call', toolName: 'request_confirmation',
            toolCallId: 'confirmation-1', toolStatus: 'done', toolResult: 'YES',
        },
        { id: 'resumed-answer', role: 'assistant', content: '恢复后的正式答复' },
    ], false));
    assert.equal(
        shouldProjectConversationTurnProgress(resumedAnswerEntries, true, {}),
        false,
        'the restored progress row still disappears when the resumed answer starts',
    );
}

{
    const messageId = 'initial-assistant:session-1';
    let messages = applyAssistantStreamMessage([], {
        type: 'done',
        content: '欢迎使用报修服务',
        messageId,
        now: '2026-07-31T00:00:00.000Z',
    });
    messages = applyAssistantStreamMessage(messages, {
        type: 'done',
        content: '欢迎使用报修服务',
        messageId,
        now: '2026-07-31T00:00:01.000Z',
    });

    assert.equal(messages.length, 1);
    assert.equal(messages[0].id, messageId);
    assert.equal(messages[0].content, '欢迎使用报修服务');
}

{
    let messages = [
        { id: 'body', role: 'assistant', content: '这段正文必须保留', streaming: true, turnAnchorId: 'u1' },
        { id: 'terminal', role: 'tool_call', toolCallId: 'terminal', toolName: 'wait_for_external_input', toolStatus: 'running', turnAnchorId: 'u1' },
    ];
    messages = applyAssistantDoneMessage(messages, {
        type: 'done',
        content: '',
        turnAnchorId: 'u1',
        turnSuspended: true,
    });
    assert.equal(messages.length, 2);
    assert.equal(messages[0].content, '这段正文必须保留');
    assert.equal(messages[0].streaming, false);

    messages = applyAssistantDoneMessage(messages, { type: 'done', content: '确认后的最终答复' });
    messages = applyAssistantDoneMessage(messages, { type: 'done', content: '确认后的最终答复' });
    assert.equal(messages.filter((message) => message.content === '确认后的最终答复').length, 1);

    messages = applyAssistantDoneMessage(messages, {
        type: 'done',
        content: '确认后的最终答复',
        messageId: 'different-message',
    });
    assert.equal(messages.filter((message) => message.content === '确认后的最终答复').length, 2);
}

{
    let messages = [
        { id: 'u1', role: 'user', content: '查询并发送视频' },
        { id: 'a1', role: 'assistant', content: '知识库正文 A', streaming: true, _streaming: true },
        { id: 'lookup', role: 'tool_call', toolName: 'knowledge_search', toolCallId: 'lookup', toolStatus: 'done' },
        { id: 'a2', role: 'assistant', content: '补充正文 B', streaming: true, _streaming: true },
        { id: 'media-caption', role: 'assistant', content: '视频说明', streaming: false },
        { id: 'media', role: 'tool_call', toolName: 'send_media', toolCallId: 'media', toolStatus: 'done' },
    ];

    messages = applyAssistantDoneMessage(messages, {
        type: 'done',
        content: '知识库正文 A\n\n补充正文 B',
        now: '2026-08-18T00:00:00.000Z',
    });

    assert.equal(messages.filter((message) => message.role === 'assistant' && message.streaming).length, 0);
    assert.equal(messages.filter((message) => message.content === '知识库正文 A').length, 0);
    assert.equal(messages.filter((message) => message.content === '补充正文 B').length, 0);
    assert.equal(messages.filter((message) => message.content === '知识库正文 A\n\n补充正文 B').length, 1);
    assert.equal(messages.filter((message) => message.content === '视频说明').length, 1);
    assert.equal(messages.at(-1).content, '知识库正文 A\n\n补充正文 B');
}

{
    let messages = [
        { id: 'u1', role: 'user', content: '运行多个工具' },
        { id: 'tool-1', role: 'tool_call', toolName: 'run_subagent', toolCallId: 'tool-1', toolStatus: 'done' },
        { id: 'stale-stream', role: 'assistant', content: '中间轮次的临时正文', thinking: '中间思考', streaming: true, _streaming: true },
        { id: 'tool-2', role: 'tool_call', toolName: 'run_subagent', toolCallId: 'tool-2', toolStatus: 'done' },
        {
            id: 'committed-final',
            role: 'assistant',
            content: '最终已提交正文',
            thinking: '服务端已提交的完整思考',
            streaming: false,
            _streaming: false,
        },
    ];

    messages = applyAssistantDoneMessage(messages, {
        type: 'done',
        content: '最终已提交正文',
        messageId: 'committed-final',
        now: '2026-08-20T00:00:00.000Z',
    });

    assert.equal(messages.some((message) => message.id === 'stale-stream'), false);
    assert.equal(messages.filter((message) => message.id === 'committed-final').length, 1);
    assert.equal(messages.at(-1).content, '最终已提交正文');
    assert.equal(
        messages.at(-1).thinking,
        '服务端已提交的完整思考',
        'durable thinking must not be overwritten by a partial local stream',
    );
}

{
    const sameText = '已为你发送视频';
    let messages = [
        { id: 'u1', role: 'user', content: '发送视频' },
        { id: 'caption', role: 'assistant', content: sameText },
        { id: 'media', role: 'tool_call', toolName: 'send_media', toolCallId: 'media', toolStatus: 'done' },
    ];

    messages = applyAssistantDoneMessage(messages, { type: 'done', content: sameText });
    messages = applyAssistantDoneMessage(messages, { type: 'done', content: sameText });

    assert.equal(messages.filter((message) => message.content === sameText).length, 2);
    assert.equal(messages.filter((message) => message._canonicalDone).length, 1);
}

{
    let messages = [
        { id: 'u1', role: 'user', content: '需要确认' },
        { id: 'a1', role: 'assistant', content: '知识库正文 A', streaming: true, _streaming: true, turnAnchorId: 'u1' },
        { id: 'media', role: 'tool_call', toolName: 'send_media', toolCallId: 'media', toolStatus: 'done', turnAnchorId: 'u1' },
        { id: 'caption', role: 'assistant', content: '视频说明' },
        { id: 'a2', role: 'assistant', content: '补充正文 B', streaming: true, _streaming: true, turnAnchorId: 'u1' },
        { id: 'terminal', role: 'tool_call', toolName: 'wait_for_external_input', toolCallId: 'terminal', toolStatus: 'running', turnAnchorId: 'u1' },
    ];

    messages = applyAssistantDoneMessage(messages, {
        type: 'done',
        content: '',
        turnAnchorId: 'u1',
        turnSuspended: true,
    });

    assert.equal(messages.filter((message) => message.streaming || message._streaming).length, 0);
    assert.equal(messages.filter((message) => message.content === '知识库正文 A').length, 1);
    assert.equal(messages.filter((message) => message.content === '补充正文 B').length, 1);
    assert.equal(messages.filter((message) => message.content === '视频说明').length, 1);
    assert.equal(
        messages.map((message) => message.id).join(','),
        'u1,a1,media,caption,a2,terminal',
        'suspension finalizes every stream without collapsing across tool boundaries',
    );
    assert.equal(messages.at(-1).toolName, 'wait_for_external_input');
}

{
    const turn = {
        turn_anchor_id: 'anchored-confirmation-user',
        generation: 3,
        revision: 7,
        status: 'suspended',
        phase: 'suspended',
    };
    let messages = [
        { id: 'anchored-confirmation-user', role: 'user', content: '先查询知识库再让我确认' },
    ];
    messages = foldConversationTimelineEvent(messages, {
        type: 'tool_call',
        name: 'knowledge_search',
        call_id: 'knowledge-search',
        status: 'done',
        result: '知识库结果',
        turn,
    }).messages;
    messages = foldConversationTimelineEvent(messages, {
        type: 'chunk',
        content: '这是知识库回答，确认后继续。',
        message_id: 'confirmation-intro-stream',
        turn,
    }).messages;
    messages = foldConversationTimelineEvent(messages, {
        type: 'tool_call',
        name: 'wait_for_external_input',
        call_id: 'anchored-terminal-tool',
        args: { title: '确认继续', summary: '是否继续？' },
        status: 'running',
        turn,
    }).messages;

    assert.equal(
        messages.map((message) => message.id).join(','),
        [
            'anchored-confirmation-user',
            'tool:anchored-confirmation-user:default:knowledge-search',
            'confirmation-intro-stream',
            'tool:anchored-confirmation-user:default:anchored-terminal-tool',
        ].join(','),
        'a turn-ending tool initially renders after the streamed knowledge answer',
    );

    messages = foldConversationTimelineEvent(messages, {
        type: 'done',
        content: '',
        turn,
    }).messages;

    assert.equal(
        messages.map((message) => message.id).join(','),
        [
            'anchored-confirmation-user',
            'tool:anchored-confirmation-user:default:knowledge-search',
            'confirmation-intro-stream',
            'tool:anchored-confirmation-user:default:anchored-terminal-tool',
        ].join(','),
        'suspending the turn must not move the answer after its terminal tool',
    );
    assert.equal(messages[2].streaming, false);
    assert.equal(messages[2]._canonicalDone, undefined);
}

{
    const beforeEntries = buildH5ConversationEntries([
        { id: 'u1', role: 'user', content: 'hi' },
        { id: 'a1', role: 'assistant', content: '正在生成', streaming: true },
    ]);
    const afterEntries = buildH5ConversationEntries([
        { id: 'u1', role: 'user', content: 'hi' },
        { id: 'a1', role: 'assistant', content: '正在生成更长的回复', streaming: true },
    ]);

    assert.notEqual(getH5ScrollAnchor(beforeEntries, false), getH5ScrollAnchor(afterEntries, false));
    assert.notEqual(getH5ScrollAnchor(afterEntries, false), getH5ScrollAnchor(afterEntries, true));
}

{
    const pending = toolCallMessageFromEvent({
        type: 'tool_call',
        name: 'request_confirmation',
        call_id: 'confirm-1',
        args: {
            title: '确认发送',
            summary: '是否发送这条消息？',
            buttons: [{ text: '确认', value: 'confirm' }],
        },
        status: 'running',
    }, () => 'pending-local', '2026-07-08T00:00:00.000Z');
    const done = toolCallMessageFromEvent({
        type: 'tool_call',
        name: 'request_confirmation',
        call_id: 'confirm-1',
        args: pending.toolArgs,
        status: 'done',
        result: '已确认',
    }, () => 'done-local', '2026-07-08T00:00:01.000Z');

    assert.equal(isConfirmationToolCall(pending), true);
    assert.equal(hasPendingConfirmation([pending]), true);
    const persistedPending = mapHistoryMessage({
        role: 'tool_call',
        toolName: 'request_confirmation',
        toolCallId: 'confirm-history',
        toolArgs: {
            title: '历史确认',
            summary: '刷新后仍需确认',
            force_confirmation: true,
        },
        toolStatus: 'pending',
    }, () => 'history-pending');
    assert.equal(persistedPending.toolStatus, 'running');
    assert.equal(hasPendingConfirmation([persistedPending]), true);
    const optionalPending = {
        ...pending,
        toolCallId: 'confirm-optional',
        toolArgs: {
            ...pending.toolArgs,
            force_confirmation: false,
        },
    };
    assert.equal(hasPendingConfirmation([optionalPending]), false);
    const merged = upsertToolCallMessage([pending], done);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].toolStatus, 'done');
    assert.equal(hasPendingConfirmation(merged), false);

    const entries = buildH5ConversationEntries(merged);
    assert.equal(JSON.stringify(entries.map((entry) => entry.type)), JSON.stringify(['special_render']));
    assert.equal(entries[0].renderType, 'confirmation');
    assert.equal(entries[0].msg.role, 'tool_call');
    assert.equal(isConfirmationToolCall(entries[0].msg), true);
}

{
    const entries = buildH5ConversationEntries([
        { id: 'u1', role: 'user', content: '奶茶机扫码不出料怎么办？' },
        { id: 'lookup', role: 'tool_call', toolCallId: 'lookup', toolName: 'knowledge_search', toolStatus: 'done', toolResult: 'result' },
        { id: 'answer', role: 'assistant', content: '这是完整且必须展示的知识库排查正文。' },
        { id: 'confirm', role: 'tool_call', toolCallId: 'confirm', toolName: 'request_confirmation', toolStatus: 'done', toolResult: 'NO' },
        { id: 'feedback', role: 'tool_call', toolCallId: 'feedback', toolName: 'work_order_feedback', toolStatus: 'done', toolResult: 'ok' },
        { id: 'final', role: 'assistant', content: '已记录反馈。' },
    ]);

    assert.equal(JSON.stringify(entries.map((entry) => entry.type)), JSON.stringify([
        'message', 'analysis_group', 'message', 'special_render', 'analysis_group', 'message',
    ]));
    assert.equal(entries[2].msg.id, 'answer');
    assert.equal(entries[2].msg.content, '这是完整且必须展示的知识库排查正文。');
    assert.equal(entries[3].renderType, 'confirmation');
}

{
    const local = [
        { id: 'u1-local', role: 'user', content: '发测试消息' },
        { id: 'tc-local', role: 'tool_call', content: '', toolName: 'send_channel_message', toolCallId: 'tc3', toolArgs: { text: '测试消息' }, toolStatus: 'done', toolResult: 'ok' },
        { id: 'a-local', role: 'assistant', content: '已完成！' },
    ];
    const history = [
        { id: 'u1-history', role: 'user', content: '发测试消息' },
        { id: 'tc-history', role: 'tool_call', content: '', toolName: 'send_channel_message', toolCallId: 'tc3', toolArgs: { text: '测试消息' }, toolStatus: 'done', toolResult: 'ok' },
        { id: 'a-history', role: 'assistant', content: '已完成！' },
    ];

    const merged = mergeHistoryMessages(local, history);
    assert.equal(merged.length, 3);
    assert.equal(merged[1].id, 'tc-history');
    assert.equal(merged.filter((msg) => msg.role === 'tool_call').length, 1);
}

{
    const localDone = {
        id: 'live-tool', role: 'tool_call', content: '', toolName: 'toolscall',
        toolCallId: 'batch-1', _toolCallIdExplicit: true, turnAnchorId: 'turn-1',
        toolStatus: 'done', toolResult: 'created', toolArgs: { table_id: 18 },
    };
    const staleHistoryRunning = {
        ...localDone,
        id: 'history-tool',
        toolStatus: 'running',
        toolResult: '',
    };

    const merged = mergeHistoryMessages([localDone], [staleHistoryRunning]);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].id, 'history-tool', 'history keeps the durable row identity');
    assert.equal(merged[0].toolStatus, 'done', 'stale history cannot regress a live terminal tool');
    assert.equal(merged[0].toolResult, 'created');

    const reconciled = reconcileLatestHistoryWindow(
        [{ id: 'turn-1', role: 'user', content: 'run' }, localDone],
        [{ id: 'turn-1', role: 'user', content: 'run' }, staleHistoryRunning],
    );
    assert.equal(reconciled[1].toolStatus, 'done');
    assert.equal(reconciled[1].toolResult, 'created');

    const lateLiveRunning = upsertToolCallMessage([localDone], {
        ...staleHistoryRunning,
        id: 'late-live-tool',
        streaming: true,
        _streaming: true,
    });
    assert.equal(lateLiveRunning[0].toolStatus, 'done');
    assert.equal(lateLiveRunning[0].toolResult, 'created');
    assert.equal(lateLiveRunning[0].streaming, false);
    assert.equal(lateLiveRunning[0]._streaming, false);

    const rawDone = {
        ...localDone,
        id: 'raw-done-tool',
        content: JSON.stringify({ status: 'done', result: 'raw-created' }),
        toolStatus: undefined,
        toolResult: undefined,
    };
    const rawLateRunning = upsertToolCallMessage([rawDone], {
        ...staleHistoryRunning,
        id: 'raw-late-live-tool',
        streaming: true,
    });
    assert.equal(rawLateRunning[0].toolStatus, 'done');
    assert.equal(rawLateRunning[0].toolResult, 'raw-created');
    assert.equal(rawLateRunning[0].streaming, false);
}

{
    const liveResult = JSON.stringify({
        type: 'platform_file_delivery',
        path: 'workspace/reports/report.pdf',
        filename: 'report.pdf',
        message: '这是报告',
        mime_type: 'application/pdf',
        size: 123,
    });
    const local = [
        { id: 'u1-local', role: 'user', content: '发报告' },
        { id: 'call-live', role: 'tool_call', content: '', toolName: 'send_channel_file', toolCallId: 'model-call-id', _toolCallIdExplicit: false, toolArgs: { file_path: 'workspace/reports/report.pdf' }, toolStatus: 'done', toolResult: liveResult },
        { id: 'a-local', role: 'assistant', content: '已发送' },
    ];
    const history = [
        { id: 'u1-history', role: 'user', content: '发报告' },
        { id: 'row-history', role: 'tool_call', content: '', toolName: 'send_channel_file', toolCallId: 'db-row-id', _toolCallIdExplicit: false, toolArgs: { file_path: 'workspace/reports/report.pdf' }, toolStatus: 'done', toolResult: liveResult },
        { id: 'a-history', role: 'assistant', content: '已发送' },
    ];

    const merged = mergeHistoryMessages(local, history);
    assert.equal(merged.length, 3);
    assert.equal(merged[1].id, 'row-history');
    assert.equal(merged.filter((msg) => msg.role === 'tool_call').length, 1);
    const entries = buildH5ConversationEntries(merged);
    assert.equal(entries.filter((entry) => (
        entry.type === 'special_render' && entry.renderType === 'file-delivery'
    )).length, 1);
}

{
    const first = toolCallMessageFromEvent({
        type: 'tool_call', name: 'read_file', call_id: 'call-a',
        args: { path: 'same.txt' }, status: 'running',
        turn: { turn_anchor_id: 'turn-1', generation: 1 },
        producer_scope: 'producer-1',
    });
    const second = toolCallMessageFromEvent({
        type: 'tool_call', name: 'read_file', call_id: 'call-b',
        args: { path: 'same.txt' }, status: 'running',
        turn: { turn_anchor_id: 'turn-1', generation: 1 },
        producer_scope: 'producer-1',
    });
    const parallel = upsertToolCallMessage(
        upsertToolCallMessage([], first),
        second,
    );
    assert.equal(parallel.length, 2);
    assert.equal(mergeHistoryMessages([first], [second]).length, 2);

    const legacyResult = JSON.stringify({ path: 'same.txt', filename: 'same.txt' });
    const legacyLive = { ...first, id: 'legacy-live', toolName: 'send_channel_file', toolResult: legacyResult, toolCallId: 'fallback-live', _toolCallIdExplicit: false };
    const legacyHistory = { ...first, id: 'legacy-history', toolName: 'send_channel_file', toolResult: legacyResult, toolCallId: 'row-id', _toolCallIdExplicit: false };
    assert.equal(mergeHistoryMessages([legacyLive], [legacyHistory]).length, 1);
}

{
    const mapped = mapHistoryMessage({
        role: 'tool_call',
        content: '',
        toolCallId: 'row-id',
        toolName: 'request_confirmation',
        toolArgs: { title: '确认' },
        toolStatus: 'running',
    }, () => 'history-id');

    assert.equal(mapped.role, 'tool_call');
    assert.equal(mapped.id, 'row-id');
    assert.equal(mapped.toolCallId, 'row-id');
    assert.equal(isConfirmationToolCall(mapped), true);
}

console.log('h5 chat timeline tests passed');
