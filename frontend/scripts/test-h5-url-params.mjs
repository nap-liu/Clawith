import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));

const h5ParamsPath = resolve(__dirname, '../src/pages/h5/h5Params.ts');
const chatUrlParamsPath = resolve(__dirname, '../src/utils/chatUrlParams.ts');
const { parseH5Theme } = loadTypeScriptModule(h5ParamsPath);
const { parseChatSessionId, writeChatSessionIdToHref } = loadTypeScriptModule(chatUrlParamsPath);

assert.equal(parseH5Theme('light'), 'light');
assert.equal(parseH5Theme('dark'), 'dark');
assert.equal(parseH5Theme(' DARK '), 'dark');
assert.equal(parseH5Theme('system'), 'system');
assert.equal(parseH5Theme(''), 'system');
assert.equal(parseH5Theme('invalid'), 'system');
assert.equal(parseH5Theme(null), 'system');

assert.equal(parseChatSessionId(' session-1 '), 'session-1');
assert.equal(parseChatSessionId(''), null);
assert.equal(parseChatSessionId(null), null);

assert.equal(
    writeChatSessionIdToHref('https://example.test/h5/agents/a1/chat?channel=wechat_miniprogram&theme=dark#view', 's2'),
    '/h5/agents/a1/chat?channel=wechat_miniprogram&theme=dark&session_id=s2#view',
);
assert.equal(
    writeChatSessionIdToHref('https://example.test/h5/agents/a1/chat?theme=system#view', 's2'),
    '/h5/agents/a1/chat?theme=system&session_id=s2#view',
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
