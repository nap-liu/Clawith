import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const {
    buildDingTalkMiniProgramWebviewRoute,
    isDingTalkMiniProgramWebViewCandidate,
    isDingTalkMiniProgramWebViewRuntime,
    navigateDingTalkMiniProgramPage,
    openDingTalkMiniProgramWebview,
} = loadTypeScriptModule(
    resolve(__dirname, '../src/utils/dingtalkLink.ts'),
    { clearTimeout, setTimeout },
);
const {
    openExternalLinkWithBrowserDefault,
} = loadTypeScriptModule(resolve(__dirname, '../src/utils/browserLink.ts'));

assert.equal(
    buildDingTalkMiniProgramWebviewRoute('https://docs.example.com/a?name=中文#part'),
    '/subPackages/webview/index?url=https%3A%2F%2Fdocs.example.com%2Fa%3Fname%3D%E4%B8%AD%E6%96%87%23part',
);
assert.equal(isDingTalkMiniProgramWebViewCandidate('Mozilla/5.0 DingTalk/8.0'), false);
assert.equal(
    isDingTalkMiniProgramWebViewCandidate('Mozilla/5.0 DingTalk/8.0 dd-web'),
    true,
);
assert.equal(isDingTalkMiniProgramWebViewCandidate('Mozilla/5.0 AliApp(AP/10.7.66.8000)'), false);

assert.equal(await isDingTalkMiniProgramWebViewRuntime({
    targetWindow: {},
    userAgent: 'Mozilla/5.0 DingTalk/8.0',
}), false);
assert.equal(await isDingTalkMiniProgramWebViewRuntime({
    targetWindow: {},
    userAgent: 'Mozilla/5.0 DingTalk/8.0 dd-web',
}), false);

const loadedWindow = {
    dd: {
        navigateTo() {},
    },
};
assert.equal(await isDingTalkMiniProgramWebViewRuntime({
    targetWindow: loadedWindow,
    userAgent: 'Mozilla/5.0 DingTalk/8.0 dd-web',
}), true);

const navigateCalls = [];
await openDingTalkMiniProgramWebview('https://docs.example.com/a?name=中文#part', {
    currentHref: 'https://docs.example.com/h5/agents/a1/chat',
    targetWindow: {
        dd: {
            navigateTo(options) {
                navigateCalls.push(options.url);
                options.success?.();
            },
        },
    },
    userAgent: 'Mozilla/5.0 DingTalk/8.0 dd-web',
    duplicateWindowMs: 0,
    navigateTimeoutMs: 50,
});
assert.deepEqual(navigateCalls, [
    '/subPackages/webview/index?url=https%3A%2F%2Fdocs.example.com%2Fa%3Fname%3D%25E4%25B8%25AD%25E6%2596%2587%23part',
]);

const directPageCalls = [];
await navigateDingTalkMiniProgramPage('/pages/order/detail?id=123', {
    targetWindow: {
        dd: {
            navigateTo(options) {
                directPageCalls.push(options.url);
                options.success?.();
            },
        },
    },
    userAgent: 'Mozilla/5.0 DingTalk/8.0 dd-web',
    duplicateWindowMs: 0,
    navigateTimeoutMs: 50,
});
assert.deepEqual(directPageCalls, ['/pages/order/detail?id=123']);
await assert.rejects(
    navigateDingTalkMiniProgramPage('//evil.example/page', {
        targetWindow: loadedWindow,
        userAgent: 'Mozilla/5.0 DingTalk/8.0 dd-web',
        duplicateWindowMs: 0,
    }),
    /internal page route/,
);

await assert.rejects(
    openDingTalkMiniProgramWebview('https://direct.example.com/path', {
        currentHref: 'https://direct.example.com/h5/agents/a1/chat',
        targetWindow: {
            dd: {
                navigateTo() {},
            },
        },
        userAgent: 'Mozilla/5.0 DingTalk/8.0',
        duplicateWindowMs: 0,
    }),
    /navigation SDK is unavailable/,
);
await assert.rejects(
    openDingTalkMiniProgramWebview('javascript:alert(1)', {
        targetWindow: loadedWindow,
        userAgent: 'Mozilla/5.0 DingTalk/8.0 dd-web',
        duplicateWindowMs: 0,
    }),
    /only accepts HTTP\(S\)/,
);
await assert.rejects(
    openDingTalkMiniProgramWebview('https://failure.example.com/path', {
        currentHref: 'https://failure.example.com/h5/agents/a1/chat',
        targetWindow: {
            dd: {
                navigateTo(options) {
                    options.fail?.(new Error('route failure'));
                },
            },
        },
        userAgent: 'Mozilla/5.0 DingTalk/8.0 dd-web',
        duplicateWindowMs: 0,
        navigateTimeoutMs: 50,
    }),
    /route failure/,
);
await assert.rejects(
    openDingTalkMiniProgramWebview('https://timeout.example.com/path', {
        currentHref: 'https://timeout.example.com/h5/agents/a1/chat',
        targetWindow: {
            dd: {
                navigateTo() {},
            },
        },
        userAgent: 'Mozilla/5.0 DingTalk/8.0 dd-web',
        duplicateWindowMs: 0,
        navigateTimeoutMs: 0,
    }),
    /Timed out navigating/,
);

let crossOriginNavigateCalls = 0;
await assert.rejects(
    openDingTalkMiniProgramWebview('https://external.example.com/path', {
        currentHref: 'https://ai.example.com/h5/agents/a1/chat',
        targetWindow: {
            dd: {
                navigateTo() {
                    crossOriginNavigateCalls += 1;
                },
            },
        },
        userAgent: 'Mozilla/5.0 DingTalk/8.0 dd-web',
        duplicateWindowMs: 0,
    }),
    /only allows same-origin URLs/,
);
assert.equal(crossOriginNavigateCalls, 0);
await assert.rejects(
    openDingTalkMiniProgramWebview('https://ai.example.com/path', {
        currentHref: 'not-a-valid-h5-url',
        targetWindow: loadedWindow,
        userAgent: 'Mozilla/5.0 DingTalk/8.0 dd-web',
        duplicateWindowMs: 0,
    }),
    /Current H5 origin is unavailable/,
);

let duplicateCalls = 0;
const duplicateOptions = {
    currentHref: 'https://duplicate.example.com/h5/agents/a1/chat',
    targetWindow: {
        dd: {
            navigateTo(options) {
                duplicateCalls += 1;
                options.success?.();
            },
        },
    },
    userAgent: 'Mozilla/5.0 DingTalk/8.0 dd-web',
    duplicateWindowMs: 500,
    navigateTimeoutMs: 50,
    now: () => 10_000,
};
await openDingTalkMiniProgramWebview('https://duplicate.example.com/path', duplicateOptions);
await openDingTalkMiniProgramWebview('https://duplicate.example.com/path', duplicateOptions);
assert.equal(duplicateCalls, 1);

const popup = { opener: {} };
let sameWindowUrl = '';
assert.equal(openExternalLinkWithBrowserDefault('https://popup.example.com/path', {
    open() {
        return popup;
    },
    location: {
        assign(url) {
            sameWindowUrl = String(url);
        },
    },
}), true);
assert.equal(popup.opener, null);
assert.equal(sameWindowUrl, '');

assert.equal(openExternalLinkWithBrowserDefault('https://same-window.example.com/path', {
    open() {
        return null;
    },
    location: {
        assign(url) {
            sameWindowUrl = String(url);
        },
    },
}), true);
assert.equal(sameWindowUrl, 'https://same-window.example.com/path');

console.log('dingtalk link runtime tests passed');
