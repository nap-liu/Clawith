import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const ts = require('typescript');

const __dirname = dirname(fileURLToPath(import.meta.url));
const sourcePath = resolve(__dirname, '../src/utils/chatFileDelivery.ts');
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

const { parseFileDeliveryToolResult } = module.exports;

{
    const delivery = parseFileDeliveryToolResult(
        'send_channel_file',
        JSON.stringify({
            type: 'platform_file_delivery',
            path: 'workspace/reports/report.pdf',
            filename: 'report.pdf',
            message: '这是报告',
            mime_type: 'application/pdf',
            size: 123,
        }),
        {},
        'tc-file',
    );

    assert.equal(delivery.id, 'tc-file');
    assert.equal(delivery.path, 'workspace/reports/report.pdf');
    assert.equal(delivery.filename, 'report.pdf');
    assert.equal(delivery.message, '这是报告');
    assert.equal(delivery.mimeType, 'application/pdf');
    assert.equal(delivery.size, 123);
}

{
    const delivery = parseFileDeliveryToolResult(
        'send_channel_file',
        {
            type: 'platform_file_delivery',
            path: 'workspace/out/demo.csv',
        },
        { file_path: 'workspace/out/fallback.csv' },
    );

    assert.equal(delivery.path, 'workspace/out/demo.csv');
    assert.equal(delivery.filename, 'demo.csv');
}

{
    const delivery = parseFileDeliveryToolResult(
        'send_channel_file',
        '请下载\n\nFile ready: [report.pdf](https://evil.example/api/agents/other/files/download?path=workspace%2Freports%2Freport.pdf&token=bad)',
        {},
        'tc-legacy',
    );

    assert.equal(delivery.id, 'tc-legacy');
    assert.equal(delivery.path, 'workspace/reports/report.pdf');
    assert.equal(delivery.filename, 'report.pdf');
    assert.equal(delivery.message, '请下载');
}

assert.equal(parseFileDeliveryToolResult('send_channel_message', '{"type":"platform_file_delivery","path":"workspace/a.pdf"}'), null);
assert.equal(parseFileDeliveryToolResult('send_channel_file', '{"type":"platform_file_delivery","path":"/etc/passwd"}'), null);
assert.equal(parseFileDeliveryToolResult('send_channel_file', '{"type":"platform_file_delivery","path":"../secret.txt"}'), null);
assert.equal(parseFileDeliveryToolResult('send_channel_file', '{"type":"platform_file_delivery","path":"https://evil.example/a.pdf"}'), null);

console.log('chat file delivery tests passed');
