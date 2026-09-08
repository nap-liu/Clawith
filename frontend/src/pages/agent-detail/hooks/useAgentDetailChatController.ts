import { useRef } from 'react';
import { pendingPcRouteRecoveryRuntimeKeys, workspaceFileName } from '../shared';
import { useAgentDetailChatState } from './useAgentDetailChatState';
import { useAgentDetailResumeComposer } from './useAgentDetailResumeComposer';
import { useAgentDetailSessionSelection } from './useAgentDetailSessionSelection';
import { useAgentDetailSocketMessages } from './useAgentDetailSocketMessages';

export function useAgentDetailChatController({
    id,
    agent,
    activeTab,
    currentUser,
    dialog,
    livePanelVisible,
    setLivePanelVisible,
    sidePanelTab,
    setSidePanelTab,
    requestedSessionId,
    skipNextSessionUrlRestoreRef,
    writeSessionIdToUrl,
    queryClient,
    i18n,
    toast,
    t,
    onboardingRequestsRef,
    setOnboardingKickoffRequest,
    resources,
    reasoningEffortOverride,
}: any) {
    const parseChatMsgRef = useRef<any>(() => undefined);
    const scheduleComposerFocusRef = useRef<() => void>(() => undefined);
    const dispatchChatMessageRef = useRef<any>(() => undefined);
    const handleWorkspacePathDeletedRef = useRef<any>(() => undefined);
    const historyAutoLoadCursorRef = useRef<string | null>(null);
    const discardChatStreamBatchRef = useRef<() => void>(() => undefined);
    const discardMonitorStreamBatchRef = useRef<() => void>(() => undefined);

    const chat = useAgentDetailChatState({
        id,
        agent,
        currentUser,
        setOnboardingKickoffRequest,
        onboardingRequestsRef,
        skipNextSessionUrlRestoreRef,
        writeSessionIdToUrl,
        livePanelVisible,
        setLivePanelVisible,
        sidePanelTab,
        setSidePanelTab,
    });
    discardChatStreamBatchRef.current = chat.discardChatStreamBatch;

    const helpers = {
        parseChatMsgRef,
        scheduleComposerFocusRef,
        dispatchChatMessageRef,
        handleWorkspacePathDeletedRef,
        historyAutoLoadCursorRef,
        discardChatStreamBatchRef,
        discardMonitorStreamBatchRef,
        onboardingRequestsRef,
        pendingPcRouteRecoveryRuntimeKeys,
        workspaceFileName,
        wsRef: chat.wsRef,
    };

    const sessionSelection = useAgentDetailSessionSelection({
        id,
        activeTab,
        currentUser,
        requestedSessionId,
        skipNextSessionUrlRestoreRef,
        writeSessionIdToUrl,
        t,
        toast,
        dialog,
        chat,
        helpers,
    });

    const socketMessages = useAgentDetailSocketMessages({
        id,
        i18n,
        queryClient,
        chat,
        resources,
        sessionSelection,
        helpers,
    });

    const resumeComposer = useAgentDetailResumeComposer({
        id,
        agent,
        activeTab,
        token: chat.token,
        effectiveChatModelId: resources.effectiveChatModelId,
        reasoningEffortOverride,
        showNoModelState: resources.showNoModelState,
        toast,
        t,
        chat,
        sessionSelection,
        socketMessages,
        helpers,
    });

    const onAdminTabOthers = () => {
        chat.onAdminTabOthers();
        if (chat.allSessions.length === 0) void sessionSelection.fetchAllSessions();
    };

    return {
        ...chat,
        ...sessionSelection,
        ...socketMessages,
        ...resumeComposer,
        onAdminTabOthers,
    };
}
