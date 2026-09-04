import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

globalThis.localStorage = {
    getItem: (key) => key === 'token' ? 'legacy-query-token' : null,
};

const server = await createServer({
    appType: 'custom',
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
});

try {
    const [{ MarkdownRenderer }, { ToastProvider }] = await Promise.all([
        server.ssrLoadModule('/src/components/MarkdownRenderer.tsx'),
        server.ssrLoadModule('/src/components/Toast/ToastProvider.tsx'),
    ]);
    const renderMarkdown = (content, agentId) => renderToStaticMarkup(
        React.createElement(
            ToastProvider,
            null,
            React.createElement(MarkdownRenderer, { content, agentId }),
        ),
    );

    const chatImage = renderMarkdown(
        '![chart](workspace/reports/chart 1.png)',
        '00000000-0000-0000-0000-000000000001',
    );
    assert.match(
        chatImage,
        /src="\/api\/agents\/00000000-0000-0000-0000-000000000001\/files\/download\?path=workspace%2Freports%2Fchart%201\.png&amp;inline=1"/,
    );
    assert.doesNotMatch(chatImage, /legacy-query-token|[?&]token=/);

    for (const [prefix, expectedPath] of [
        ['/workspace/reports/chart.png', 'workspace%2Freports%2Fchart.png'],
        ['./workspace/reports/chart.png', 'workspace%2Freports%2Fchart.png'],
        ['chart.png', 'chart.png'],
    ]) {
        const normalizedImage = renderMarkdown(
            `![normalized](${prefix})`,
            '00000000-0000-0000-0000-000000000001',
        );
        assert.match(
            normalizedImage,
            new RegExp(`src="/api/agents/00000000-0000-0000-0000-000000000001/files/download\\?path=${expectedPath}&amp;inline=1"`),
        );
    }

    const existingAgentImage = renderMarkdown(
        '![existing](/api/agents/existing/files/download?path=workspace/chart.png)',
        '00000000-0000-0000-0000-000000000001',
    );
    assert.match(
        existingAgentImage,
        /src="\/api\/agents\/existing\/files\/download\?path=workspace\/chart\.png"/,
    );
    assert.doesNotMatch(existingAgentImage, /legacy-query-token|[?&]token=/);

    const externalImage = renderMarkdown(
        '![external](https://example.com/chart.png)',
        '00000000-0000-0000-0000-000000000001',
    );
    assert.match(externalImage, /src="https:\/\/example\.com\/chart\.png"/);
    assert.doesNotMatch(externalImage, /\/api\/agents\//);

    const nonChatMarkdown = renderMarkdown('![chart](workspace/reports/chart.png)');
    assert.doesNotMatch(nonChatMarkdown, /<img|\/api\/agents\//);
    assert.match(nonChatMarkdown, />!chart<\/p>/);

    const unsafeImage = renderMarkdown(
        '![unsafe](workspace/../private.png)',
        '00000000-0000-0000-0000-000000000001',
    );
    assert.doesNotMatch(unsafeImage, /<img|\/api\/agents\//);

    const slashPrefixedImage = renderMarkdown(
        '![asset](/assets/chart.png)',
        '00000000-0000-0000-0000-000000000001',
    );
    assert.match(slashPrefixedImage, /path=assets%2Fchart\.png&amp;inline=1/);

    console.log('chat markdown relative image tests passed');
} finally {
    await server.close();
}
