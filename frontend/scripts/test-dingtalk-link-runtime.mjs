import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const {
    hasDingTalkMiniProgramOpenLink,
    openDingTalkExternalLink,
} = loadTypeScriptModule(resolve(__dirname, '../src/utils/dingtalkLink.ts'));
const {
    openExternalLinkWithBrowserDefault,
} = loadTypeScriptModule(resolve(__dirname, '../src/utils/browserLink.ts'));

assert.equal(hasDingTalkMiniProgramOpenLink({}), false);
assert.equal(hasDingTalkMiniProgramOpenLink({ dd: { openLink() {} } }), false);
assert.equal(hasDingTalkMiniProgramOpenLink({
    dd: { env: { appType: 'WEB' }, openLink() {} },
}), false);
assert.equal(hasDingTalkMiniProgramOpenLink({
    dd: { env: { appType: 'WEBVIEW_IN_MINIAPP' }, openLink() {} },
}), true);
assert.equal(hasDingTalkMiniProgramOpenLink({
    dd: { biz: { util: { openLink() {} } } },
}), false);

let directClientCalls = 0;
await assert.rejects(
    openDingTalkExternalLink('https://direct.example.com/path', {
        targetWindow: {
            dd: {
                env: { appType: 'WEB' },
                openLink() {
                    directClientCalls += 1;
                    return Promise.resolve();
                },
            },
        },
        duplicateWindowMs: 0,
    }),
    /SDK is unavailable/,
);
assert.equal(directClientCalls, 0);

const directCalls = [];
await openDingTalkExternalLink('https://docs.example.com/a?name=中文#part', {
    targetWindow: {
        dd: {
            env: { appType: 'WEBVIEW_IN_MINIAPP' },
            openLink(params) {
                directCalls.push(params.url);
                return Promise.resolve();
            },
        },
    },
    duplicateWindowMs: 0,
});
assert.deepEqual(directCalls, [
    'https://docs.example.com/a?name=%E4%B8%AD%E6%96%87#part',
]);

await assert.rejects(
    openDingTalkExternalLink('https://void.example.com/path', {
        targetWindow: {
            dd: {
                env: { appType: 'WEBVIEW_IN_MINIAPP' },
                openLink() {},
            },
        },
        duplicateWindowMs: 0,
    }),
    /did not return a Promise/,
);

await assert.rejects(
    openDingTalkExternalLink('javascript:alert(1)', {
        targetWindow: {
            dd: {
                env: { appType: 'WEBVIEW_IN_MINIAPP' },
                openLink() {},
            },
        },
        duplicateWindowMs: 0,
    }),
    /only accepts HTTP\(S\)/,
);
await assert.rejects(
    openDingTalkExternalLink('https://missing.example.com', {
        targetWindow: {},
        duplicateWindowMs: 0,
    }),
    /SDK is unavailable/,
);
await assert.rejects(
    openDingTalkExternalLink('https://failure.example.com', {
        targetWindow: {
            dd: {
                env: { appType: 'WEBVIEW_IN_MINIAPP' },
                openLink() {
                    return Promise.reject(new Error('native failure'));
                },
            },
        },
        duplicateWindowMs: 0,
    }),
    /native failure/,
);
await assert.rejects(
    openDingTalkExternalLink('https://throw.example.com', {
        targetWindow: {
            dd: {
                env: { appType: 'WEBVIEW_IN_MINIAPP' },
                openLink() {
                    throw new Error('synchronous failure');
                },
            },
        },
        duplicateWindowMs: 0,
    }),
    /synchronous failure/,
);

let duplicateCalls = 0;
const duplicateWindow = {
    dd: {
        env: { appType: 'WEBVIEW_IN_MINIAPP' },
        openLink() {
            duplicateCalls += 1;
            return Promise.resolve();
        },
    },
};
const duplicateOptions = {
    targetWindow: duplicateWindow,
    duplicateWindowMs: 500,
    now: () => 10_000,
};
await openDingTalkExternalLink('https://duplicate.example.com/path', duplicateOptions);
await openDingTalkExternalLink('https://duplicate.example.com/path', duplicateOptions);
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

let unsafeOpenCalls = 0;
assert.equal(openExternalLinkWithBrowserDefault('javascript:alert(1)', {
    open() {
        unsafeOpenCalls += 1;
        return popup;
    },
}), false);
assert.equal(unsafeOpenCalls, 0);

assert.equal(openExternalLinkWithBrowserDefault('https://blocked.example.com/path', {
    open() {
        throw new Error('popup blocked');
    },
    location: {
        assign() {
            throw new Error('navigation blocked');
        },
    },
}), false);

console.log('dingtalk link runtime tests passed');
