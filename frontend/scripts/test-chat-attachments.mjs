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
    isPreviewableImageName,
    modelSupportsVision,
    resolveEffectiveChatModelId,
    splitAttachmentFileNames,
    stripChatImageDataMarkers,
} = module.exports;

{
    const payload = buildChatAttachmentPayload({
        input: '总结一下',
        supportsVision: false,
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
    assert.equal(payload.contentForLLM.includes('File location: workspace/uploads/report.pdf'), true);
    assert.equal(payload.contentForLLM.includes('Question: 总结一下'), true);
}

{
    const payload = buildChatAttachmentPayload({
        input: '',
        supportsVision: false,
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
    assert.equal(payload.contentForLLM.includes('[图片文件已上传: diagram.png'), true);
}

{
    const payload = buildChatAttachmentPayload({
        input: '看这张图',
        supportsVision: true,
        attachments: [{
            name: 'diagram.png',
            text: '',
            imageUrl: 'data:image/png;base64,abc',
        }],
    });

    assert.equal(payload.contentForLLM, '[image_data:data:image/png;base64,abc]\n\n看这张图');
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
        { name: 'one.png', text: '', imageUrl: 'data:image/png;base64,1' },
        { name: 'two.jpg', text: '', imageUrl: 'data:image/jpeg;base64,2' },
        { name: 'note.txt', text: 'hello' },
    ];
    const images = buildPreviewImagesFromAttachments(attachments);
    const payload = buildChatAttachmentPayload({
        input: '比较一下',
        supportsVision: true,
        attachments,
    });
    assert.equal(images.length, 2);
    assert.equal(images[0].filename, 'one.png');
    assert.equal(payload.previewImages.length, 2);
    assert.equal(payload.imageUrl, undefined);
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
    assert.equal(modelSupportsVision(models, 'vision-model'), true);
    assert.equal(modelSupportsVision(models, 'text-model'), false);
}

console.log('chat attachment tests passed');
