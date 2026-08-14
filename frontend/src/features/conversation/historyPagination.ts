export const CONVERSATION_HISTORY_TURN_PAGE_SIZE = 20;

export function createConversationHistoryPageParams(before?: string | null) {
    const params = new URLSearchParams({
        turn_limit: String(CONVERSATION_HISTORY_TURN_PAGE_SIZE),
    });
    if (before) params.set('before', before);
    return params;
}

export function resolveConversationHistoryHasMore(header: string | null) {
    return header === 'true';
}
