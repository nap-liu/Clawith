import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(__dirname, '../src/pages/agent-detail/AgentDetailPage.tsx'), 'utf8');

assert.match(
    source,
    /const\s+applyMonitorEvent\s*=\s*\(prev:[\s\S]*?if\s*\(d\.type\s*===\s*['"]tool_call['"]\)/,
    'PC read-only monitor must fold live tool_call broadcasts into historyMsgs',
);

assert.match(
    source,
    /role:\s*['"]tool_call['"][\s\S]*?toolName:\s*d\.name[\s\S]*?toolCallId:\s*String\(d\.call_id\s*\|\|\s*d\.id\s*\|\|\s*d\.index\s*\|\|\s*['"]['"]\)[\s\S]*?toolResult:\s*d\.result/,
    'PC read-only monitor tool_call mapping must keep normalized tool fields used by file-delivery rendering',
);

assert.match(
    source,
    /activeReadOnlyRef\.current[\s\S]*?\[\s*['"]channel_user_message['"],\s*['"]thinking['"],\s*['"]chunk['"],\s*['"]tool_call['"],\s*['"]done['"]\s*\]\.includes\(d\.type\)/,
    'PC read-only monitor active event allow-list must include tool_call',
);

console.log('agent detail read-only monitor tests passed');
