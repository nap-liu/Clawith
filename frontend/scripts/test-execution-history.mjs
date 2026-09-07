import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const { chatSessionApi } = loadTypeScriptModule(
    fileURLToPath(new URL('../src/services/api.ts', import.meta.url)),
    {
        fetch: (...args) => globalThis.fetch(...args),
        localStorage: { getItem: () => 'test-token', removeItem() {} },
        window: { location: { href: 'http://test', pathname: '/agents', replace() {} } },
        Response,
        AbortSignal,
    },
);

const rows = Array.from({ length: 621 }, (_, index) => ({
    id: String(index),
    role: index === 0 ? 'user' : index === 620 ? 'assistant' : 'tool_call',
    content: index === 0 ? 'Full task instructions\n' + 'Evidence requirement.\n'.repeat(600) : `Record ${index}`,
}));
let requests = [];
globalThis.fetch = async (url) => {
    const query = new URL(url, 'http://test').searchParams;
    requests.push(query.get('before'));
    assert.equal(query.get('paginated'), 'true');
    assert.equal(query.get('limit'), '500');
    const older = query.has('before');
    return Response.json({
        items: older ? rows.slice(0, 122) : rows.slice(121),
        has_more: !older,
        next_cursor: older ? null : 'timestamp|121',
    });
};
const full = await chatSessionApi.allMessages('agent', 'session');
assert.deepEqual(requests, [null, 'timestamp|121']);
assert.equal(full.length, 621);
assert.equal(new Set(full.map(row => row.id)).size, 621);
assert.equal(full[0].content, rows[0].content);
assert.equal(full.at(-1).content, 'Record 620');

let fail = true;
globalThis.fetch = async (url) => {
    const older = new URL(url, 'http://test').searchParams.has('before');
    if (older && fail) return Response.json({ detail: 'temporary failure' }, { status: 503 });
    return Response.json({ items: older ? rows.slice(0, 121) : rows.slice(121),
        has_more: !older, next_cursor: older ? null : 'timestamp|121' });
};
await assert.rejects(chatSessionApi.allMessages('agent', 'session'), /temporary failure/);
fail = false;
assert.equal((await chatSessionApi.allMessages('agent', 'session'))[0].id, '0');

for (const next_cursor of [null, 'unchanged']) {
    globalThis.fetch = async () => Response.json({ items: [rows[620]], has_more: true, next_cursor });
    await assert.rejects(chatSessionApi.allMessages('agent', 'session'), /pagination did not advance/);
}
globalThis.fetch = async () => Response.json({ items: [], has_more: false, next_cursor: null });
assert.equal((await chatSessionApi.allMessages('agent', 'session')).length, 0);
console.log('Execution history: full instructions, 621 rows, overlap, failure/retry, cursor guard and empty history passed.');
