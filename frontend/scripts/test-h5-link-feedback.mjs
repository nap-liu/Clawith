import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const {
    copyH5LinkWithFeedback,
} = loadTypeScriptModule(
    resolve(__dirname, '../src/utils/h5LinkFeedback.ts'),
);

const targetUrl = 'https://docs.example.com/path?q=1';

for (const copyResult of [true, false]) {
    const copiedUrls = [];
    const notifications = [];
    const copied = await copyH5LinkWithFeedback(targetUrl, {
        copy: async (url) => {
            copiedUrls.push(url);
            return copyResult;
        },
        onCopied: () => notifications.push('已复制请到浏览器中打开'),
        onCopyFailed: () => notifications.push('复制失败，请稍后重试'),
    });

    assert.equal(copied, copyResult);
    assert.deepEqual(copiedUrls, [targetUrl]);
    assert.deepEqual(
        notifications,
        [copyResult ? '已复制请到浏览器中打开' : '复制失败，请稍后重试'],
    );
}

const notificationsAfterError = [];
assert.equal(await copyH5LinkWithFeedback(targetUrl, {
    copy: async () => {
        throw new Error('clipboard unavailable');
    },
    onCopied: () => notificationsAfterError.push('已复制请到浏览器中打开'),
    onCopyFailed: () => notificationsAfterError.push('复制失败，请稍后重试'),
}), false);
assert.deepEqual(notificationsAfterError, ['复制失败，请稍后重试']);

console.log('h5 link feedback tests passed');
