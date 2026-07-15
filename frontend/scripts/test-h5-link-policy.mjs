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

const { isWechatMiniProgramWebView, resolveExternalHttpLink } = module.exports;

assert.equal(
    isWechatMiniProgramWebView('Mozilla/5.0 MicroMessenger/8.0.50 miniProgram'),
    true,
);
assert.equal(
    isWechatMiniProgramWebView('Mozilla/5.0 MICROMESSENGER/8.0.50 MINIPROGRAM'),
    true,
);
assert.equal(isWechatMiniProgramWebView('Mozilla/5.0 MicroMessenger/8.0.50'), false);
assert.equal(isWechatMiniProgramWebView('Mozilla/5.0 DingTalk/7.6.0'), false);
assert.equal(isWechatMiniProgramWebView('Mozilla/5.0 Chrome/150.0.0.0 Safari/537.36'), false);

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

console.log('h5 link policy tests passed');
