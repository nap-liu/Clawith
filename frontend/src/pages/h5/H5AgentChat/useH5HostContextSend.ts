import { useCallback, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { buildChatAttachmentPayload, type ChatAttachedFile } from '../../../utils/chatAttachments';
import type { HostContextDraft } from '../../../utils/h5HostContext';
import { makeId } from './model';
import type { useH5AgentChatState } from './useH5AgentChatState';

export function useH5HostContextSend(
    state: ReturnType<typeof useH5AgentChatState>,
    effectiveModelId: string | null | undefined,
) {
    const { t } = useTranslation();
    const { hostContext } = state;
    const current = useRef(state);
    current.current = state;
    const composerKey = JSON.stringify([state.input, state.attachedFiles]);
    useEffect(() => {
        hostContext.cancelChangedDraft(composerKey, state.sessionId);
    }, [composerKey, hostContext, state.sessionId]);
    useEffect(() => () => hostContext.cancelPending(), [hostContext, state.token]);
    useEffect(() => {
        hostContext.onRejected = (draft) => {
            const latest = current.current;
            if (latest.sessionIdRef.current !== draft.sessionId || latest.input || latest.attachedFiles.length) return false;
            latest.setInput(draft.rawContent);
            latest.setAttachedFiles(draft.files);
            return true;
        };
        return () => { hostContext.onRejected = null; };
    }, [hostContext]);
    useEffect(() => {
        const latest = current.current;
        hostContext.retry(latest.wsRef.current, latest.sessionIdRef.current,
            latest.connectionStatus === 'connected' && !latest.isReadOnly
            && !latest.messageRuntimeBlockedRef.current && !latest.confirmationPending
            && !latest.pageSuspendedRef.current && !document.hidden);
    }, [hostContext, state.connectionStatus, state.confirmationPending, state.isReadOnly,
        state.isStopping, state.isWaiting, state.isStreaming, state.pageActive]);

    return useCallback(async (rawContent: string, files: ChatAttachedFile[], consumeComposer: boolean) => {
        const latest = current.current;
        if (latest.messageDispatchLockedRef.current || latest.messageRuntimeBlockedRef.current
            || latest.confirmationPending || latest.isReadOnly || latest.pageSuspendedRef.current || document.hidden) return;
        const sessionId = latest.sessionIdRef.current;
        const socket = latest.wsRef.current;
        if (!sessionId || !socket || socket.readyState !== WebSocket.OPEN || latest.connectionStatus !== 'connected') {
            latest.setUploadError(t('hostContext.connectionUnavailable'));
            return;
        }
        const payload = buildChatAttachmentPayload({ input: rawContent.trim(), attachments: files });
        const messageId = makeId();
        const candidate: HostContextDraft = {
            key: JSON.stringify([sessionId, rawContent, files, effectiveModelId, consumeComposer]),
            composerKey: JSON.stringify([latest.input, latest.attachedFiles]),
            sessionId, messageId, rawContent, files: structuredClone(files), consumeComposer,
            frame: {
                message_id: messageId, content: payload.contentForLLM, display_content: payload.displayContent,
                file_name: payload.fileName, attachments: payload.attachments, model_id: effectiveModelId,
            },
            preview: {
                id: messageId, role: 'user', content: payload.displayContent, display_content: payload.displayContent,
                fileName: payload.fileName, imageUrl: payload.imageUrl, previewImages: payload.previewImages,
                attachments: payload.attachments, created_at: new Date().toISOString(),
            },
        };
        latest.messageDispatchLockedRef.current = true;
        latest.setUploadError('');
        try {
            const draft = await hostContext.prepare(candidate);
            const active = current.current;
            if (active.hostContext !== hostContext || active.sessionIdRef.current !== draft.sessionId
                || active.wsRef.current !== socket || socket.readyState !== WebSocket.OPEN
                || active.connectionStatus !== 'connected' || active.messageRuntimeBlockedRef.current
                || active.confirmationPending || active.isReadOnly || active.unmountedRef.current
                || active.pageSuspendedRef.current || document.hidden
                || JSON.stringify([active.input, active.attachedFiles]) !== draft.composerKey) {
                throw new DOMException('Aborted', 'AbortError');
            }
            hostContext.send(draft, socket);
            active.resumeAutoFollowRef.current();
            active.setMessages((messages) => messages.some((message) => message.id === draft.messageId)
                ? messages : [...messages, draft.preview]);
            if (consumeComposer) {
                active.setInput('');
                active.setAttachedFiles([]);
            }
            if (!active.generationActiveRef.current) {
                active.generationActiveRef.current = true;
                active.setIsWaiting(true);
            }
        } catch (error) {
            if ((error as Error).name !== 'AbortError' && current.current.hostContext === hostContext) {
                const key = (error as Error).message;
                current.current.setUploadError(t(key === 'hostContext.timeout' ? key : 'hostContext.unavailable'));
            }
        } finally {
            latest.messageDispatchLockedRef.current = false;
        }
    }, [effectiveModelId, hostContext, t]);
}
