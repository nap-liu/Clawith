import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const {
    findMenuVisibleSceneQuickAction,
    isSceneQuickActionUnavailable,
    menuVisibleSceneQuickActions,
} = loadTypeScriptModule(
    resolve(__dirname, '../src/utils/sceneQuickActions.ts'),
);

const actions = [
    { id: 'default-visible', label: 'Default' },
    { id: 'menu-only', label: 'Menu only', menu_visible: true, ai_visible: false },
    { id: 'ai-only', label: 'AI only', menu_visible: false, ai_visible: true },
    { id: 'legacy-visible', label: 'Legacy visible', enabled: true },
    { id: 'legacy-hidden', label: 'Legacy hidden', enabled: false },
];

assert.deepEqual(
    menuVisibleSceneQuickActions(actions).map((action) => action.id),
    ['default-visible', 'menu-only', 'legacy-visible'],
);
assert.equal(findMenuVisibleSceneQuickAction(actions, 'menu-only')?.label, 'Menu only');
assert.equal(findMenuVisibleSceneQuickAction(actions, 'ai-only'), undefined);
assert.equal(findMenuVisibleSceneQuickAction(actions, 'legacy-hidden'), undefined);
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
