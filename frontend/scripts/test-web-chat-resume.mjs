import assert from 'node:assert/strict';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const resumeModulePath = resolve(__dirname, '../src/features/conversation/core/resumeRecovery.ts');
const {
    prepareMessagesForActiveTurnResume,
    resolveVisibleTerminalRecoveryAction,
    shouldCompleteRecoveryPolling,
} = loadTypeScriptModule(resumeModulePath);
const {
    bufferResumeEvent,
    createResumeEventGate,
    drainResumeEventGate,
} = loadTypeScriptModule(resumeModulePath);

const liveTurn = [
    { id: 'old-user', role: 'user', content: 'an older failed request' },
    { id: 'old-partial', role: 'assistant', content: 'keep this failed partial', _streaming: true },
    { id: 'user', role: 'user', content: 'run a long task' },
    { id: 'stream-a', role: 'assistant', content: 'partial before tool', _streaming: true },
    { id: 'tool', role: 'tool_call', toolCallId: 'call-1', toolStatus: 'running' },
    { id: 'stream-b', role: 'assistant', content: 'partial after tool', streaming: true },
    { id: 'committed-media', role: 'assistant', content: 'committed attachment caption' },
];

assert.deepEqual(
    prepareMessagesForActiveTurnResume(liveTurn).map((message) => message.id),
    ['old-user', 'old-partial', 'user', 'tool', 'committed-media'],
    'foreground resume must remove only client-local assistant rows in the current turn',
);

const durableOnly = [
    { id: 'user', role: 'user', content: 'hello' },
    { id: 'reply', role: 'assistant', content: 'done' },
];
assert.equal(
    prepareMessagesForActiveTurnResume(durableOnly),
    durableOnly,
    'an ordinary foreground transition must preserve the timeline reference',
);
assert.deepEqual(
    prepareMessagesForActiveTurnResume([
        { id: 'onboarding-stream', role: 'assistant', content: 'welcome...', streaming: true },
        { id: 'onboarding-committed', role: 'assistant', content: 'welcome' },
    ]).map((message) => message.id),
    ['onboarding-committed'],
    'assistant-first onboarding resume must remove transient output without a user row',
);

const gate = createResumeEventGate('agent:session-a', 7);
assert.equal(bufferResumeEvent(gate, 'agent:session-b', { type: 'chunk', value: 'wrong' }), false);
assert.equal(bufferResumeEvent(gate, 'agent:session-a', { type: 'chunk', value: 'A' }), true);
assert.equal(bufferResumeEvent(gate, 'agent:session-a', { type: 'tool_call', value: 'tool-1' }), true);
assert.equal(bufferResumeEvent(gate, 'agent:session-a', { type: 'chunk', value: 'B' }), true);
assert.equal(
    drainResumeEventGate(gate).map((event) => `${event.type}:${event.value}`).join(','),
    'chunk:A,tool_call:tool-1,chunk:B',
    'events received during durable reconciliation must replay in arrival order',
);
assert.equal(drainResumeEventGate(gate).length, 0, 'a resume generation must drain exactly once');

assert.equal(
    resolveVisibleTerminalRecoveryAction({ isActiveRuntime: true, recoveryNeeded: true }),
    'continue',
    'a visible terminal event must not cancel an in-flight route/background recovery window',
);
assert.equal(
    resolveVisibleTerminalRecoveryAction({ isActiveRuntime: true, recoveryNeeded: false }),
    'clear',
    'an ordinary locally observed terminal event must keep the fast no-poll path',
);
assert.equal(
    resolveVisibleTerminalRecoveryAction({ isActiveRuntime: false, recoveryNeeded: true }),
    'ignore',
    'a background session terminal event must not mutate the active recovery window',
);
assert.equal(shouldCompleteRecoveryPolling({
    pollingGeneration: 4,
    currentGeneration: 5,
    loadedSuccessfully: true,
    successfulLoads: 2,
    stillActive: true,
    socketReadyState: 1,
}), false, 'a stale poll owner must not clear a newer recovery generation');
assert.equal(shouldCompleteRecoveryPolling({
    pollingGeneration: 5,
    currentGeneration: 5,
    loadedSuccessfully: false,
    successfulLoads: 0,
    stillActive: true,
    socketReadyState: 1,
}), false, 'failed history requests must retain the recovery marker and retry window');
assert.equal(shouldCompleteRecoveryPolling({
    pollingGeneration: 5,
    currentGeneration: 5,
    loadedSuccessfully: true,
    successfulLoads: 1,
    stillActive: true,
    socketReadyState: 1,
}), false, 'one early snapshot is not enough to close the persistence window');
assert.equal(shouldCompleteRecoveryPolling({
    pollingGeneration: 5,
    currentGeneration: 5,
    loadedSuccessfully: true,
    successfulLoads: 2,
    stillActive: true,
    socketReadyState: 1,
}), true, 'two successful snapshots from the current generation may finish recovery');

console.log('web chat foreground resume tests passed');
