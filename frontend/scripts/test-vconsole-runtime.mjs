import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(resolve(__dirname, '../index.html'), 'utf8');
const bootstrapMatch = html.match(/<script id="vconsole-bootstrap">([\s\S]*?)<\/script>/);
const initMatch = html.match(/<script id="vconsole-init">([\s\S]*?)<\/script>/);
assert.ok(bootstrapMatch);
assert.ok(initMatch);
assert.ok(html.indexOf('id="vconsole-bootstrap"') < html.indexOf('src="/src/main.tsx"'));

function runBootstrap(search) {
    const writes = [];
    vm.runInNewContext(bootstrapMatch[1], {
        URLSearchParams,
        window: { location: { search } },
        document: { write: (value) => writes.push(value) },
    });
    return writes;
}

assert.deepEqual(runBootstrap(''), []);
assert.deepEqual(runBootstrap('?vconsole=0'), []);
assert.deepEqual(runBootstrap('?foo=1&vconsole=1'), [
    '<script src="https://cdn.jsdelivr.net/npm/vconsole@latest/dist/vconsole.min.js"></script>',
]);
assert.deepEqual(runBootstrap('?vconsole=10'), []);

let instanceCount = 0;
const targetWindow = {
    location: { search: '?vconsole=1' },
    VConsole: class {
        constructor() {
            instanceCount += 1;
        }
    },
};
vm.runInNewContext(initMatch[1], { URLSearchParams, window: targetWindow });
vm.runInNewContext(initMatch[1], { URLSearchParams, window: targetWindow });
assert.equal(instanceCount, 1);
assert.ok(targetWindow.__clawithVConsole);

console.log('vConsole runtime tests passed');
