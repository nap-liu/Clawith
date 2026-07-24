import { useEffect, useRef } from 'react';

export type OnboardingKickoffRequest = {
    sessionId: string;
    required: boolean;
    socket: WebSocket;
};

type UseOnboardingKickoffOptions = {
    request: OnboardingKickoffRequest | null;
    activeSessionId: string | null | undefined;
    effectiveModelId: string | null | undefined;
    enabled?: boolean;
    onStart: () => void;
};

/**
 * Shared Web/H5 client for the server-authorized onboarding greeting.
 *
 * The backend owns eligibility and idempotency. A reconnect supplies a new
 * WebSocket/request object and may retry safely; the database claim ensures
 * that only one greeting turn runs across tabs and instances.
 */
export function useOnboardingKickoff({
    request,
    activeSessionId,
    effectiveModelId,
    enabled = true,
    onStart,
}: UseOnboardingKickoffOptions) {
    const attemptedSocketsRef = useRef<WeakSet<WebSocket>>(new WeakSet());

    useEffect(() => {
        if (!enabled || !request?.required || !effectiveModelId) return;
        if (request.sessionId !== String(activeSessionId || '')) return;
        if (request.socket.readyState !== WebSocket.OPEN) return;
        if (attemptedSocketsRef.current.has(request.socket)) return;

        attemptedSocketsRef.current.add(request.socket);
        onStart();
        request.socket.send(JSON.stringify({
            content: '',
            kind: 'onboarding_trigger',
            model_id: effectiveModelId,
        }));
    }, [activeSessionId, effectiveModelId, enabled, onStart, request]);
}
