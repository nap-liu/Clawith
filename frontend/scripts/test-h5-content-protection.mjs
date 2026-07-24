import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

globalThis.localStorage = {
    getItem: () => null,
};

const server = await createServer({
    appType: 'custom',
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
});

try {
    const [{ MarkdownRenderer }, { ToastProvider }, { default: ChatFileDeliveryCard }] = await Promise.all([
        server.ssrLoadModule('/src/components/MarkdownRenderer.tsx'),
        server.ssrLoadModule('/src/components/Toast/ToastProvider.tsx'),
        server.ssrLoadModule('/src/components/ChatFileDeliveryCard.tsx'),
    ]);

    const renderMarkdown = (props) => renderToStaticMarkup(
        React.createElement(
            ToastProvider,
            null,
            React.createElement(MarkdownRenderer, {
                content: '![protected image](https://example.com/protected.png)',
                ...props,
            }),
        ),
    );

    const protectedMarkdown = renderMarkdown({
        imagePreviewMode: 'mobile',
        allowImageDownload: false,
        protectImages: true,
    });
    assert.match(protectedMarkdown, /<img[^>]*draggable="false"/);
    assert.doesNotMatch(protectedMarkdown, /data-markdown-image-download=/);

    const desktopMarkdown = renderMarkdown({
        imagePreviewMode: 'desktop',
    });
    assert.match(desktopMarkdown, /data-markdown-image-download=/);

    const imageDelivery = {
        id: 'image-delivery',
        path: 'workspace/shared/protected.png',
        filename: 'protected.png',
        mimeType: 'image/png',
    };
    const h5FileCard = renderToStaticMarkup(React.createElement(ChatFileDeliveryCard, {
        agentId: '00000000-0000-0000-0000-000000000001',
        delivery: imageDelivery,
        mode: 'h5',
        onPreviewImages: () => {},
    }));
    assert.match(h5FileCard, /<img[^>]*draggable="false"/);
    assert.doesNotMatch(h5FileCard, /aria-label="下载文件"/);

    const desktopFileCard = renderToStaticMarkup(React.createElement(ChatFileDeliveryCard, {
        agentId: '00000000-0000-0000-0000-000000000001',
        delivery: imageDelivery,
        mode: 'pc',
        onPreviewImages: () => {},
    }));
    assert.match(desktopFileCard, /aria-label="下载文件"/);

    console.log('h5 content protection tests passed');
} finally {
    await server.close();
}
