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

// Compile the canonical shared conversation core directly. The H5 module is a
// compatibility facade over this file, so these fixtures lock the behaviour H5
// consumes while allowing Web to share the same transformations.
const sourcePath = resolve(__dirname, '../src/features/conversation/core/chatTimeline.ts');
const timelineRequire = (id) => {
    if (id === '../../../utils/chatFileDelivery') return fileDeliveryModule.exports;
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
const resumeRecoveryModule = compileTsModule(resolve(
    __dirname,
    '../src/features/conversation/core/resumeRecovery.ts',
));

const {
    applyAssistantDoneMessage,
    applyAssistantStreamMessage,
    buildConversationEntries: buildH5ConversationEntries,
    getConversationScrollAnchor: getH5ScrollAnchor,
    hasPendingConfirmation,
    isConfirmationToolCall,
    latestHistoryWindowOverlaps,
    mapHistoryMessage,
    mergeHistoryMessages,
    normalizeChatTimelineMessages,
    reconcileLatestHistoryWindow,
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
        { id: 'body', role: 'assistant', content: '这段正文必须保留', streaming: true },
        { id: 'card', role: 'tool_call', toolCallId: 'card', toolName: 'request_confirmation', toolStatus: 'running' },
    ];
    messages = applyAssistantDoneMessage(messages, { type: 'done', content: '' });
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
        { id: 'a1', role: 'assistant', content: '知识库正文 A', streaming: true, _streaming: true },
        { id: 'media', role: 'tool_call', toolName: 'send_media', toolCallId: 'media', toolStatus: 'done' },
        { id: 'caption', role: 'assistant', content: '视频说明' },
        { id: 'a2', role: 'assistant', content: '补充正文 B', streaming: true, _streaming: true },
        { id: 'confirm', role: 'tool_call', toolName: 'request_confirmation', toolCallId: 'confirm', toolStatus: 'running' },
    ];

    messages = applyAssistantDoneMessage(messages, { type: 'done', content: '' });

    assert.equal(messages.filter((message) => message.streaming || message._streaming).length, 0);
    assert.equal(messages.filter((message) => message.content === '知识库正文 A\n\n补充正文 B').length, 1);
    assert.equal(messages.filter((message) => message.content === '视频说明').length, 1);
    assert.equal(messages.at(-2).content, '知识库正文 A\n\n补充正文 B');
    assert.equal(messages.at(-1).toolName, 'request_confirmation');
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
        { id: 'call-live', role: 'tool_call', content: '', toolName: 'send_channel_file', toolCallId: 'model-call-id', toolArgs: { file_path: 'workspace/reports/report.pdf' }, toolStatus: 'done', toolResult: liveResult },
        { id: 'a-local', role: 'assistant', content: '已发送' },
    ];
    const history = [
        { id: 'u1-history', role: 'user', content: '发报告' },
        { id: 'row-history', role: 'tool_call', content: '', toolName: 'send_channel_file', toolCallId: 'db-row-id', toolArgs: { file_path: 'workspace/reports/report.pdf' }, toolStatus: 'done', toolResult: liveResult },
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
