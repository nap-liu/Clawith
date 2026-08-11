import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const {
    detectH5ContainerRuntime,
} = loadTypeScriptModule(
    resolve(__dirname, '../src/utils/h5ContainerRuntime.ts'),
    { clearTimeout, setTimeout },
);

assert.equal(await detectH5ContainerRuntime({
    targetWindow: {
        dd: {
            navigateTo() {},
        },
    },
    userAgent: 'Mozilla/5.0 DingTalk/8.0',
}), 'standard');

assert.equal(await detectH5ContainerRuntime({
    targetWindow: {
        dd: {
            navigateTo() {},
        },
    },
    userAgent: 'Mozilla/5.0 DingTalk/8.0 dd-web',
}), 'dingtalk-miniapp-webview');

assert.equal(await detectH5ContainerRuntime({
    targetWindow: {},
    userAgent: 'Mozilla/5.0 Chrome/150.0 Safari/537.36',
}), 'standard');

assert.equal(await detectH5ContainerRuntime({
    targetWindow: {},
    targetDocument: {
        addEventListener() {},
        removeEventListener() {},
    },
    userAgent: 'Mozilla/5.0 MicroMessenger/8.0',
    wechatBridgeWaitTimeoutMs: 0,
}), 'standard');

assert.equal(await detectH5ContainerRuntime({
    targetWindow: {
        __wxjs_environment: 'miniprogram',
        wx: {
            miniProgram: {
                getEnv(callback) {
                    callback({ miniprogram: true });
                },
                navigateTo() {},
            },
        },
    },
    userAgent: 'Mozilla/5.0 MicroMessenger/8.0 miniProgram',
    wechatEnvTimeoutMs: 50,
}), 'wechat-miniapp-webview');

assert.equal(await detectH5ContainerRuntime({
    targetWindow: {
        __wxjs_environment: 'miniprogram',
    },
    targetDocument: {
        addEventListener() {},
        removeEventListener() {},
    },
    userAgent: 'Mozilla/5.0 MicroMessenger/8.0 miniProgram',
}), 'wechat-miniapp-webview');

console.log('h5 container runtime tests passed');
