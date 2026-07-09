import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const ts = require('typescript');

const __dirname = dirname(fileURLToPath(import.meta.url));
const sourcePath = resolve(__dirname, '../src/pages/h5/h5Params.ts');
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

const { parseH5SessionId, parseH5Theme, writeH5SessionIdToHref } = module.exports;

assert.equal(parseH5Theme('light'), 'light');
assert.equal(parseH5Theme('dark'), 'dark');
assert.equal(parseH5Theme('system'), 'light');
assert.equal(parseH5Theme(''), 'light');
assert.equal(parseH5Theme(null), 'light');

assert.equal(typeof parseH5SessionId, 'function', 'H5 params must export parseH5SessionId');
assert.equal(parseH5SessionId(' session-1 '), 'session-1');
assert.equal(parseH5SessionId(''), null);
assert.equal(parseH5SessionId(null), null);

assert.equal(typeof writeH5SessionIdToHref, 'function', 'H5 params must export writeH5SessionIdToHref');
assert.equal(
    writeH5SessionIdToHref('https://example.test/h5/agents/a1/chat?channel=wechat_miniprogram&theme=dark#view', 's2'),
    '/h5/agents/a1/chat?channel=wechat_miniprogram&theme=dark&session_id=s2#view',
);
assert.equal(
    writeH5SessionIdToHref('https://example.test/h5/agents/a1/chat?channel=wechat_miniprogram&session_id=s1', null),
    '/h5/agents/a1/chat?channel=wechat_miniprogram',
);

console.log('h5 url param tests passed');
