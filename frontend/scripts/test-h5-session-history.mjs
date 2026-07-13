import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const api = readFileSync(resolve(__dirname, '../src/services/api.ts'), 'utf8');
const h5 = readFileSync(resolve(__dirname, '../src/pages/h5/H5AgentChat.tsx'), 'utf8');
const css = readFileSync(resolve(__dirname, '../src/pages/h5/H5AgentChat.css'), 'utf8');

assert.match(
    api,
    /list:\s*\(\s*agentId:\s*string,\s*options:\s*\{[\s\S]*?scope\?:[\s\S]*?source_channel\?:[\s\S]*?limit\?:[\s\S]*?offset\?:/,
    'chatSessionApi.list must expose scope/source_channel/limit/offset for H5 history',
);
assert.match(api, /params\.set\(['"]source_channel['"]/, 'chatSessionApi.list must send source_channel when provided');

assert.match(
    h5,
    /parseChatSessionId\(new URLSearchParams\(searchString\)\.get\(['"]session_id['"]\)\)/,
    'H5 chat must parse session_id from URL',
);
assert.match(
    h5,
    /chatSessionApi\.list\(agentId,\s*\{\s*scope:\s*['"]mine['"],\s*limit:\s*50,\s*offset:\s*0,?\s*\}\)/,
    'H5 history must load all channel types with a bounded page size',
);
assert.doesNotMatch(
    h5,
    /chatSessionApi\.list\(agentId,[\s\S]{0,160}source_channel:\s*channel/,
    'H5 history must not hide sessions from other channel types',
);
assert.match(h5, /const\s+activateSession\s*=\s*useCallback/, 'H5 chat must implement session activation');
assert.match(
    h5,
    /writeChatSessionIdToHref\(window\.location\.href,\s*nextSessionId\)/,
    'H5 chat must write selected session_id back to URL',
);
assert.match(h5, /aria-label=["']历史会话["']/, 'H5 header must expose a history session button');
assert.match(h5, /h5-chat__session-panel/, 'H5 chat must render a session history panel');
assert.match(css, /\.h5-chat__session-panel/, 'H5 CSS must style the session history panel');

console.log('h5 session history tests passed');
