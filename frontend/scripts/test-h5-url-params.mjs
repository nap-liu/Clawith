import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const ts = require('typescript');

const __dirname = dirname(fileURLToPath(import.meta.url));
function loadTypeScriptModule(sourcePath) {
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
    return module.exports;
}

const h5ParamsPath = resolve(__dirname, '../src/pages/h5/h5Params.ts');
const chatUrlParamsPath = resolve(__dirname, '../src/utils/chatUrlParams.ts');
const { parseH5Theme } = loadTypeScriptModule(h5ParamsPath);
const { parseChatSessionId, writeChatSessionIdToHref } = loadTypeScriptModule(chatUrlParamsPath);

assert.equal(parseH5Theme('light'), 'light');
assert.equal(parseH5Theme('dark'), 'dark');
assert.equal(parseH5Theme('system'), 'light');
assert.equal(parseH5Theme(''), 'light');
assert.equal(parseH5Theme(null), 'light');

assert.equal(typeof parseChatSessionId, 'function', 'shared chat params must export parseChatSessionId');
assert.equal(parseChatSessionId(' session-1 '), 'session-1');
assert.equal(parseChatSessionId(''), null);
assert.equal(parseChatSessionId(null), null);

assert.equal(typeof writeChatSessionIdToHref, 'function', 'shared chat params must export writeChatSessionIdToHref');
assert.equal(
    writeChatSessionIdToHref('https://example.test/h5/agents/a1/chat?channel=wechat_miniprogram&theme=dark#view', 's2'),
    '/h5/agents/a1/chat?channel=wechat_miniprogram&theme=dark&session_id=s2#view',
);
assert.equal(
    writeChatSessionIdToHref('https://example.test/h5/agents/a1/chat?channel=wechat_miniprogram&session_id=s1', null),
    '/h5/agents/a1/chat?channel=wechat_miniprogram',
);
assert.equal(
    writeChatSessionIdToHref('https://example.test/agents/a1/chat?theme=dark#latest', 's3'),
    '/agents/a1/chat?theme=dark&session_id=s3#latest',
);

console.log('h5 url param tests passed');
