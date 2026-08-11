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

const mediaCardSource = readFileSync(
    resolve(__dirname, '../src/components/ChatMediaCard.tsx'),
    'utf8',
);
assert.match(mediaCardSource, /externalStarted\s*&&\s*mediaRef\.current/);
assert.match(mediaCardSource, /mediaRef\.current\.load\(\)/);
assert.match(mediaCardSource, /manual\)\s*void mediaRef\.current\.play\(\)/);

const sourcePath = resolve(__dirname, '../src/pages/h5/chatTimeline.ts');
const timelineRequire = (id) => {
    if (id === '../../utils/chatFileDelivery') return fileDeliveryModule.exports;
    if (id === '../../utils/clientId') return { createClientId: () => 'test-client-id' };
    if (id === '../../components/ChatToolCallRenderer') {
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

const {
    applyAssistantDoneMessage,
    applyAssistantStreamMessage,
    buildH5ConversationEntries,
    getH5ScrollAnchor,
    hasPendingConfirmation,
    isConfirmationToolCall,
    mapHistoryMessage,
    mergeHistoryMessages,
    normalizeChatTimelineMessages,
    toolCallMessageFromEvent,
    upsertToolCallMessage,
} = module.exports;

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
