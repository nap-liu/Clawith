import assert from 'node:assert/strict';

export function runH5ChatTimelinePart3(ctx) {
    const {
        reconcileLatestHistoryWindow,
        latestHistoryWindowOverlaps,
        mapHistoryMessage,
        parseMediaDeliveryErrorResult,
        parseFileDeliveryToolResult,
        normalizeChatTimelineMessages,
        buildH5ConversationEntries,
        toolCallMessageFromEvent,
        applyAssistantStreamMessage,
        upsertToolCallMessage,
        projectConversationTurnProgress,
        shouldProjectConversationTurnProgress,
    } = ctx;

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
}
