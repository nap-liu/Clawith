import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const { parseFileDeliveryToolResult, parseMediaDeliveryErrorResult } = loadTypeScriptModule(
    fileURLToPath(new URL('../src/utils/chatFileDelivery.ts', import.meta.url)),
);

{
    const delivery = parseFileDeliveryToolResult(
        'send_channel_file',
        JSON.stringify({
            type: 'platform_file_delivery',
            path: 'workspace/reports/report.pdf',
            filename: 'report.pdf',
            message: '这是报告',
            mime_type: 'application/pdf',
            size: 123,
        }),
        {},
        'tc-file',
    );

    assert.equal(delivery.id, 'tc-file');
    assert.equal(delivery.path, 'workspace/reports/report.pdf');
    assert.equal(delivery.filename, 'report.pdf');
    assert.equal(delivery.message, '这是报告');
    assert.equal(delivery.mimeType, 'application/pdf');
    assert.equal(delivery.size, 123);
}

{
    const delivery = parseFileDeliveryToolResult(
        'send_channel_file',
        {
            type: 'platform_file_delivery',
            path: 'workspace/out/demo.csv',
        },
        { file_path: 'workspace/out/fallback.csv' },
    );

    assert.equal(delivery.path, 'workspace/out/demo.csv');
    assert.equal(delivery.filename, 'demo.csv');
}

{
    const delivery = parseFileDeliveryToolResult(
        'send_channel_file',
        '请下载\n\nFile ready: [report.pdf](https://evil.example/api/agents/other/files/download?path=workspace%2Freports%2Freport.pdf&token=bad)',
        {},
        'tc-legacy',
    );

    assert.equal(delivery.id, 'tc-legacy');
    assert.equal(delivery.path, 'workspace/reports/report.pdf');
    assert.equal(delivery.filename, 'report.pdf');
    assert.equal(delivery.message, '请下载');
}

{
    const delivery = parseFileDeliveryToolResult(
        'send_media',
        JSON.stringify({
            type: 'platform_media_delivery',
            status: 'sent',
            media_kind: 'video',
            path: 'media/imported/opaque-preview.mp4',
            filename: 'opaque-preview.mp4',
            title: '  示例媒体\n\u0000展示标题  ',
        }),
        { title: '不应覆盖回执标题' },
        'tc-media-title',
    );

    assert.equal(delivery.title, '示例媒体 展示标题');
    assert.equal(delivery.filename, 'opaque-preview.mp4');
}

{
    const delivery = parseFileDeliveryToolResult(
        'send_media',
        JSON.stringify({
            type: 'platform_media_delivery',
            status: 'sent',
            media_kind: 'audio',
            path: 'exports/briefing.mp3',
            filename: 'briefing.mp3',
        }),
        { title: '示例音频标题' },
    );

    assert.equal(delivery.title, '示例音频标题');
}

assert.equal(parseFileDeliveryToolResult('send_channel_message', '{"type":"platform_file_delivery","path":"workspace/a.pdf"}'), null);
assert.equal(parseFileDeliveryToolResult('send_channel_file', '{"type":"platform_file_delivery","path":"/etc/passwd"}'), null);
assert.equal(parseFileDeliveryToolResult('send_channel_file', '{"type":"platform_file_delivery","path":"../secret.txt"}'), null);
assert.equal(parseFileDeliveryToolResult('send_channel_file', '{"type":"platform_file_delivery","path":"https://evil.example/a.pdf"}'), null);

for (const mediaKind of ['audio', 'video']) {
    const result = {
        type: 'media_generation',
        status: 'completed',
        delivery: {
            type: 'platform_media_delivery', status: 'sent', media_kind: mediaKind,
            path: `workspace/media/result.${mediaKind === 'audio' ? 'wav' : 'mp4'}`,
            message_id: 'original-receipt', allow_download: true,
        },
    };
    for (const payload of [result, JSON.stringify(result)]) {
        const delivery = parseFileDeliveryToolResult('generate_media', payload, {}, 'generation-call');
        assert.equal(delivery.mediaKind, mediaKind);
        assert.equal(delivery.messageId, 'original-receipt');
        assert.equal(delivery.path, result.delivery.path);
        assert.equal(delivery.allowDownload, true);
    }
    result.delivery.status = 'unknown';
    result.delivery.code = 'MEDIA_DELIVERY_STATE_UNKNOWN';
    assert.equal(parseFileDeliveryToolResult('generate_media', result), null);
    assert.equal(parseMediaDeliveryErrorResult('generate_media', result).status, 'unknown');
}

console.log('chat file delivery tests passed');
