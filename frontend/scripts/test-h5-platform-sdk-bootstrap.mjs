import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(resolve(__dirname, '../index.html'), 'utf8');
const bootstrapMatch = html.match(/<script id="h5-platform-sdk-bootstrap">([\s\S]*?)<\/script>/);
assert.ok(bootstrapMatch);

function runBootstrap(pathname, userAgent) {
    const writes = [];
    vm.runInNewContext(bootstrapMatch[1], {
        window: { location: { pathname } },
        navigator: { userAgent },
        document: { writeln: (value) => writes.push(value) },
    });
    return writes;
}

const appxScript = '<script src="https://appx/web-view.min.js"></script>';
const wechatScript = '<script src="https://res.wx.qq.com/open/js/jweixin-1.3.2.js"></script>';

assert.deepEqual(runBootstrap('/agents/a1/chat', 'DingTalk/8.0 dd-web'), []);
assert.deepEqual(runBootstrap('/h5/agents/a1/chat', 'Chrome/150.0'), []);
assert.deepEqual(runBootstrap('/h5/agents/a1/chat', 'DingTalk/8.0 dd-web'), [appxScript]);
assert.deepEqual(runBootstrap('/h5/agents/a1/chat', 'dingtalk/8.0 dd-web'), [appxScript]);
assert.deepEqual(runBootstrap('/h5/agents/a1/chat', 'AliApp(AP/10.7.66.8000)'), [appxScript]);
assert.deepEqual(
    runBootstrap('/h5/agents/a1/chat', 'DingTalk/8.0 dd-web AliApp(AP/10.7.66.8000)'),
    [appxScript],
);
assert.deepEqual(runBootstrap('/h5/agents/a1/chat', 'MicroMessenger/8.0'), [wechatScript]);
assert.deepEqual(
    runBootstrap('/h5/agents/a1/chat', 'DingTalk/8.0 dd-web MicroMessenger/8.0'),
    [appxScript, wechatScript],
);

console.log('H5 platform SDK bootstrap tests passed');
