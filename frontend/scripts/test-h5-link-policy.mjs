import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const ts = require('typescript');

const __dirname = dirname(fileURLToPath(import.meta.url));
const sourcePath = resolve(__dirname, '../src/utils/h5LinkPolicy.ts');
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
    URL,
}, { filename: sourcePath });

const {
    resolveExternalHttpLink,
    resolveH5LinkAction,
} = module.exports;

function assertLinkAction(actual, expected) {
    assert.equal(actual.type, expected.type);
    assert.equal(actual.url, expected.url);
    assert.equal(actual.reason, expected.reason);
}

const current = 'https://ai.example.test/h5/agents/a1/chat?channel=wechat_miniprogram';
assert.equal(resolveExternalHttpLink('/api/health', current), null);
assert.equal(resolveExternalHttpLink('https://ai.example.test/docs?q=1', current), null);
assert.equal(
    resolveExternalHttpLink('https://docs.example.com/a?name=中文#part', current),
    'https://docs.example.com/a?name=%E4%B8%AD%E6%96%87#part',
);
assert.equal(
    resolveExternalHttpLink('http://docs.example.com/path', current),
    'http://docs.example.com/path',
);
assert.equal(resolveExternalHttpLink('mailto:help@example.com', current), null);
assert.equal(resolveExternalHttpLink('javascript:alert(1)', current), null);
assert.equal(resolveExternalHttpLink('https://[invalid', current), null);

assertLinkAction(
    resolveH5LinkAction('https://docs.example.com/path?q=中文#part', {
        currentHref: current,
    }),
    { type: 'native' },
);
assertLinkAction(
    resolveH5LinkAction('https://docs.example.com/path?q=中文#part', {
        currentHref: current,
        runtime: 'dingtalk-miniapp-webview',
    }),
    {
        type: 'dingtalk-open',
        url: 'https://docs.example.com/path?q=%E4%B8%AD%E6%96%87#part',
    },
);
assertLinkAction(
    resolveH5LinkAction('https://docs.example.com/path', {
        currentHref: current,
        runtime: 'wechat-miniapp-webview',
    }),
    {
        type: 'blocked',
        reason: 'wechat-cross-origin',
        url: 'https://docs.example.com/path',
    },
);
assertLinkAction(
    resolveH5LinkAction('/docs?q=中文#part', {
        currentHref: current,
        runtime: 'wechat-miniapp-webview',
    }),
    {
        type: 'wechat-miniapp-open',
        url: 'https://ai.example.test/docs?q=%E4%B8%AD%E6%96%87#part',
    },
);
for (const href of [
    'http://ai.example.test/docs',
    'https://ai.example.test:8443/docs',
    'https://sub.ai.example.test/docs',
    'https://ai.example.test.evil.test/docs',
]) {
    assertLinkAction(
        resolveH5LinkAction(href, {
            currentHref: current,
            runtime: 'wechat-miniapp-webview',
        }),
        {
            type: 'blocked',
            reason: 'wechat-cross-origin',
            url: new URL(href).href,
        },
    );
}
assertLinkAction(
    resolveH5LinkAction('https://docs.example.com/path', {
        currentHref: `${current}&channel=dingtalk`,
    }),
    { type: 'native' },
);
assertLinkAction(
    resolveH5LinkAction('https://docs.example.com/path', {
        currentHref: current,
        runtime: 'standard',
    }),
    { type: 'native' },
);
assertLinkAction(
    resolveH5LinkAction('/docs', {
        currentHref: current,
        runtime: 'dingtalk-miniapp-webview',
    }),
    { type: 'dingtalk-open', url: 'https://ai.example.test/docs' },
);
for (const href of ['mailto:help@example.com', 'tel:10086', 'javascript:alert(1)']) {
    assertLinkAction(
        resolveH5LinkAction(href, {
            currentHref: current,
            runtime: 'dingtalk-miniapp-webview',
        }),
        { type: 'native' },
    );
}

console.log('h5 link policy tests passed');
