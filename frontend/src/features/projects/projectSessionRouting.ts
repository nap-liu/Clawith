export type ProjectSessionRecord = Record<string, unknown>;
export type ProjectSessionIntent = 'auto' | 'a2a' | 'run' | 'group';
export type ProjectSessionRoute = {
    sessionId: string;
    kind: 'group' | 'session';
    agentId: string;
    intent: Exclude<ProjectSessionIntent, 'auto'> | 'work';
    /** Durable ChatMessage id that identifies the exact turn behind the project record. */
    anchorMessageId?: string;
};

const PROJECT_MESSAGE_ANCHOR_KEYS = [
    'turn_anchor_id',
    'subagent_turn_anchor_id',
    'origin_turn_anchor_id',
    'anchor_message_id',
    'message_anchor_id',
    'source_message_id',
    'request_message_id',
    'group_message_id',
] as const;

function routeAnchor(records: ProjectSessionRecord[]): Pick<ProjectSessionRoute, 'anchorMessageId'> | Record<string, never> {
    const anchorMessageId = closestProjectTraceValue(records, ...PROJECT_MESSAGE_ANCHOR_KEYS);
    return anchorMessageId ? { anchorMessageId } : {};
}

function directText(source: ProjectSessionRecord, ...keys: string[]): string {
    for (const key of keys) {
        const value = source[key];
        if (typeof value === 'string' || typeof value === 'number') return String(value);
    }
    return '';
}

function parseRecord(value: unknown): ProjectSessionRecord | null {
    if (value && typeof value === 'object' && !Array.isArray(value)) return value as ProjectSessionRecord;
    if (typeof value !== 'string') return null;
    const source = value.trim();
    const start = source.indexOf('{');
    if (start < 0) return null;
    try {
        const parsed = JSON.parse(source.slice(start));
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as ProjectSessionRecord : null;
    } catch {
        return null;
    }
}

export function projectTraceRecords(source: ProjectSessionRecord): ProjectSessionRecord[] {
    const records: ProjectSessionRecord[] = [];
    const seen = new Set<ProjectSessionRecord>();
    const visit = (value: unknown, depth: number) => {
        if (depth > 3) return;
        const record = parseRecord(value);
        if (!record || seen.has(record)) return;
        seen.add(record);
        records.push(record);
        ['event_metadata', 'metadata', 'input', 'dispatch', 'output', 'runtime', 'result', 'delivery_result', 'message_meta', 'session', 'subagent_run', 'member_snapshot', 'associations', 'relations', 'trace'].forEach((key) => visit(record[key], depth + 1));
        ['subagent_runs', 'related_runs', 'runs', 'sessions', 'related_sessions', 'events', 'related_events', 'commits', 'related_commits', 'files', 'related_files', 'evidence'].forEach((key) => {
            if (Array.isArray(record[key])) (record[key] as unknown[]).forEach((item) => visit(item, depth + 1));
        });
    };
    visit(source, 0);
    return records;
}

export function projectTraceValue(records: ProjectSessionRecord[], ...keys: string[]): string {
    for (const key of keys) {
        for (const record of records) {
            const value = record[key];
            if (typeof value === 'string' || typeof value === 'number') return String(value);
        }
    }
    return '';
}

export function closestProjectTraceValue(records: ProjectSessionRecord[], ...keys: string[]): string {
    for (const record of records) {
        for (const key of keys) {
            const value = record[key];
            if (typeof value === 'string' || typeof value === 'number') return String(value);
        }
    }
    return '';
}

export function inferProjectSessionIntent(source: ProjectSessionRecord): ProjectSessionIntent {
    const records = projectTraceRecords(source);
    const eventType = closestProjectTraceValue(records, 'event_type', 'type').toLowerCase();
    const triggerType = closestProjectTraceValue(records, 'trigger_type').toLowerCase();
    const sourceChannel = closestProjectTraceValue(records, 'source_channel').toLowerCase();
    const explicitMode = closestProjectTraceValue(records, 'session_mode', 'mode', 'kind').toLowerCase();
    if (eventType.startsWith('group.') || sourceChannel === 'project' || explicitMode === 'group') return 'group';
    if (eventType.startsWith('a2a.') || triggerType === 'a2a' || sourceChannel === 'agent') return 'a2a';
    if (sourceChannel === 'subagent' || closestProjectTraceValue(records, 'subagent_session_id', 'subagent_run_id', 'child_session_id')) return 'run';
    return 'auto';
}

/**
 * Resolve the exact persisted conversation behind a project record.
 *
 * ProjectRun, ProjectEvent, member snapshot and Git commit ids are domain ids,
 * not chat session ids. Only a real ChatSession (`source_channel` present on
 * the record) may use its own `id`; every other entity must provide an
 * explicit session anchor in its trace metadata.
 */
export function resolveProjectSessionRoute(
    source: ProjectSessionRecord,
    requestedIntent: ProjectSessionIntent = 'auto',
): ProjectSessionRoute | null {
    const records = projectTraceRecords(source);
    const directSourceChannel = directText(source, 'source_channel').toLowerCase();
    const directSessionId = directText(source, 'id', 'session_id', 'conversation_id');
    const intent = requestedIntent === 'auto' ? inferProjectSessionIntent(source) : requestedIntent;

    if (directSessionId && ['project', 'agent', 'subagent'].includes(directSourceChannel)) {
        const kind = directSourceChannel === 'project' ? 'group' : 'session';
        return {
            sessionId: directSessionId,
            kind,
            agentId: directText(source, 'agent_id', 'access_agent_id', 'session_agent_id'),
            intent: directSourceChannel === 'project' ? 'group' : directSourceChannel === 'agent' ? 'a2a' : 'run',
            ...routeAnchor(records),
        };
    }

    if (intent === 'group') {
        const sessionId = closestProjectTraceValue(records, 'group_session_id');
        return sessionId ? {
            sessionId,
            kind: 'group',
            agentId: closestProjectTraceValue(records, 'access_agent_id', 'session_agent_id', 'leader_agent_id', 'agent_id'),
            intent: 'group',
            ...routeAnchor(records),
        } : null;
    }

    if (intent === 'a2a') {
        const sessionId = closestProjectTraceValue(records, 'a2a_session_id', 'session_id', 'conversation_id');
        const fromAgentId = closestProjectTraceValue(records, 'from_agent_id', 'source_agent_id');
        const toAgentId = closestProjectTraceValue(records, 'to_agent_id', 'target_agent_id');
        const fallbackOwner = [fromAgentId, toAgentId].filter(Boolean).sort()[0] || '';
        return sessionId ? {
            sessionId,
            kind: 'session',
            agentId: closestProjectTraceValue(records, 'session_agent_id', 'session_access_agent_id', 'access_agent_id', 'execution_agent_id', 'agent_id', 'to_agent_id') || fallbackOwner,
            intent: 'a2a',
            ...routeAnchor(records),
        } : null;
    }

    const childSessionId = closestProjectTraceValue(records, 'subagent_session_id', 'subagent_run_id', 'child_session_id');
    if (childSessionId) {
        return {
            sessionId: childSessionId,
            kind: 'session',
            agentId: closestProjectTraceValue(records, 'execution_agent_id', 'subagent_agent_id', 'agent_id', 'to_agent_id', 'assignee_agent_id'),
            intent: 'run',
            ...routeAnchor(records),
        };
    }
    if (requestedIntent === 'run') return null;

    const sessionId = closestProjectTraceValue(records, 'session_id', 'conversation_id');
    return sessionId ? {
        sessionId,
        kind: 'session',
        agentId: closestProjectTraceValue(records, 'session_agent_id', 'session_access_agent_id', 'access_agent_id', 'execution_agent_id', 'subagent_agent_id', 'agent_id', 'actor_agent_id', 'to_agent_id', 'from_agent_id'),
        intent: 'work',
        ...routeAnchor(records),
    } : null;
}
