import assert from 'node:assert/strict';

export function runH5ChatTimelinePart4(ctx) {
    const {
        applyAssistantStreamMessage,
        applyAssistantDoneMessage,
        foldConversationTimelineEvent,
        toolCallMessageFromEvent,
        isConfirmationToolCall,
        hasPendingConfirmation,
        mapHistoryMessage,
        upsertToolCallMessage,
        buildH5ConversationEntries,
        getH5ScrollAnchor,
        mergeHistoryMessages,
        reconcileLatestHistoryWindow,
    } = ctx;

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
        content: '最终答复 C',
        now: '2026-08-18T00:00:00.000Z',
    });

    assert.equal(messages.filter((message) => message.role === 'assistant' && message.streaming).length, 0);
    assert.equal(messages.filter((message) => message.content === '知识库正文 A').length, 0);
    assert.equal(messages.filter((message) => message.content === '补充正文 B').length, 0);
    assert.equal(messages.filter((message) => message.content === '知识库正文 A\n\n补充正文 B').length, 0);
    assert.equal(messages.filter((message) => message.content === '最终答复 C').length, 1);
    assert.equal(messages.filter((message) => message.content === '视频说明').length, 1);
    assert.equal(messages.at(-1).content, '最终答复 C');
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

    const legacyResult = 'File ready: [same.txt](/api/agents/agent/files/download?path=same.txt)';
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
}
