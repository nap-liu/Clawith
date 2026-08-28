export const CONVERSATION_HISTORY_TURN_PAGE_SIZE = 20;
export const CONVERSATION_HISTORY_RECONCILE_TURN_PAGE_SIZE = 2;

export function createConversationHistoryPageParams(
    before?: string | null,
    turnLimit = CONVERSATION_HISTORY_TURN_PAGE_SIZE,
) {
    const params = new URLSearchParams({
        turn_limit: String(turnLimit),
    });
    if (before) params.set('before', before);
    return params;
}

export function resolveConversationHistoryHasMore(header: string | null) {
    return header === 'true';
}
