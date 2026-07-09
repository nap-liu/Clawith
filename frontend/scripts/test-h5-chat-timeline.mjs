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

const sourcePath = resolve(__dirname, '../src/pages/h5/chatTimeline.ts');
const timelineRequire = (id) => {
    if (id === '../../utils/chatFileDelivery') return fileDeliveryModule.exports;
    return require(id);
};

const module = compileTsModule(sourcePath, timelineRequire);

const {
    applyAssistantStreamMessage,
    buildH5ConversationEntries,
    getH5ScrollAnchor,
    isConfirmationToolCall,
    mapHistoryMessage,
    mergeHistoryMessages,
    toolCallMessageFromEvent,
    upsertToolCallMessage,
} = module.exports;

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
    const messages = [
        { id: 'u1', role: 'user', content: '你添加一下，然后发一条测试消息' },
        { id: 'a1', role: 'assistant', content: '我先查找联系人。' },
        { id: 't1', role: 'tool_call', content: '', toolName: 'search_contacts', toolCallId: 'tc1', toolArgs: { query: '朱志超' }, toolStatus: 'done', toolResult: 'ok' },
        { id: 't2', role: 'tool_call', content: '', toolName: 'add_contact', toolCallId: 'tc2', toolArgs: { user_id: 'u' }, toolStatus: 'done', toolResult: 'ok' },
        { id: 't3', role: 'tool_call', content: '', toolName: 'send_channel_message', toolCallId: 'tc3', toolArgs: { text: '测试消息' }, toolStatus: 'done', toolResult: 'ok' },
        { id: 'a2', role: 'assistant', content: '已完成！' },
    ];

    const entries = buildH5ConversationEntries(messages);
    assert.equal(JSON.stringify(entries.map((entry) => entry.type)), JSON.stringify(['message', 'analysis_group', 'message']));
    assert.equal(entries[1].items.filter((item) => item.type === 'tool').length, 3);
    assert.equal(entries[1].items[0].type, 'thinking');
    assert.equal(entries[2].msg.role, 'assistant');
    assert.equal(entries[2].msg.content, '已完成！');
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

    assert.equal(JSON.stringify(entries.map((entry) => entry.type)), JSON.stringify(['message', 'file_delivery', 'message']));
    assert.equal(entries[1].delivery.path, 'workspace/reports/report.pdf');
    assert.equal(entries[1].delivery.filename, 'report.pdf');
    assert.equal(entries[1].delivery.message, '这是报告');
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
    const merged = upsertToolCallMessage([pending], done);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].toolStatus, 'done');

    const entries = buildH5ConversationEntries(merged);
    assert.equal(JSON.stringify(entries.map((entry) => entry.type)), JSON.stringify(['message']));
    assert.equal(entries[0].msg.role, 'tool_call');
    assert.equal(isConfirmationToolCall(entries[0].msg), true);
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
    assert.equal(entries.filter((entry) => entry.type === 'file_delivery').length, 1);
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
