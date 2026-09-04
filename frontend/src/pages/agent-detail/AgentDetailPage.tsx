import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
    IconAlertTriangle,
    IconSettings,
} from '@tabler/icons-react';

import type { SidePanelTab } from '../../components/AgentSidePanel';
import { useDialog } from '../../components/Dialog/DialogProvider';
import { useToast } from '../../components/Toast/ToastProvider';
import { useOnboardingKickoff, type OnboardingKickoffRequest } from '../../hooks/useOnboardingKickoff';
import { useUnsavedChangesGuard } from '../../hooks/useUnsavedChangesGuard';
import { agentApi } from '../../services/api';
import { useAuthStore } from '../../stores';
import { parseChatSessionId, writeChatSessionIdToHref } from '../../utils/chatUrlParams';

import AgentDetailPageContent from './components/AgentDetailPageContent';
import { useAgentDetailChatController } from './hooks/useAgentDetailChatController';
import { useAgentDetailResources } from './hooks/useAgentDetailResources';
import { useAgentDetailRoute } from './hooks/useAgentDetailRoute';
import './AccessPermissionsPanel.css';

function AgentDetailPageLoaded({ id, agent }: { id: string; agent: any }) {
    const { t, i18n } = useTranslation();
    const dialog = useDialog();
    const toast = useToast();
    const navigate = useNavigate();
    const location = useLocation();
    const queryClient = useQueryClient();
    const currentUser = useAuthStore((state) => state.user);
    const [sceneConfigDirty, setSceneConfigDirty] = useState(false);
    const {
        activeTab,
        isChatRoute,
        isSettingsRoute,
        setActiveTab,
    } = useAgentDetailRoute({ agentId: id });
    useUnsavedChangesGuard(
        activeTab === 'scenes' && sceneConfigDirty,
        '当前有未保存的编辑内容，继续操作将丢失这些修改。是否继续？',
    );

    useEffect(() => {
        if (
            activeTab === 'scenes'
            && (agent.access_level !== 'manage' || !agent.scene_config_enabled)
        ) {
            setActiveTab('tools');
        }
    }, [activeTab, agent, setActiveTab]);

    const skipNextSessionUrlRestoreRef = useRef(false);
    const requestedSessionId = useMemo(
        () => parseChatSessionId(new URLSearchParams(location.search).get('session_id')),
        [location.search],
    );
    const writeSessionIdToUrl = useCallback((sessionId: string | null | undefined) => {
        const nextHref = writeChatSessionIdToHref(window.location.href, sessionId);
        const currentHref = `${window.location.pathname}${window.location.search}${window.location.hash}`;
        if (nextHref === currentHref) return false;
        navigate(nextHref, { replace: true });
        return true;
    }, [navigate]);

    const [overrideModelId, setOverrideModelId] = useState<string | null>(null);
    const [reasoningEffortOverride, setReasoningEffortOverride] = useState<string>('');
    useEffect(() => {
        if (agent.primary_model_id && agent.primary_model_id !== overrideModelId) {
            setOverrideModelId(agent.primary_model_id);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [agent?.primary_model_id]);
    const handleModelChange = useCallback((newModelId: string | null) => {
        setOverrideModelId(newModelId);
    }, []);

    const onboardingRequestsRef = useRef<Record<string, OnboardingKickoffRequest>>({});
    const [onboardingKickoffRequest, setOnboardingKickoffRequest] = useState<OnboardingKickoffRequest | null>(null);
    const [livePanelVisible, setLivePanelVisible] = useState(false);
    const [sidePanelTab, setSidePanelTab] = useState<SidePanelTab>('workspace');
    const awarePanelVisible = activeTab === 'chat' && livePanelVisible && sidePanelTab === 'aware';
    const awareDataActive = activeTab === 'aware' || awarePanelVisible;

    const resources = useAgentDetailResources({
        id,
        agent,
        activeTab,
        awareDataActive,
        currentUser,
        queryClient,
        toast,
        t,
        i18n,
        location,
        navigate,
        overrideModelId,
        noModelIcon: <IconAlertTriangle size={20} stroke={1.8} />,
        settingsIcon: <IconSettings size={15} stroke={1.75} />,
    });

    const chat = useAgentDetailChatController({
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
    });

    const handleOnboardingStart = useCallback(() => {
        chat.setIsWaiting(true);
        chat.setIsStreaming(false);
    }, [chat]);

    useOnboardingKickoff({
        request: onboardingKickoffRequest,
        activeSessionId: chat.activeSession?.id,
        effectiveModelId: resources.effectiveChatModelId,
        enabled: chat.wsConnected && !resources.llmModelsLoading && resources.effectiveModelReady,
        onStart: handleOnboardingStart,
    });

    return (
        <AgentDetailPageContent
            t={t}
            i18n={i18n}
            dialog={dialog}
            toast={toast}
            id={id}
            agent={agent}
            activeTab={activeTab}
            isChatRoute={isChatRoute}
            isSettingsRoute={isSettingsRoute}
            location={location}
            navigate={navigate}
            queryClient={queryClient}
            setActiveTab={setActiveTab}
            setSceneConfigDirty={setSceneConfigDirty}
            overrideModelId={overrideModelId}
            handleModelChange={handleModelChange}
            reasoningEffortOverride={reasoningEffortOverride}
            setReasoningEffortOverride={setReasoningEffortOverride}
            currentUser={currentUser}
            {...resources}
            {...chat}
        />
    );
}

export default function AgentDetailPage() {
    const { t } = useTranslation();
    const { id } = useParams<{ id: string }>();
    const { data: agent, isLoading } = useQuery({
        queryKey: ['agent', id],
        queryFn: () => agentApi.get(id!),
        enabled: !!id,
    });

    if (isLoading || !agent || !id) {
        return <div style={{ padding: '40px', color: 'var(--text-tertiary)' }}>{t('common.loading')}</div>;
    }

    return <AgentDetailPageLoaded id={id} agent={agent} />;
}
