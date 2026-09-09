import type React from 'react';
import { useCallback, useMemo } from 'react';
import {
    chatSessionApi,
    uploadFileWithProgress,
} from '../../../services/api';
import {
    buildChatAttachmentPayload,
    resolveEffectiveChatModelId,
    type ChatAttachedFile,
} from '../../../utils/chatAttachments';
import { writeChatSessionIdToHref } from '../../../utils/chatUrlParams';
import { useOnboardingKickoff } from '../../../hooks/useOnboardingKickoff';
import {
    IDLE_CONVERSATION_TURN,
} from '../../../features/conversation/core/conversationTurnLifecycle';
import { makeId, normalizeH5SessionSummary, type H5UploadDraft } from './model';
import type { useH5AgentChatState } from './useH5AgentChatState';
import type { useH5AgentChatHistory } from './useH5AgentChatHistory';
import type { useH5AgentChatSocket } from './useH5AgentChatSocket';
import { useH5HostContextSend } from './useH5HostContextSend';

export function useH5AgentChatComposer(
    state: ReturnType<typeof useH5AgentChatState>,
    history: ReturnType<typeof useH5AgentChatHistory>,
    socket: ReturnType<typeof useH5AgentChatSocket>,
) {
    const {
        uploadAbortRef,
        setUploadDrafts,
        setSessionsPanelOpen,
        recoveryPollingNeededRef,
        resumeCleanupNeededRef,
        resumeReconcilePromiseRef,
        resumeEventGateRef,
        reconnectTimerRef,
        resumeReconnectTimerRef,
        finishResumeEventGate,
        closeCurrentSocket,
        discardStreamBatch,
        isWaiting,
        isStreaming,
        isStopping,
        setSessionsError,
        setIsSwitchingSession,
        setIsWaiting,
        setIsStreaming,
        setIsStopping,
        setUploadError,
        setAttachedFiles,
        sessionIdRef,
        setSessionId,
        setMessages,
        skipNextConnectedHistoryRef,
        agentId,
        isStartingNew,
        isSwitchingSession,
        setIsStartingNew,
        channel,
        setSessions,
        wsRef,
        turnRuntimeBySessionRef,
        generationActiveRef,
        confirmationPending,
        attachedFiles,
        uploadDrafts,
        setInput,
        tenantDefaultModelId,
        llmModels,
        agent,
        onboardingKickoffRequest,
        sessionId,
        connectionStatus,
        isReadOnly,
        messageDispatchLockedRef,
        messageRuntimeBlockedRef,
        resumeAutoFollowRef,
        input,
        speech,
    } = state;
    const {
        cancelRecoveryPolling,
        loadHistory,
        loadSessions,
    } = history;
    const { openSocket } = socket;

    const clearUploadDrafts = useCallback((abortUploads = false) => {
        if (abortUploads) {
            uploadAbortRef.current.forEach((abort) => abort());
            uploadAbortRef.current.clear();
        }
        setUploadDrafts((prev) => {
            prev.forEach((draft) => {
                if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
            });
            return [];
        });
    }, []);

    const openSessionPanel = useCallback(() => {
        setSessionsPanelOpen(true);
        loadSessions();
    }, [loadSessions]);

    const resetResumeRecoveryForSessionChange = useCallback(() => {
        recoveryPollingNeededRef.current = false;
        resumeCleanupNeededRef.current = false;
        cancelRecoveryPolling();
        resumeReconcilePromiseRef.current = null;
        const gate = resumeEventGateRef.current;
        if (gate) finishResumeEventGate(gate, false);
        if (reconnectTimerRef.current) {
            window.clearTimeout(reconnectTimerRef.current);
            reconnectTimerRef.current = null;
        }
        if (resumeReconnectTimerRef.current) {
            window.clearTimeout(resumeReconnectTimerRef.current);
            resumeReconnectTimerRef.current = null;
        }
    }, [cancelRecoveryPolling, finishResumeEventGate]);

    const activateSession = useCallback(async (nextSessionId: string) => {
        if (!nextSessionId) return;
        if (isWaiting || isStreaming || isStopping) {
            setSessionsError('当前回复进行中，请先终止后再切换会话');
            return;
        }
        if (nextSessionId === sessionIdRef.current) {
            setSessionsPanelOpen(false);
            return;
        }

        state.hostContext.cancelPending();
        setIsSwitchingSession(true);
        setSessionsPanelOpen(false);
        setIsWaiting(false);
        setIsStreaming(false);
        setIsStopping(false);
        setUploadError('');
        setAttachedFiles([]);
        discardStreamBatch();
        resetResumeRecoveryForSessionChange();
        clearUploadDrafts(true);
        sessionIdRef.current = nextSessionId;
        setSessionId(nextSessionId);
        setMessages([]);
        window.history.replaceState({}, '', writeChatSessionIdToHref(window.location.href, nextSessionId));

        closeCurrentSocket();
        if (await loadHistory(nextSessionId)) skipNextConnectedHistoryRef.current = nextSessionId;
        window.setTimeout(() => {
            openSocket(nextSessionId);
            setIsSwitchingSession(false);
        }, 0);
    }, [clearUploadDrafts, closeCurrentSocket, discardStreamBatch, isStopping, isStreaming, isWaiting, loadHistory, openSocket, resetResumeRecoveryForSessionChange, state.hostContext]);

    const startNewSession = useCallback(async () => {
        if (!agentId || isStartingNew) return;
        state.hostContext.cancelPending();
        setIsStartingNew(true);
        setIsWaiting(false);
        setIsStreaming(false);
        setIsStopping(false);
        setUploadError('');
        setAttachedFiles([]);
        discardStreamBatch();
        clearUploadDrafts(true);
        try {
            const session = await chatSessionApi.create(agentId, { source_channel: channel });
            const nextSessionId = String(session.id);
            resetResumeRecoveryForSessionChange();
            const summary = normalizeH5SessionSummary(session);
            sessionIdRef.current = nextSessionId;
            setSessionId(nextSessionId);
            setMessages([]);
            setSessionsPanelOpen(false);
            window.history.replaceState({}, '', writeChatSessionIdToHref(window.location.href, nextSessionId));
            if (summary) {
                setSessions((prev) => [
                    summary,
                    ...prev
                        .filter((item) => item.id !== summary.id)
                        .map((item) => (
                            item.source_channel === summary.source_channel
                                ? { ...item, is_primary: false }
                                : item
                        )),
                ]);
            }
            closeCurrentSocket();
            window.setTimeout(() => {
                openSocket(nextSessionId);
            }, 0);
        } catch (error: any) {
            setMessages((prev) => [...prev, {
                id: makeId(),
                role: 'system',
                content: error?.message || '无法开启新会话',
                created_at: new Date().toISOString(),
            }]);
        } finally {
            setIsStartingNew(false);
        }
    }, [agentId, channel, clearUploadDrafts, closeCurrentSocket, discardStreamBatch, isStartingNew, openSocket, resetResumeRecoveryForSessionChange, state.hostContext]);

    const stopGeneration = useCallback(() => {
        state.hostContext.stopSession(sessionIdRef.current);
        const ws = wsRef.current;
        if (ws?.readyState === WebSocket.OPEN) {
            const runtimeSessionId = String(sessionIdRef.current || '');
            const snapshot = (
                turnRuntimeBySessionRef.current[runtimeSessionId] || IDLE_CONVERSATION_TURN
            ).snapshot;
            ws.send(JSON.stringify({
                type: 'abort',
                turn_anchor_id: snapshot.turnAnchorId,
                generation: snapshot.generation,
            }));
            generationActiveRef.current = true;
            setIsStopping(true);
            return;
        }
        generationActiveRef.current = false;
        setIsWaiting(false);
        setIsStreaming(false);
        setIsStopping(false);
    }, [state.hostContext]);

    const removeAttachedFile = useCallback((index: number) => {
        setAttachedFiles((prev) => prev.filter((_, i) => i !== index));
    }, []);

    const cancelUploadDraft = useCallback((draft: H5UploadDraft) => {
        uploadAbortRef.current.get(draft.id)?.();
        uploadAbortRef.current.delete(draft.id);
        if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
        setUploadDrafts((prev) => prev.filter((item) => item.id !== draft.id));
    }, []);

    const handleH5Files = useCallback(async (files: File[]) => {
        if (confirmationPending || !agentId || !files.length) return;
        setUploadError('');
        const availableSlots = Math.max(0, 10 - attachedFiles.length - uploadDrafts.length);
        const allowedFiles = files.slice(0, availableSlots);
        if (!allowedFiles.length) {
            setUploadError('最多可附加 10 个文件');
            return;
        }
        if (allowedFiles.length < files.length) {
            setUploadError(`最多可附加 10 个文件，已选择前 ${allowedFiles.length} 个`);
        }

        const baseTime = Date.now();
        const newDrafts = allowedFiles.map((file, index) => ({
            id: `h5-up-${baseTime}-${index}-${file.name}`,
            name: file.name,
            percent: 0,
            previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
            sizeBytes: file.size,
        }));
        setUploadDrafts((prev) => [...prev, ...newDrafts]);

        const runOne = async (file: File, draft: H5UploadDraft) => {
            const { promise, abort } = uploadFileWithProgress(
                '/chat/upload',
                file,
                (pct) => {
                    setUploadDrafts((prev) =>
                        prev.map((item) => item.id === draft.id ? { ...item, percent: pct >= 101 ? 100 : pct } : item),
                    );
                },
                { agent_id: agentId },
                600_000,
            );
            uploadAbortRef.current.set(draft.id, abort);
            try {
                const data = await promise;
                const uploadedName = data.saved_filename || data.filename || file.name;
                setAttachedFiles((prev) => [...prev, {
                    name: uploadedName,
                    text: data.extracted_text || '',
                    path: data.workspace_path,
                    imageUrl: data.image_data_url || undefined,
                    mimeType: file.type || undefined,
                    sizeBytes: data.size ?? file.size,
                }].slice(0, 10));
            } catch (error: any) {
                if (error?.message !== 'Upload cancelled') {
                    setUploadError(error?.message || '文件上传失败');
                }
            } finally {
                uploadAbortRef.current.delete(draft.id);
                if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
                setUploadDrafts((prev) => prev.filter((item) => item.id !== draft.id));
            }
        };

        await Promise.all(allowedFiles.map((file, index) => runOne(file, newDrafts[index])));
    }, [agentId, attachedFiles.length, confirmationPending, uploadDrafts.length]);

    const handleFileInputChange = useCallback((event: React.ChangeEvent<HTMLInputElement>) => {
        const files = Array.from(event.target.files || []);
        event.target.value = '';
        void handleH5Files(files);
    }, [handleH5Files]);

    const handlePaste = useCallback((event: React.ClipboardEvent<HTMLTextAreaElement>) => {
        const items = event.clipboardData?.items;
        if (!items?.length) return;
        const imageFiles: File[] = [];
        for (let i = 0; i < items.length; i += 1) {
            const item = items[i];
            if (!item.type.startsWith('image/')) continue;
            const blob = item.getAsFile();
            if (!blob) continue;
            const ext = blob.type.split('/')[1] || 'png';
            imageFiles.push(new File([blob], `paste-${Date.now()}-${i}.${ext}`, { type: blob.type }));
        }
        if (!imageFiles.length) return;
        event.preventDefault();
        void handleH5Files(imageFiles);
    }, [handleH5Files]);

    const effectiveModelId = useMemo(() => resolveEffectiveChatModelId({
        preferredModelId: agent?.primary_model_id || null,
        tenantDefaultModelId,
        models: llmModels,
    }), [agent?.primary_model_id, llmModels, tenantDefaultModelId]);
    const dispatchHostMessage = useH5HostContextSend(state, effectiveModelId);

    const handleOnboardingStart = useCallback(() => {
        generationActiveRef.current = true;
        setIsWaiting(true);
        setIsStreaming(false);
    }, []);

    useOnboardingKickoff({
        request: onboardingKickoffRequest,
        activeSessionId: sessionId,
        effectiveModelId,
        enabled: connectionStatus === 'connected' && !state.hostContext.enabled,
        onStart: handleOnboardingStart,
    });

    const dispatchMessage = useCallback(async (
        rawContent: string,
        files: ChatAttachedFile[],
        consumeComposer: boolean,
    ) => {
        const content = rawContent.trim();
        if (!content && files.length === 0) return;
        if (isReadOnly) return;

        // Session-control commands are control-plane operations, not dialogue.
        // They stay available even while a confirmation card is pending.
        if (files.length === 0 && (content === '/new' || content === '/reset')) {
            state.hostContext.cancelPending();
            messageDispatchLockedRef.current = true;
            if (consumeComposer) setInput('');
            try {
                await startNewSession();
            } finally {
                messageDispatchLockedRef.current = false;
            }
            return;
        }

        const isContinueCommand = files.length === 0 && content.toLowerCase() === '/continue';
        if (state.hostContext.enabled && !isContinueCommand) {
            return dispatchHostMessage(rawContent, files, consumeComposer);
        }

        if (
            messageDispatchLockedRef.current
            || messageRuntimeBlockedRef.current
            || confirmationPending
        ) return;
        messageDispatchLockedRef.current = true;

        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            setMessages((prev) => [...prev, {
                id: makeId(),
                role: 'system',
                content: '连接未就绪',
                created_at: new Date().toISOString(),
            }]);
            openSocket(sessionIdRef.current);
            messageDispatchLockedRef.current = false;
            return;
        }

        const payload = buildChatAttachmentPayload({
            input: content,
            attachments: files,
        });
        const messageId = makeId();
        resumeAutoFollowRef.current();
        setMessages((prev) => [...prev, {
            id: messageId,
            role: 'user',
            content: payload.displayContent,
            display_content: payload.displayContent,
            fileName: payload.fileName,
            imageUrl: payload.imageUrl,
            previewImages: payload.previewImages,
            attachments: payload.attachments,
            created_at: new Date().toISOString(),
        }]);
        if (consumeComposer) {
            setInput('');
            setAttachedFiles([]);
        }
        if (!generationActiveRef.current) {
            generationActiveRef.current = true;
            setIsWaiting(true);
        }
        ws.send(JSON.stringify({
            message_id: messageId,
            content: payload.contentForLLM,
            display_content: payload.displayContent,
            file_name: payload.fileName,
            attachments: payload.attachments,
            model_id: effectiveModelId,
        }));
        messageDispatchLockedRef.current = false;
    }, [confirmationPending, dispatchHostMessage, effectiveModelId, isReadOnly, isStartingNew, isStreaming, isStopping, isSwitchingSession, isWaiting, openSocket, speech.isActive, startNewSession, uploadDrafts.length, state.hostContext]);

    const sendMessage = useCallback(
        () => dispatchMessage(input, attachedFiles, true),
        [attachedFiles, dispatchMessage, input],
    );

    const handleInputKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            if (!event.nativeEvent.isComposing && !isStopping && uploadDrafts.length === 0 && !speech.isActive) {
                sendMessage();
            }
        }
    };

    return {
        activateSession,
        cancelUploadDraft,
        clearUploadDrafts,
        dispatchMessage,
        effectiveModelId,
        handleFileInputChange,
        handleH5Files,
        handleInputKeyDown,
        handlePaste,
        openSessionPanel,
        removeAttachedFile,
        sendMessage,
        startNewSession,
        stopGeneration,
    };
}
