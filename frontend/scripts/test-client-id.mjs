import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const sourcePath = resolve(__dirname, '../src/utils/clientId.ts');
const uuidV4Pattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

{
    const { createClientId } = loadTypeScriptModule(sourcePath, {
        crypto: {
            randomUUID: () => '123e4567-e89b-42d3-a456-426614174000',
        },
    });
    assert.equal(createClientId(), '123e4567-e89b-42d3-a456-426614174000');
}

{
    const { createClientId } = loadTypeScriptModule(sourcePath, {
        crypto: {
            getRandomValues: (bytes) => {
                bytes.fill(0xab);
                return bytes;
            },
        },
    });
    assert.match(createClientId(), uuidV4Pattern);
}

{
    const { createClientId } = loadTypeScriptModule(sourcePath);
    assert.match(createClientId(), uuidV4Pattern);
}

console.log('client ID compatibility tests passed');
