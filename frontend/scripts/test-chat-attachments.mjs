import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const ts = require('typescript');

const __dirname = dirname(fileURLToPath(import.meta.url));
const sourcePath = resolve(__dirname, '../src/utils/chatAttachments.ts');
const source = readFileSync(sourcePath, 'utf8');
const compiled = ts.transpileModule(source, {
    compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2020,
        esModuleInterop: true,
    },
}).outputText;

const module = { exports: {} };
vm.runInNewContext(compiled, {
    module,
    exports: module.exports,
    require,
    console,
}, { filename: sourcePath });

const {
    buildPreviewImagesFromAttachments,
    buildChatAttachmentPayload,
    collectMarkdownImages,
    extractChatImageDataMarkers,
    getChatAttachmentIconKind,
    getChatQuotedMessageTypeLabel,
    isPreviewableImageName,
    normalizeChatAttachmentFields,
    normalizeChatQuotedMessage,
    partitionChatQuotedContent,
    resolveEffectiveChatModelId,
    splitAttachmentFileNames,
    stripChatImageDataMarkers,
} = module.exports;

{
    const quotedMessage = normalizeChatQuotedMessage({
        message_type: 'RICH_TEXT',
        content_status: 'partial',
        text: '引用正文',
        attachments: [
            { display_name: '引用图片.png', path: 'workspace/uploads/quote.png', kind: 'image' },
        ],
    });
    const partitioned = partitionChatQuotedContent(
        quotedMessage,
        [
            ...quotedMessage.attachments,
            { display_name: '当前文件.pdf', path: 'workspace/uploads/current.pdf', kind: 'file' },
        ],
        [
            { src: '/quote', path: 'workspace/uploads/quote.png' },
            { src: '/current', path: 'workspace/uploads/current.png' },
        ],
    );

    assert.equal(quotedMessage.message_type, 'rich_text');
    assert.equal(getChatQuotedMessageTypeLabel(quotedMessage.message_type), '富文本');
    assert.equal(partitioned.quotedAttachments.length, 1);
    assert.equal(partitioned.attachments[0].display_name, '当前文件.pdf');
    assert.equal(partitioned.quotedPreviewImages[0].src, '/quote');
    assert.equal(partitioned.previewImages[0].src, '/current');
}

{
    const providerRef = 'legacy-provider-sender-id';
    const unknownSender = normalizeChatQuotedMessage({
        message_type: 'text',
        content_status: 'available',
        text: '引用正文',
        attachments: [],
        sender_ref: providerRef,
        sender_name: 'untrusted provider nickname',
    });
    assert.equal(unknownSender.sender_status, 'unknown');
    assert.equal(Object.hasOwn(unknownSender, 'sender_ref'), false);
    assert.equal(Object.hasOwn(unknownSender, 'sender_name'), false);

    const senderUserId = '9275f720-016d-4ea1-a1bf-366cbd62f57b';
    const resolvedSender = normalizeChatQuotedMessage({
        message_type: 'text',
        content_status: 'available',
        text: '引用正文',
        attachments: [],
        sender_status: 'resolved',
        sender_user_id: senderUserId,
        sender_name: 'Canonical Sender',
    });
    assert.equal(resolvedSender.sender_status, 'resolved');
    assert.equal(resolvedSender.sender_user_id, senderUserId);
    assert.equal(resolvedSender.sender_name, 'Canonical Sender');
}

{
    const cases = [
        ['contract.PDF', undefined, undefined, 'pdf'],
        ['proposal.docx', undefined, undefined, 'word'],
        ['orders.xlsx', undefined, undefined, 'spreadsheet'],
        ['roadmap.pptx', undefined, undefined, 'presentation'],
        ['source.tar.gz', undefined, undefined, 'archive'],
        ['README.md', undefined, undefined, 'text'],
        ['settings.json', undefined, undefined, 'code'],
        ['recording.bin', 'audio/mpeg', undefined, 'audio'],
        ['clip.bin', undefined, 'video', 'video'],
        ['payload.bin', undefined, undefined, 'generic'],
    ];
    cases.forEach(([name, mimeType, kind, expected]) => {
        assert.equal(getChatAttachmentIconKind({ name, mimeType, kind }), expected);
    });
}

{
    const payload = buildChatAttachmentPayload({
        input: '总结一下',
        attachments: [{
            name: 'report.pdf',
            text: 'PDF extracted text',
            path: 'workspace/uploads/report.pdf',
        }],
    });

    assert.equal(payload.userMsg, '[Attachment: report.pdf]\n总结一下');
    assert.equal(payload.fileName, 'report.pdf');
    assert.equal(payload.imageUrl, undefined);
    assert.equal(payload.contentForLLM.includes('[File: report.pdf]'), true);
    assert.equal(payload.contentForLLM.includes('workspace/uploads/report.pdf'), false);
    assert.equal(payload.contentForLLM.includes('relative path: "uploads/report.pdf"'), false);
    assert.equal(payload.contentForLLM.endsWith('总结一下'), true);
}

{
    const payload = buildChatAttachmentPayload({
        input: '',
        attachments: [{
            name: 'diagram.png',
            text: '[图片文件: diagram.png，需要视觉模型分析]',
            path: 'workspace/uploads/diagram.png',
            imageUrl: 'data:image/png;base64,abc',
        }],
    });

    assert.equal(payload.userMsg, '[Attachment: diagram.png]');
    assert.equal(payload.fileName, 'diagram.png');
    assert.equal(payload.imageUrl, 'data:image/png;base64,abc');
    assert.equal(payload.contentForLLM, '请分析这些文件');
    assert.equal(payload.contentForLLM.includes('base64'), false);
}

{
    const payload = buildChatAttachmentPayload({
        input: '看这张图',
        attachments: [{
            name: 'diagram.png',
            text: '',
            imageUrl: 'data:image/png;base64,abc',
        }],
    });

    assert.equal(payload.contentForLLM, '看这张图');
}

{
    assert.equal(isPreviewableImageName('a.JPG'), true);
    assert.equal(isPreviewableImageName('a.pdf'), false);
    assert.equal(
        JSON.stringify(splitAttachmentFileNames('a.png, b.jpg, report.pdf')),
        JSON.stringify(['a.png', 'b.jpg', 'report.pdf']),
    );
    const images = collectMarkdownImages('![a](https://x/a.png) text ![b](/api/agents/1/files/download?path=x)');
    assert.equal(images.length, 2);
    assert.equal(images[1].alt, 'b');
}

{
    const attachments = [
        { name: 'one.png', text: '', path: 'workspace/uploads/one.png', imageUrl: 'data:image/png;base64,1' },
        { name: 'two.jpg', text: '', path: 'workspace/uploads/two.jpg', imageUrl: 'data:image/jpeg;base64,2' },
        { name: 'note.txt', text: 'hello', path: 'workspace/uploads/note.txt' },
    ];
    const images = buildPreviewImagesFromAttachments(attachments);
    const payload = buildChatAttachmentPayload({
        input: '比较一下',
        attachments,
    });
    assert.equal(images.length, 2);
    assert.equal(images[0].filename, 'one.png');
    assert.equal(payload.previewImages.length, 2);
    assert.equal(payload.imageUrl, undefined);
    assert.equal(payload.attachments.length, 3);
    assert.equal(payload.attachments[0].display_name, 'one.png');
}

{
    const normalized = normalizeChatAttachmentFields({
        raw: {
            content: '[file:one.jpg]\n[file:two.jpg]\n[file:two.jpg]\n比较图片',
        },
        sourceChannel: 'dingtalk',
        buildDownloadUrl: (path) => `/download?path=${encodeURIComponent(path)}`,
    });

    assert.equal(normalized.displayContent, '比较图片');
    assert.equal(normalized.attachments.length, 3);
    assert.equal(normalized.previewImages.length, 3);
    assert.equal(normalized.attachments[1].display_name, 'two.jpg');
    assert.equal(normalized.attachments[2].display_name, 'two.jpg');
}

{
    const normalized = normalizeChatAttachmentFields({
        raw: {
            content: '[file:fake.jpg]\n用户正文',
            display_content: '[file:fake.jpg]\n用户正文',
            attachments: [],
        },
        sourceChannel: 'dingtalk',
        buildDownloadUrl: (path) => path,
    });

    assert.equal(normalized.displayContent, '[file:fake.jpg]\n用户正文');
    assert.equal(normalized.attachments.length, 0);
}

{
    const normalized = normalizeChatAttachmentFields({
        raw: {
            content: '[file:ignored.jpg]\nraw',
            display_content: '结构化正文',
            attachments: [
                { display_name: '同名.jpg', path: 'workspace/uploads/a.jpg', kind: 'image' },
                { display_name: '同名.jpg', path: 'workspace/uploads/b.jpg', kind: 'image' },
                { display_name: '报告,最终版.pdf', path: 'workspace/uploads/report.pdf', kind: 'file' },
            ],
        },
        sourceChannel: 'dingtalk',
        buildDownloadUrl: (path) => path,
    });

    assert.equal(normalized.displayContent, '结构化正文');
    assert.equal(normalized.attachments.length, 3);
    assert.equal(normalized.previewImages.length, 2);
    assert.equal(normalized.attachments[2].display_name, '报告,最终版.pdf');
}

{
    const raw = '[image_data:data:image/png;base64,abc123+/=]\nQuestion: 这张图是什么？\n[image_data:data:image/jpeg;base64,def456==]';
    const images = extractChatImageDataMarkers(raw);
    assert.equal(images.length, 2);
    assert.equal(images[0].src, 'data:image/png;base64,abc123+/=');
    assert.equal(stripChatImageDataMarkers(raw), 'Question: 这张图是什么？');
}

{
    const models = [
        { id: 'disabled-vision', enabled: false, supports_vision: true },
        { id: 'text-model', enabled: true, supports_vision: false },
        { id: 'vision-model', enabled: true, supports_vision: true },
    ];

    assert.equal(resolveEffectiveChatModelId({
        preferredModelId: '',
        tenantDefaultModelId: 'vision-model',
        models,
    }), 'vision-model');
    assert.equal(resolveEffectiveChatModelId({
        preferredModelId: '',
        tenantDefaultModelId: '',
        models,
    }), 'text-model');
}

console.log('chat attachment tests passed');
