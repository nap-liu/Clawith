import { useCallback, useEffect, useLayoutEffect, useMemo } from 'react';
import {
    agentApi,
    authApi,
    enterpriseApi,
    sceneApi,
    tenantApi,
} from '../../../services/api';
import { useAuthStore } from '../../../stores';
import {
    menuVisibleSceneQuickActions,
} from '../../../utils/sceneQuickActions';
import {
    H5_LOGIN_MESSAGES,
    formatH5LoginError,
    isRememberedH5AuthCode,
    rememberH5AuthCode,
} from '../../../utils/h5AuthSession';
import { installThemeController } from '../../../utils/themeMode';
import {
    buildCodeExchangeRedirectUri,
    cleanedOAuthAddress,
    consumedExchangeCodes,
    exchangeCodeOnce,
} from './model';
import type { useH5AgentChatState } from './useH5AgentChatState';

export function useH5AgentChatLifecycle(state: ReturnType<typeof useH5AgentChatState>) {
    const {
        ensureContainerRuntime,
        setContainerRuntime,
        themeMode,
        setResolvedTheme,
        resolvedTheme,
        textareaRef,
        input,
        unmountedRef,
        reconnectTimerRef,
        resumeReconnectTimerRef,
        recoveryPollingGenerationRef,
        recoveryPollTimerRef,
        nativeNavigationFallbackTimerRef,
        uploadAbortRef,
        cancelHistoryLoad,
        discardStreamBatch,
        closeCurrentSocket,
        initialSessionId,
        sessionIdRef,
        setSessionId,
        agentId,
        setAuthStatus,
        setAuthError,
        code,
        provider,
        oauthState,
        channel,
        setAuth,
        token,
        authStatus,
        setAgentError,
        setAgent,
        sceneManifestRequestRef,
        sceneManifestRef,
        sceneManifest,
        setSceneManifest,
        sceneKey,
        pageResumeRevision,
        quickActionsRef,
        setQuickActionsOverflow,
        quickActionsMenuCloseTimerRef,
        setQuickActionsMenuOpen,
        setQuickActionsMenuClosing,
        setQuickActionSearch,
        quickActionsMenuOpen,
        closeQuickActionsMenu,
        confirmationPending,
        setLlmModels,
        setTenantDefaultModelId,
    } = state;

    useEffect(() => {
        document.body.classList.add('h5-chat-active');
        return () => document.body.classList.remove('h5-chat-active');
    }, []);

    useEffect(() => {
        let cancelled = false;
        void ensureContainerRuntime()
            .then((runtime) => {
                if (!cancelled) setContainerRuntime(runtime);
            });
        return () => {
            cancelled = true;
        };
    }, [ensureContainerRuntime]);

    useLayoutEffect(() => installThemeController({
        mode: themeMode,
        onThemeChange: setResolvedTheme,
    }), [themeMode]);

    useLayoutEffect(() => {
        document.body.dataset.h5Theme = resolvedTheme;
        return () => {
            delete document.body.dataset.h5Theme;
        };
    }, [resolvedTheme]);

    useEffect(() => {
        const textarea = textareaRef.current;
        if (!textarea) return;
        textarea.style.height = '0px';
        textarea.style.height = `${Math.min(textarea.scrollHeight, 132)}px`;
    }, [input]);

    useEffect(() => {
        unmountedRef.current = false;
        return () => {
            unmountedRef.current = true;
            if (reconnectTimerRef.current) window.clearTimeout(reconnectTimerRef.current);
            if (resumeReconnectTimerRef.current) window.clearTimeout(resumeReconnectTimerRef.current);
            recoveryPollingGenerationRef.current += 1;
            if (recoveryPollTimerRef.current) window.clearTimeout(recoveryPollTimerRef.current);
            recoveryPollTimerRef.current = null;
            if (nativeNavigationFallbackTimerRef.current) {
                window.clearTimeout(nativeNavigationFallbackTimerRef.current);
            }
            uploadAbortRef.current.forEach((abort) => abort());
            uploadAbortRef.current.clear();
            cancelHistoryLoad();
            discardStreamBatch();
            closeCurrentSocket();
        };
    }, [cancelHistoryLoad, closeCurrentSocket, discardStreamBatch]);

    useEffect(() => {
        if (!initialSessionId || sessionIdRef.current) return;
        sessionIdRef.current = initialSessionId;
        setSessionId(initialSessionId);
    }, [initialSessionId]);

    useEffect(() => {
        if (!agentId) {
            setAuthStatus('error');
            setAuthError('缺少 Agent 参数');
            return;
        }

        const existingToken = useAuthStore.getState().token;
        if (code) {
            if (!provider) {
                setAuthStatus('error');
                setAuthError(H5_LOGIN_MESSAGES.incomplete);
                return;
            }

            const lockKey = `${provider}:${code}`;
            if (consumedExchangeCodes.has(lockKey)) {
                setAuthStatus(existingToken ? 'ready' : 'error');
                setAuthError(existingToken ? '' : H5_LOGIN_MESSAGES.expired);
                return;
            }

            if (isRememberedH5AuthCode(code)) {
                if (!existingToken) {
                    setAuthStatus('error');
                    setAuthError(H5_LOGIN_MESSAGES.expired);
                    return;
                }

                setAuthStatus('checking');
                setAuthError('');
                let active = true;
                authApi.validateSession()
                    .then((user) => {
                        if (!active) return;
                        setAuth(user, existingToken);
                        setAuthStatus('ready');
                        window.history.replaceState({}, '', cleanedOAuthAddress(window.location.href));
                    })
                    .catch((error: any) => {
                        if (!active) return;
                        if (error?.status === 401) {
                            useAuthStore.getState().logout();
                        }
                        setAuthStatus('error');
                        setAuthError(formatH5LoginError(error));
                    });

                return () => { active = false; };
            }

            setAuthStatus('exchanging');
            setAuthError('');

            let active = true;
            exchangeCodeOnce(lockKey, {
                provider,
                code,
                state: oauthState,
                redirect_uri: buildCodeExchangeRedirectUri(window.location.href),
                purpose: 'h5_agent_chat',
                channel,
                context: { agent_id: agentId },
            }).then((res) => {
                if (!active) return;
                rememberH5AuthCode(code);
                consumedExchangeCodes.add(lockKey);
                setAuth(res.user, res.access_token);
                setAuthStatus('ready');
                window.history.replaceState({}, '', cleanedOAuthAddress(window.location.href));
            }).catch((error: any) => {
                if (!active) return;
                setAuthStatus('error');
                setAuthError(formatH5LoginError(error));
            });

            return () => { active = false; };
        }

        if (existingToken) {
            setAuthStatus('ready');
            setAuthError('');
        } else {
            setAuthStatus('error');
            setAuthError(H5_LOGIN_MESSAGES.expired);
        }
    }, [agentId, channel, code, oauthState, provider, setAuth, token]);

    useEffect(() => {
        if (authStatus !== 'ready' || !agentId || !token) return;
        let cancelled = false;
        setAgentError('');
        agentApi.get(agentId)
            .then((data) => {
                if (!cancelled) setAgent(data);
            })
            .catch((error: any) => {
                if (!cancelled) setAgentError(error?.message || '无法加载 Agent');
            });
        return () => { cancelled = true; };
    }, [agentId, authStatus, token]);

    const refreshSceneManifest = useCallback(async () => {
        if (authStatus !== 'ready' || !agentId || !token) return null;
        const requestId = ++sceneManifestRequestRef.current;
        try {
            const loaded = await sceneApi.manifest(agentId, sceneKey);
            const manifest = loaded.enabled ? loaded : null;
            if (requestId === sceneManifestRequestRef.current) {
                sceneManifestRef.current = manifest;
                setSceneManifest(manifest);
            }
            return requestId === sceneManifestRequestRef.current
                ? manifest
                : sceneManifestRef.current;
        } catch {
            if (requestId === sceneManifestRequestRef.current) {
                sceneManifestRef.current = null;
                setSceneManifest(null);
            }
            return null;
        }
    }, [agentId, authStatus, sceneKey, token]);

    useEffect(() => {
        void refreshSceneManifest();
        return () => {
            sceneManifestRequestRef.current += 1;
        };
    }, [refreshSceneManifest]);

    useEffect(() => {
        if (pageResumeRevision === 0) return;
        void refreshSceneManifest();
    }, [pageResumeRevision, refreshSceneManifest]);

    const activeQuickActions = useMemo(
        () => menuVisibleSceneQuickActions(sceneManifest?.quick_actions),
        [sceneManifest?.quick_actions],
    );

    useLayoutEffect(() => {
        const element = quickActionsRef.current;
        if (!element || activeQuickActions.length === 0) {
            setQuickActionsOverflow(false);
            return;
        }
        const measure = () => {
            const reservedMenuSpace = Number.parseFloat(
                window.getComputedStyle(element).paddingRight,
            ) || 0;
            setQuickActionsOverflow(
                element.scrollWidth - reservedMenuSpace > element.clientWidth + 1,
            );
        };
        const frame = window.requestAnimationFrame(measure);
        const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
        observer?.observe(element);
        window.addEventListener('resize', measure);
        return () => {
            window.cancelAnimationFrame(frame);
            observer?.disconnect();
            window.removeEventListener('resize', measure);
        };
    }, [activeQuickActions]);

    useEffect(() => {
        if (quickActionsMenuCloseTimerRef.current !== null) {
            window.clearTimeout(quickActionsMenuCloseTimerRef.current);
            quickActionsMenuCloseTimerRef.current = null;
        }
        setQuickActionsMenuOpen(false);
        setQuickActionsMenuClosing(false);
        setQuickActionSearch('');
    }, [sceneManifest?.scene_key, sceneManifest?.revision]);

    useEffect(() => {
        if (!quickActionsMenuOpen) return;
        const closeOnEscape = (event: KeyboardEvent) => {
            if (event.key === 'Escape') closeQuickActionsMenu();
        };
        const previousOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        window.addEventListener('keydown', closeOnEscape);
        return () => {
            document.body.style.overflow = previousOverflow;
            window.removeEventListener('keydown', closeOnEscape);
        };
    }, [closeQuickActionsMenu, quickActionsMenuOpen]);

    useEffect(() => {
        if (confirmationPending && quickActionsMenuOpen) {
            closeQuickActionsMenu();
        }
    }, [closeQuickActionsMenu, confirmationPending, quickActionsMenuOpen]);

    useEffect(() => {
        if (authStatus !== 'ready' || !token) return;
        let cancelled = false;

        enterpriseApi.llmModels()
            .then((models) => {
                if (!cancelled) setLlmModels(models || []);
            })
            .catch(() => {
                if (!cancelled) setLlmModels([]);
            });

        tenantApi.me()
            .then((tenant) => {
                if (!cancelled) setTenantDefaultModelId(tenant?.default_model_id || null);
            })
            .catch(() => {
                if (!cancelled) setTenantDefaultModelId(null);
            });

        return () => { cancelled = true; };
    }, [authStatus, token]);

    return { activeQuickActions, refreshSceneManifest };
}
