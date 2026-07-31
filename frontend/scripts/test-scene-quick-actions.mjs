import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const {
    enabledSceneQuickActions,
    findEnabledSceneQuickAction,
    isSceneQuickActionUnavailable,
} = loadTypeScriptModule(
    resolve(__dirname, '../src/utils/sceneQuickActions.ts'),
);

const actions = [
    { id: 'default-enabled', label: 'Default' },
    { id: 'enabled', label: 'Enabled', enabled: true },
    { id: 'disabled', label: 'Disabled', enabled: false },
];

assert.deepEqual(
    enabledSceneQuickActions(actions).map((action) => action.id),
    ['default-enabled', 'enabled'],
);
assert.equal(findEnabledSceneQuickAction(actions, 'enabled')?.label, 'Enabled');
assert.equal(findEnabledSceneQuickAction(actions, 'disabled'), undefined);
assert.equal(
    isSceneQuickActionUnavailable(
        { id: 'send', type: 'send_message' },
        { confirmationPending: false, sendMessageUnavailable: true },
    ),
    true,
);
assert.equal(
    isSceneQuickActionUnavailable(
        { id: 'link', type: 'open_uri' },
        { confirmationPending: false, sendMessageUnavailable: true },
    ),
    false,
);
assert.equal(
    isSceneQuickActionUnavailable(
        { id: 'link', type: 'open_uri' },
        { confirmationPending: true, sendMessageUnavailable: false },
    ),
    true,
);

console.log('scene quick action tests passed');
