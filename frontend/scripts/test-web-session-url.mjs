import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const page = readFileSync(resolve(__dirname, '../src/pages/agent-detail/AgentDetailPage.tsx'), 'utf8');
const api = readFileSync(resolve(__dirname, '../src/services/api.ts'), 'utf8');

assert.match(
    page,
    /parseChatSessionId\(new URLSearchParams\(location\.search\)\.get\(['"]session_id['"]\)\)/,
    'Web chat must parse session_id from the route URL',
);
assert.match(
    page,
    /writeChatSessionIdToHref\(window\.location\.href,\s*sessionId\)/,
    'Web chat must preserve the selected session_id in the URL',
);
assert.match(
    page,
    /chatSessionApi\.get\(id,\s*requestedSessionId\)/,
    'Web chat must resolve URL sessions directly instead of relying on the paged session list',
);
assert.match(
    api,
    /get:\s*\(agentId:\s*string,\s*sessionId:\s*string\)[\s\S]*?\/sessions\/\$\{sessionId\}/,
    'The frontend API must expose direct session lookup',
);

console.log('web session URL tests passed');
