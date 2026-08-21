import assert from 'node:assert/strict';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const {
    inferProjectSessionIntent,
    resolveProjectSessionRoute,
} = loadTypeScriptModule(resolve(
    __dirname,
    '../src/features/projects/projectSessionRouting.ts',
));
const plain = (value) => JSON.parse(JSON.stringify(value));

{
    const route = resolveProjectSessionRoute({
        id: 'project-run-domain-id',
        agent_id: 'leader-agent',
        trigger_type: 'manual',
        input: { dispatch: { turn_anchor_id: 'run-turn-anchor' } },
        output: { subagent_session_id: 'leader-child-session' },
    }, 'run');
    assert.deepEqual(plain(route), {
        sessionId: 'leader-child-session',
        kind: 'session',
        agentId: 'leader-agent',
        intent: 'run',
        anchorMessageId: 'run-turn-anchor',
    });
}

{
    const first = resolveProjectSessionRoute({
        subagent_session_id: 'shared-child-session',
        agent_id: 'worker-agent',
        input: { dispatch: { turn_anchor_id: 'first-run-turn' } },
    }, 'run');
    const second = resolveProjectSessionRoute({
        subagent_session_id: 'shared-child-session',
        agent_id: 'worker-agent',
        input: { dispatch: { turn_anchor_id: 'second-run-turn' } },
    }, 'run');
    assert.equal(first?.sessionId, second?.sessionId);
    assert.equal(first?.anchorMessageId, 'first-run-turn');
    assert.equal(second?.anchorMessageId, 'second-run-turn');
    assert.notEqual(first?.anchorMessageId, second?.anchorMessageId, 'Run routing must preserve its exact turn inside a shared session');
}

assert.equal(
    resolveProjectSessionRoute({ id: 'project-run-domain-id', agent_id: 'leader-agent' }, 'run'),
    null,
    'a ProjectRun id must never be treated as a ChatSession id',
);

{
    const event = {
        id: 'event-domain-id',
        event_type: 'a2a.delivered',
        from_agent_id: 'worker-z',
        to_agent_id: 'reviewer-a',
        event_metadata: {
            session_id: 'exact-a2a-session',
            session_access_agent_id: 'reviewer-a',
            group_session_id: 'project-group-session',
        },
    };
    assert.equal(inferProjectSessionIntent(event), 'a2a');
    assert.deepEqual(plain(resolveProjectSessionRoute(event)), {
        sessionId: 'exact-a2a-session',
        kind: 'session',
        agentId: 'reviewer-a',
        intent: 'a2a',
    });
    assert.equal(
        resolveProjectSessionRoute(event, 'group')?.sessionId,
        'project-group-session',
        'an explicit group route must not accidentally open the A2A thread',
    );
}

{
    const a2aRun = {
        id: 'a2a-project-run-id',
        trigger_type: 'a2a',
        from_agent_id: 'worker-z',
        to_agent_id: 'reviewer-a',
        output: {
            session_id: 'a2a-run-session',
            session_agent_id: 'reviewer-a',
        },
    };
    assert.equal(inferProjectSessionIntent(a2aRun), 'a2a');
    assert.equal(resolveProjectSessionRoute(a2aRun)?.sessionId, 'a2a-run-session');
}

{
    const enrichedA2ARun = {
        id: 'a2a-project-run-id',
        trigger_type: 'a2a',
        agent_id: 'reviewer-a',
        session_id: 'visible-a2a-session',
        subagent_session_id: 'worker-child-session',
    };
    assert.deepEqual(plain(resolveProjectSessionRoute(enrichedA2ARun)), {
        sessionId: 'visible-a2a-session',
        kind: 'session',
        agentId: 'reviewer-a',
        intent: 'a2a',
    }, 'the stable Run DTO must open the visible A2A conversation, not its worker child');
}

{
    const workItemSession = {
        run_id: 'project-run-id',
        source_channel: 'subagent',
        session_id: 'exact-worker-session',
        agent_id: 'worker-agent',
    };
    assert.deepEqual(plain(resolveProjectSessionRoute(workItemSession)), {
        sessionId: 'exact-worker-session',
        kind: 'session',
        agentId: 'worker-agent',
        intent: 'run',
    }, 'a WorkItemDetailOut session record must route directly to its exact ChatSession');
}

{
    const route = resolveProjectSessionRoute({
        event_type: 'a2a.queued',
        from_agent_id: 'z-agent',
        to_agent_id: 'a-agent',
        metadata: JSON.stringify({ session_id: 'a2a-json-session' }),
    });
    assert.equal(route?.sessionId, 'a2a-json-session');
    assert.equal(route?.agentId, 'a-agent', 'A2A fallback access identity must match the backend canonical min id');
}

assert.deepEqual(plain(resolveProjectSessionRoute({
    id: 'group-chat-session',
    source_channel: 'project',
    access_agent_id: 'leader-agent',
})), {
    sessionId: 'group-chat-session',
    kind: 'group',
    agentId: 'leader-agent',
    intent: 'group',
});

assert.deepEqual(plain(resolveProjectSessionRoute({
    id: 'a2a-chat-session',
    source_channel: 'agent',
    agent_id: 'canonical-access-agent',
})), {
    sessionId: 'a2a-chat-session',
    kind: 'session',
    agentId: 'canonical-access-agent',
    intent: 'a2a',
});

assert.equal(
    resolveProjectSessionRoute({
        id: 'git-commit-hash',
        metadata: { run_id: 'run-domain-id' },
    }),
    null,
    'Git and audit entities without an exact conversation anchor must not open an unrelated session',
);

console.log('project exact-session routing tests passed');
