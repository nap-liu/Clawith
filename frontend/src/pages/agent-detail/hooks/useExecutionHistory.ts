import { useCallback, useEffect, useRef, useState } from 'react';
import { chatSessionApi } from '../../../services/api';

export function useExecutionHistory(agentId?: string) {
    const [messages, setMessages] = useState<Record<string, any[]>>({});
    const cache = useRef(messages);
    cache.current = messages;
    const pending = useRef(new Map<string, Promise<any[]>>());
    const latestRequest = useRef(new Map<string, string>());
    const currentAgentId = useRef(agentId);
    currentAgentId.current = agentId;

    useEffect(() => {
        cache.current = {};
        setMessages({});
    }, [agentId]);

    const loadMessages = useCallback((sessionId: string, revision?: string): Promise<any[]> => {
        if (!agentId) return Promise.resolve([]);
        const key = `${agentId}:${sessionId}:${revision || ''}`;
        latestRequest.current.set(sessionId, key);
        const existing = pending.current.get(key);
        if (existing) return existing;
        if (!revision && cache.current[sessionId]) return Promise.resolve(cache.current[sessionId]);
        const request = chatSessionApi.allMessages(agentId, sessionId)
            .then((rows) => {
                if (currentAgentId.current === agentId && latestRequest.current.get(sessionId) === key) {
                    cache.current = { ...cache.current, [sessionId]: rows };
                    setMessages(cache.current);
                }
                return rows;
            })
            .finally(() => pending.current.delete(key));
        pending.current.set(key, request);
        return request;
    }, [agentId]);

    return { messages, setMessages, loadMessages };
}
