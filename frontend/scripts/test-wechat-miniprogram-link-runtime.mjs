import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const {
    buildWechatMiniProgramWebviewRoute,
    isWechatMiniProgramWebViewRuntime,
    openWechatMiniProgramWebview,
} = loadTypeScriptModule(
    resolve(__dirname, '../src/utils/wechatMiniProgramLink.ts'),
    { clearTimeout, setTimeout },
);

assert.equal(
    buildWechatMiniProgramWebviewRoute('https://docs.example.com/a?name=中文#part'),
    '/subPackages/webview/index?url=https%3A%2F%2Fdocs.example.com%2Fa%3Fname%3D%E4%B8%AD%E6%96%87%23part',
);

let directWechatSdkLoads = 0;
const directWechatDocument = {
    addEventListener() {},
    removeEventListener() {},
    querySelector() {
        return null;
    },
    createElement() {
        directWechatSdkLoads += 1;
        throw new Error('JSSDK must not load in a direct WeChat browser');
    },
};
assert.equal(await isWechatMiniProgramWebViewRuntime({
    targetWindow: {},
    targetDocument: directWechatDocument,
    bridgeWaitTimeoutMs: 0,
}), false);
assert.equal(directWechatSdkLoads, 0);

const failedWechatScriptListeners = new Map();
const failedWechatScript = {
    dataset: {},
    parentNode: {
        removeChild() {},
    },
    addEventListener(type, listener) {
        failedWechatScriptListeners.set(type, listener);
    },
    removeEventListener(type) {
        failedWechatScriptListeners.delete(type);
    },
};
assert.equal(await isWechatMiniProgramWebViewRuntime({
    targetWindow: {
        __wxjs_environment: 'miniprogram',
    },
    targetDocument: {
        addEventListener() {},
        removeEventListener() {},
        querySelector() {
            return null;
        },
        createElement() {
            return failedWechatScript;
        },
        head: {
            appendChild() {
                queueMicrotask(() => failedWechatScriptListeners.get('error')?.());
            },
        },
    },
    envTimeoutMs: 50,
}), true);

const miniProgramWindow = {
    __wxjs_environment: 'miniprogram',
    wx: {
        miniProgram: {
            getEnv(callback) {
                callback({ miniprogram: true });
            },
            navigateTo() {},
        },
    },
};
assert.equal(await isWechatMiniProgramWebViewRuntime({
    targetWindow: miniProgramWindow,
    envTimeoutMs: 50,
}), true);

const ordinaryWechatWindow = {
    __wxjs_environment: 'miniprogram',
    wx: {
        miniProgram: {
            getEnv(callback) {
                callback({ miniprogram: false });
            },
            navigateTo() {},
        },
    },
};
assert.equal(await isWechatMiniProgramWebViewRuntime({
    targetWindow: ordinaryWechatWindow,
    envTimeoutMs: 50,
}), false);

const navigateCalls = [];
await openWechatMiniProgramWebview('https://docs.example.com/a?name=中文#part', {
    currentHref: 'https://docs.example.com/h5/chat',
    targetWindow: {
        __wxjs_environment: 'miniprogram',
        wx: {
            miniProgram: {
                navigateTo(options) {
                    navigateCalls.push(options.url);
                    options.success?.();
                },
            },
        },
    },
    duplicateWindowMs: 0,
});
assert.deepEqual(navigateCalls, [
    '/subPackages/webview/index?url=https%3A%2F%2Fdocs.example.com%2Fa%3Fname%3D%25E4%25B8%25AD%25E6%2596%2587%23part',
]);

await assert.rejects(
    openWechatMiniProgramWebview('https://direct.example.com/path', {
        currentHref: 'https://direct.example.com/h5/chat',
        targetWindow: {
            wx: {
                miniProgram: {
                    navigateTo() {},
                },
            },
        },
        duplicateWindowMs: 0,
    }),
    /navigation SDK is unavailable/,
);
await assert.rejects(
    openWechatMiniProgramWebview('javascript:alert(1)', {
        currentHref: 'https://docs.example.com/h5/chat',
        targetWindow: miniProgramWindow,
        duplicateWindowMs: 0,
    }),
    /only accepts HTTP\(S\)/,
);
await assert.rejects(
    openWechatMiniProgramWebview('https://failure.example.com/path', {
        currentHref: 'https://failure.example.com/h5/chat',
        targetWindow: {
            __wxjs_environment: 'miniprogram',
            wx: {
                miniProgram: {
                    navigateTo(options) {
                        options.fail?.(new Error('route failure'));
                    },
                },
            },
        },
        duplicateWindowMs: 0,
    }),
    /route failure/,
);

let duplicateCalls = 0;
const duplicateOptions = {
    currentHref: 'https://duplicate.example.com/h5/chat',
    targetWindow: {
        __wxjs_environment: 'miniprogram',
        wx: {
            miniProgram: {
                navigateTo(options) {
                    duplicateCalls += 1;
                    options.success?.();
                },
            },
        },
    },
    duplicateWindowMs: 500,
    now: () => 20_000,
};
await openWechatMiniProgramWebview('https://duplicate.example.com/path', duplicateOptions);
await openWechatMiniProgramWebview('https://duplicate.example.com/path', duplicateOptions);
assert.equal(duplicateCalls, 1);

await assert.rejects(
    openWechatMiniProgramWebview('https://external.example.com/path', {
        currentHref: 'https://ai.example.test/h5/chat',
        targetWindow: miniProgramWindow,
        duplicateWindowMs: 0,
    }),
    /only allows same-origin URLs/,
);

console.log('wechat mini-program link runtime tests passed');
