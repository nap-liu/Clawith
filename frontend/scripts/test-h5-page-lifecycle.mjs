import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const { installH5PageLifecycle } = loadTypeScriptModule(
    resolve(__dirname, '../src/utils/h5PageLifecycle.ts'),
);

class FakeEventTarget {
    listeners = new Map();

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || new Set();
        listeners.add(listener);
        this.listeners.set(type, listeners);
    }

    removeEventListener(type, listener) {
        this.listeners.get(type)?.delete(listener);
    }

    dispatch(type, event = {}) {
        for (const listener of this.listeners.get(type) || []) listener(event);
    }
}

const windowTarget = new FakeEventTarget();
windowTarget.innerHeight = 760;
windowTarget.visualViewport = new FakeEventTarget();
windowTarget.visualViewport.height = 720;

const styleValues = new Map();
const documentTarget = new FakeEventTarget();
documentTarget.visibilityState = 'visible';
documentTarget.documentElement = {
    style: {
        setProperty(name, value) {
            styleValues.set(name, value);
        },
        removeProperty(name) {
            styleValues.delete(name);
        },
    },
};

const events = [];
const cleanup = installH5PageLifecycle({
    targetWindow: windowTarget,
    targetDocument: documentTarget,
    onSuspend: () => events.push('suspend'),
    onResume: (reason) => events.push(`resume:${reason}`),
});

assert.equal(styleValues.get('--h5-viewport-height'), '720px');

documentTarget.visibilityState = 'hidden';
documentTarget.dispatch('visibilitychange');
windowTarget.dispatch('pagehide');
assert.deepEqual(events, ['suspend']);

windowTarget.visualViewport.height = 690;
documentTarget.visibilityState = 'visible';
documentTarget.dispatch('visibilitychange');
assert.equal(styleValues.get('--h5-viewport-height'), '690px');
assert.deepEqual(events, ['suspend', 'resume:visible']);

windowTarget.dispatch('pagehide');
windowTarget.dispatch('pageshow', { persisted: false });
assert.deepEqual(events, [
    'suspend',
    'resume:visible',
    'suspend',
    'resume:pageshow',
]);

windowTarget.dispatch('online');
assert.equal(events.at(-1), 'resume:online');

cleanup();
assert.equal(styleValues.has('--h5-viewport-height'), false);
assert.equal(documentTarget.listeners.get('visibilitychange').size, 0);

console.log('h5 page lifecycle tests passed');
