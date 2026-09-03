import type React from 'react';
import { useCallback, useEffect, useMemo } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { IconAlertTriangle } from '@tabler/icons-react';
import type { SceneManifestQuickAction } from '../../../services/api';
import ChatAttachmentIcon from '../../../components/ChatAttachmentIcon';
import ChatMediaCard from '../../../components/ChatMediaCard';
import ChatToolCallRenderer from '../../../components/ChatToolCallRenderer';
import MarkdownRenderer from '../../../components/MarkdownRenderer';
import Avatar from '../../../components/ui/Avatar';
import { useConversationAutoFollow } from '../../../features/conversation/useConversationAutoFollow';
import {
    buildPreviewImage,
    buildPreviewImagesFromAttachments,
    extractChatImageDataMarkers,
    getChatQuotedMessageTypeLabel,
    partitionChatQuotedContent,
    splitAttachmentFileNames,
    stripChatImageDataMarkers,
    type ChatMessageAttachment,
} from '../../../utils/chatAttachments';
import {
    findMenuVisibleSceneQuickAction,
    isSceneQuickActionUnavailable,
} from '../../../utils/sceneQuickActions';
import { openExternalLinkWithBrowserDefault } from '../../../utils/browserLink';
import { copyToClipboard } from '../../../utils/clipboard';
import { copyH5LinkWithFeedback } from '../../../utils/h5LinkFeedback';
import type { H5ContainerRuntime } from '../../../utils/h5ContainerRuntime';
import { resolveH5LinkAction } from '../../../utils/h5LinkPolicy';
import {
    buildH5ConversationEntries,
    getH5ScrollAnchor,
    projectConversationTurnProgress,
    shouldProjectConversationTurnProgress,
    upsertToolCallMessage,
} from '../chatTimeline';
import H5AnalysisCard from './H5AnalysisCard';
import {
    VIRTUALIZE_ENTRY_THRESHOLD,
    estimateConversationEntrySize,
    resolveAgentAvatarUrl,
    stripAttachmentDisplayPrefix,
} from './model';
import type { useH5AgentChatState } from './useH5AgentChatState';
import type { useH5AgentChatHistory } from './useH5AgentChatHistory';
import type { useH5AgentChatLifecycle } from './useH5AgentChatLifecycle';
import type { useH5AgentChatSocket } from './useH5AgentChatSocket';
import type { useH5AgentChatComposer } from './useH5AgentChatComposer';

export function useH5AgentChatPresentation(
    state: ReturnType<typeof useH5AgentChatState>,
    history: ReturnType<typeof useH5AgentChatHistory>,
    lifecycle: ReturnType<typeof useH5AgentChatLifecycle>,
    socket: ReturnType<typeof useH5AgentChatSocket>,
    composer: ReturnType<typeof useH5AgentChatComposer>,
) {
    const {
        isWaiting,
        isStreaming,
        isStopping,
        messages,
        analysisExpanded,
        sessionId,
        turnRuntimeBySessionRef,
        attachedFiles,
        messagesScrollerRef,
        pageResumeRevision,
        resumeAutoFollowRef,
        cancelAutoFollowRef,
        connectionStatus,
        pageActive,
        authStatus,
        agent,
        agentError,
        historyOldestCursorRef,
        historyHasMore,
        historyLoadingOlder,
        setAnalysisExpanded,
        toast,
        containerRuntime,
        ensureContainerRuntime,
        setContainerRuntime,
        agentId,
        t,
        setImagePreview,
        openSubagentSession,
        setMessages,
        unavailableAttachmentKeys,
        markAttachmentUnavailable,
        handleAttachmentDownload,
        token,
        currentUser,
        isReadOnly,
        confirmationPending,
        messageRuntimeBlockedRef,
        messageDispatchLockedRef,
        speech,
        isStartingNew,
        isSwitchingSession,
        uploadDrafts,
        input,
        quickActionSearch,
        quickActionActivationRef,
        closeQuickActionsMenu,
    } = state;
    const { loadOlderHistory } = history;
    const { activeQuickActions, refreshSceneManifest } = lifecycle;
    const { prepareForNativeNavigation, recoverFromNativeNavigation } = socket;
    const { dispatchMessage } = composer;

    const generationActive = isWaiting || isStreaming || isStopping;
    const baseConversationEntries = useMemo(
        () => buildH5ConversationEntries(projectConversationTurnProgress(messages, false)),
        [messages],
    );
    const showTurnProgress = shouldProjectConversationTurnProgress(
        baseConversationEntries,
        generationActive,
        analysisExpanded,
    );
    const projectedMessages = useMemo(
        () => projectConversationTurnProgress(messages, showTurnProgress, {
            id: `conversation-turn-progress:${sessionId || 'new'}:${turnRuntimeBySessionRef.current[String(sessionId || '')]?.snapshot.generation || 0}`,
        }),
        [messages, sessionId, showTurnProgress],
    );
    const conversationEntries = useMemo(
        () => buildH5ConversationEntries(projectedMessages),
        [projectedMessages],
    );
    const attachedImagePreviews = useMemo(() => buildPreviewImagesFromAttachments(attachedFiles), [attachedFiles]);
    const scrollAnchor = useMemo(
        () => getH5ScrollAnchor(conversationEntries, generationActive),
        [conversationEntries, generationActive],
    );
    const virtualizeMessages = conversationEntries.length > VIRTUALIZE_ENTRY_THRESHOLD;
    const virtualItemCount = conversationEntries.length;
    const rowVirtualizer = useVirtualizer({
        count: virtualizeMessages ? virtualItemCount : 0,
        getScrollElement: () => messagesScrollerRef.current,
        estimateSize: (index) => {
            const entry = conversationEntries[index];
            if (!entry) return 54;
            return estimateConversationEntrySize(entry, entry.type === 'analysis_group' && !!analysisExpanded[entry.key]);
        },
        getItemKey: (index) => {
            return conversationEntries[index].key;
        },
        overscan: 8,
        enabled: virtualizeMessages,
    });
    useEffect(() => {
        if (!virtualizeMessages || pageResumeRevision === 0 || document.hidden) return;
        const measureMountedRows = () => {
            messagesScrollerRef.current
                ?.querySelectorAll<HTMLElement>('.h5-chat__virtual-row')
                .forEach((element) => rowVirtualizer.measureElement(element));
        };
        let secondFrame: number | null = null;
        const firstFrame = window.requestAnimationFrame(() => {
            measureMountedRows();
            secondFrame = window.requestAnimationFrame(measureMountedRows);
        });
        return () => {
            window.cancelAnimationFrame(firstFrame);
            if (secondFrame != null) window.cancelAnimationFrame(secondFrame);
        };
    }, [pageResumeRevision, rowVirtualizer, virtualizeMessages]);
    const alignH5ConversationBottom = useCallback((scroller: HTMLElement) => {
        if (virtualizeMessages && virtualItemCount > 0) {
            rowVirtualizer.scrollToIndex(virtualItemCount - 1, { align: 'end' });
        }
        scroller.scrollTop = scroller.scrollHeight;
    }, [rowVirtualizer, virtualItemCount, virtualizeMessages]);
    const {
        showScrollToBottom,
        resumeAutoFollow,
        cancelPendingAutoFollow,
        interactionProps: autoFollowInteractionProps,
    } = useConversationAutoFollow({
        scrollerRef: messagesScrollerRef,
        contentKey: `${scrollAnchor}:${connectionStatus}:${pageResumeRevision}`,
        resetKey: sessionId,
        enabled: pageActive && authStatus === 'ready' && !!agent && !agentError,
        alignBottom: alignH5ConversationBottom,
    });
    useEffect(() => {
        resumeAutoFollowRef.current = resumeAutoFollow;
        cancelAutoFollowRef.current = cancelPendingAutoFollow;
    }, [cancelPendingAutoFollow, resumeAutoFollow]);
    const handleMessagesScroll = useCallback((event: React.UIEvent<HTMLElement>) => {
        if (event.currentTarget.scrollTop <= 120) void loadOlderHistory();
    }, [loadOlderHistory]);
    useEffect(() => {
        const scroller = messagesScrollerRef.current;
        if (
            !scroller
            || !historyHasMore
            || historyLoadingOlder
            || !historyOldestCursorRef.current
            || scroller.clientHeight <= 0
            || scroller.scrollHeight > scroller.clientHeight + 1
        ) return;
        void loadOlderHistory();
    }, [conversationEntries.length, historyHasMore, historyLoadingOlder, loadOlderHistory]);
    const toggleAnalysis = useCallback((key: string) => {
        setAnalysisExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
    }, []);

    const renderWaitingMessage = useCallback(() => (
        <article className="h5-chat__message h5-chat__message--assistant">
            <div className="h5-chat__bubble">
                <div className="h5-chat__typing"><span /><span /><span /></div>
            </div>
        </article>
    ), []);
    const preventProtectedContentAction = useCallback((event: React.SyntheticEvent) => {
        event.preventDefault();
    }, []);
    const preventProtectedImageDrag = useCallback((event: React.DragEvent<HTMLElement>) => {
        const target = event.target;
        if (target instanceof Element && target.closest('img')) {
            event.preventDefault();
        }
    }, []);

    const copyLinkWithFeedback = useCallback(async (url: string) => {
        await copyH5LinkWithFeedback(url, {
            copy: copyToClipboard,
            onCopied: () => toast.success('已复制请到浏览器中打开'),
            onCopyFailed: () => toast.error('复制失败，请稍后重试'),
        });
    }, [toast]);

    const handlePlatformLinkFailure = useCallback(async (
        platform: 'DingTalk' | 'WeChat',
        url: string,
        error: unknown,
    ) => {
        recoverFromNativeNavigation();
        console.warn(`${platform} mini-program link open failed`, error);
        if (openExternalLinkWithBrowserDefault(url)) return;

        await copyLinkWithFeedback(url);
    }, [copyLinkWithFeedback, recoverFromNativeNavigation]);

    const handleLinkForRuntime = useCallback((href: string, runtime: H5ContainerRuntime): boolean => {
        const action = resolveH5LinkAction(href, { runtime });
        if (action.type === 'dingtalk-miniapp-navigate') {
            prepareForNativeNavigation();
            void import('../../../utils/dingtalkLink')
                .then(({ navigateDingTalkMiniProgramPage }) => (
                    navigateDingTalkMiniProgramPage(action.route)
                ))
                .catch((error) => {
                    recoverFromNativeNavigation();
                    console.warn('DingTalk mini-program page navigation failed', error);
                    toast.error('小程序页面跳转失败');
                });
            return true;
        }
        if (action.type === 'wechat-miniapp-navigate') {
            prepareForNativeNavigation();
            void import('../../../utils/wechatMiniProgramLink')
                .then(({ navigateWechatMiniProgramPage }) => (
                    navigateWechatMiniProgramPage(action.route)
                ))
                .catch((error) => {
                    recoverFromNativeNavigation();
                    console.warn('WeChat mini-program page navigation failed', error);
                    toast.error('小程序页面跳转失败');
                });
            return true;
        }
        if (action.type === 'miniprogram-unavailable') {
            toast.warning('当前环境不支持跳转小程序');
            return true;
        }
        if (action.type === 'invalid-miniprogram-uri') {
            toast.error('无效的小程序页面地址');
            return true;
        }
        if (action.type === 'native') return false;

        if (action.type === 'dingtalk-open') {
            prepareForNativeNavigation();
            void import('../../../utils/dingtalkLink')
                .then(({ openDingTalkMiniProgramWebview }) => (
                    openDingTalkMiniProgramWebview(action.url)
                ))
                .catch((error) => handlePlatformLinkFailure('DingTalk', action.url, error));
            return true;
        }

        if (action.type === 'blocked') {
            void copyLinkWithFeedback(action.url);
            return true;
        }

        prepareForNativeNavigation();
        void import('../../../utils/wechatMiniProgramLink')
            .then(({ openWechatMiniProgramWebview }) => openWechatMiniProgramWebview(action.url))
            .catch((error) => handlePlatformLinkFailure('WeChat', action.url, error));
        return true;
    }, [
        copyLinkWithFeedback,
        handlePlatformLinkFailure,
        prepareForNativeNavigation,
        recoverFromNativeNavigation,
        toast,
    ]);

    const handleMarkdownLinkClick = useCallback((href: string): boolean => {
        if (containerRuntime !== 'detecting') {
            return handleLinkForRuntime(href, containerRuntime);
        }

        void ensureContainerRuntime().then((runtime) => {
            setContainerRuntime(runtime);
            if (!handleLinkForRuntime(href, runtime)) {
                window.location.assign(href);
            }
        });
        return true;
    }, [containerRuntime, ensureContainerRuntime, handleLinkForRuntime]);

    const renderConversationEntry = useCallback((entry: (typeof conversationEntries)[number]) => {
        if (entry.type === 'analysis_group') {
            return (
                <H5AnalysisCard
                    items={entry.items}
                    expanded={!!analysisExpanded[entry.key]}
                    onToggle={() => toggleAnalysis(entry.key)}
                />
            );
        }

        const msg = entry.msg;
        const messageAvatarUrl = msg.sender_avatar_url
            || (msg.role === 'user' ? currentUser?.avatar_url : agent?.avatar_url);
        const messageAvatarName = msg.sender_name
            || (msg.role === 'user' ? currentUser?.display_name : agent?.name);
        const rawDisplayContent = msg.fileName ? stripAttachmentDisplayPrefix(msg.content) : msg.content;
        const displayContent = stripChatImageDataMarkers(rawDisplayContent);
        const quotedMessage = msg.quoted_message;
        const filePreviewImages = msg.previewImages || (msg.imageUrl ? [buildPreviewImage(msg.imageUrl, msg.fileName)] : []);
        const inlinePreviewImages = filePreviewImages.length > 0 ? [] : extractChatImageDataMarkers(rawDisplayContent);
        const allPreviewImages = filePreviewImages.length > 0 ? filePreviewImages : inlinePreviewImages;
        const hasCanonicalAttachments = Array.isArray(msg.attachments);
        const allAttachments: ChatMessageAttachment[] = hasCanonicalAttachments ? msg.attachments || [] : [];
        const {
            attachments,
            quotedAttachments,
            previewImages,
            quotedPreviewImages,
        } = partitionChatQuotedContent(quotedMessage, allAttachments, allPreviewImages);
        const previewedImageNames = new Set(previewImages.map((image) => image.filename).filter(Boolean));
        const mediaAttachments = msg.role === 'user'
            ? attachments.filter((attachment) => attachment.kind === 'audio' || attachment.kind === 'video')
            : [];
        const quotedMediaAttachments = quotedAttachments.filter((attachment) => attachment.kind === 'audio' || attachment.kind === 'video');
        const fileChips: Array<{
            name: string;
            path?: string;
            kind?: ChatMessageAttachment['kind'];
            mimeType?: string;
        }> = hasCanonicalAttachments
            ? attachments
                .filter((attachment) => !['image', 'audio', 'video'].includes(attachment.kind))
                .map((attachment: ChatMessageAttachment) => ({
                    name: attachment.display_name,
                    path: attachment.path,
                    kind: attachment.kind,
                    mimeType: attachment.mime_type,
                }))
            : splitAttachmentFileNames(msg.fileName)
                .filter((name) => !previewedImageNames.has(name))
                .map((name) => ({ name }));
        const quotedFileChips = quotedAttachments
            .filter((attachment) => !['image', 'audio', 'video'].includes(attachment.kind))
            .map((attachment) => ({
                name: attachment.display_name,
                path: attachment.path,
                kind: attachment.kind,
                mimeType: attachment.mime_type,
            }));
        if (entry.type === 'special_render') {
            return (
                <article className={`h5-chat__message h5-chat__message--assistant h5-chat__message--special-render h5-chat__message--${entry.renderType}`}>
                    <Avatar
                        className="h5-chat__message-avatar"
                        src={messageAvatarUrl}
                        name={messageAvatarName}
                    />
                    <div className={`h5-chat__special-render h5-chat__${entry.renderType}-card`}>
                        <ChatToolCallRenderer
                            agentId={agentId || ''}
                            message={msg}
                            t={t}
                            mode="h5"
                            onPreviewImages={(images, index) => setImagePreview({ images, index })}
                            onOpenSubagentSession={openSubagentSession}
                            onResolved={(resolvedResult) => {
                                setMessages((prev) => upsertToolCallMessage(prev, {
                                    ...msg,
                                    toolStatus: 'done',
                                    toolResult: resolvedResult,
                                }));
                            }}
                        />
                    </div>
                </article>
            );
        }

        return (
            <article className={`h5-chat__message h5-chat__message--${msg.role}`}>
                {msg.role !== 'user' && msg.role !== 'system' ? (
                    <Avatar
                        className="h5-chat__message-avatar"
                        src={messageAvatarUrl}
                        name={messageAvatarName}
                    />
                ) : null}
                <div className="h5-chat__bubble">
                    {quotedMessage ? (
                        <div className="conversation-quoted-message">
                            <div className="conversation-quoted-message__header">
                                <span>↪ {quotedMessage.sender_name || '发送人未知'}</span>
                                <span>{getChatQuotedMessageTypeLabel(quotedMessage.message_type)}</span>
                            </div>
                            {quotedPreviewImages.length > 0 ? (
                                <div className="h5-chat__image-grid">
                                    {quotedPreviewImages.map((image, index) => (
                                        <button key={`${image.src}-${index}`} type="button" className="h5-chat__bubble-image-button" onClick={() => setImagePreview({ images: quotedPreviewImages, index })} aria-label="预览引用图片">
                                            <img className="h5-chat__bubble-image" src={image.src} alt={image.alt || image.filename || '图片'} loading="lazy" draggable={false} onError={() => markAttachmentUnavailable(image.path || image.src)} />
                                        </button>
                                    ))}
                                </div>
                            ) : null}
                            {quotedMediaAttachments.length > 0 ? (
                                <div className="h5-chat__media-list">
                                    {quotedMediaAttachments.map((attachment, index) => <ChatMediaCard key={`${attachment.path}-${index}`} agentId={agentId || ''} messageId={String(msg.id || '')} attachment={attachment} mode="h5" onUnavailable={() => markAttachmentUnavailable(attachment.path)} />)}
                                </div>
                            ) : null}
                            {quotedFileChips.length > 0 ? (
                                <div className="h5-chat__file-chip-list">
                                    {quotedFileChips.map((file, index) => <button key={`${file.path}-${index}`} type="button" className="h5-chat__file-chip" disabled={unavailableAttachmentKeys.has(file.path)} onClick={() => void handleAttachmentDownload(file.path, file.name)}><ChatAttachmentIcon name={file.name} kind={file.kind} mimeType={file.mimeType} /><span>{file.name}</span></button>)}
                                </div>
                            ) : null}
                            {quotedMessage.text ? <MarkdownRenderer className="h5-chat__markdown" content={quotedMessage.text} imagePreviewMode="mobile" allowImageDownload={false} protectImages onLinkClick={handleMarkdownLinkClick} /> : null}
                            {!quotedMessage.text && quotedMessage.attachments.length === 0 ? <div className="conversation-quoted-message__unavailable">{quotedMessage.content_status === 'failed' ? '引用内容获取失败' : '引用内容不可用'}</div> : null}
                            {quotedMessage.content_status === 'partial' ? <div className="conversation-quoted-message__unavailable">部分引用内容未能获取</div> : null}
                        </div>
                    ) : null}
                    {previewImages.length > 0 ? (
                        <div className="h5-chat__image-grid">
                            {previewImages.map((image, index) => (
                                unavailableAttachmentKeys.has(image.path || image.src) ? (
                                    <div key={`${image.path || image.src}-${index}`} className="h5-chat__file-chip">
                                        <IconAlertTriangle size={14} stroke={1.75} />
                                        <span>{image.filename || '图片'} · 当前不可访问</span>
                                    </div>
                                ) : (
                                    <button
                                        key={`${image.src}-${index}`}
                                        type="button"
                                        className="h5-chat__bubble-image-button"
                                        onClick={() => setImagePreview({ images: previewImages, index })}
                                        aria-label="预览图片"
                                    >
                                        <img
                                            className="h5-chat__bubble-image"
                                            src={image.src}
                                            alt={image.alt || image.filename || 'image'}
                                            loading="lazy"
                                            draggable={false}
                                            onError={() => markAttachmentUnavailable(image.path || image.src)}
                                        />
                                    </button>
                                )
                            ))}
                        </div>
                    ) : null}
                    {mediaAttachments.length > 0 ? (
                        <div className="h5-chat__media-list">
                            {mediaAttachments.map((attachment, mediaIndex) => (
                                <ChatMediaCard
                                    key={`${attachment.path}-${mediaIndex}`}
                                    agentId={agentId || ''}
                                    messageId={String(msg.id || '')}
                                    attachment={attachment}
                                    mode="h5"
                                    onUnavailable={() => markAttachmentUnavailable(attachment.path)}
                                />
                            ))}
                        </div>
                    ) : null}
                    {fileChips.length > 0 ? (
                        <div className="h5-chat__file-chip-list">
                            {fileChips.map((file, fileIndex) => (
                                <button
                                    key={`${file.path || file.name}-${fileIndex}`}
                                    type="button"
                                    className="h5-chat__file-chip"
                                    disabled={!file.path || unavailableAttachmentKeys.has(file.path)}
                                    onClick={() => file.path && void handleAttachmentDownload(file.path, file.name)}
                                >
                                    <ChatAttachmentIcon
                                        name={file.name}
                                        kind={file.kind}
                                        mimeType={file.mimeType}
                                    />
                                    <span>{file.name}{file.path && unavailableAttachmentKeys.has(file.path) ? ' · 当前不可访问' : ''}</span>
                                </button>
                            ))}
                        </div>
                    ) : null}
                    {msg.thinking && !msg.content ? (
                        <div className="h5-chat__thinking">思考中</div>
                    ) : null}
                    {displayContent ? (
                        <MarkdownRenderer
                            className="h5-chat__markdown"
                            content={displayContent}
                            imagePreviewMode="mobile"
                            allowImageDownload={false}
                            protectImages
                            onLinkClick={handleMarkdownLinkClick}
                        />
                    ) : msg.streaming ? (
                        <div className="h5-chat__typing"><span /><span /><span /></div>
                    ) : null}
                </div>
                {msg.role === 'user' ? (
                    <Avatar
                        className="h5-chat__message-avatar"
                        src={messageAvatarUrl}
                        name={messageAvatarName}
                    />
                ) : null}
            </article>
        );
    }, [
        agentId,
        agent?.avatar_url,
        agent?.name,
        analysisExpanded,
        currentUser?.avatar_url,
        currentUser?.display_name,
        handleAttachmentDownload,
        handleMarkdownLinkClick,
        markAttachmentUnavailable,
        openSubagentSession,
        toggleAnalysis,
        unavailableAttachmentKeys,
    ]);

    const connectionLabel = connectionStatus === 'connected'
        ? '已连接'
        : connectionStatus === 'connecting'
            ? '连接中'
            : '未连接';

    const showBlockingError = authStatus === 'error' || !!agentError;
    const isBusy = authStatus === 'checking' || authStatus === 'exchanging' || (authStatus === 'ready' && !agent && !agentError);
    messageRuntimeBlockedRef.current = generationActive
        || isReadOnly
        || confirmationPending
        || showBlockingError
        || isBusy
        || speech.isActive
        || isStartingNew
        || isSwitchingSession
        || uploadDrafts.length > 0;
    useEffect(() => {
        if (!generationActive && !isStartingNew && !isSwitchingSession) {
            messageDispatchLockedRef.current = false;
        }
    }, [generationActive, isStartingNew, isSwitchingSession]);
    const sendDisabled = (!input.trim() && attachedFiles.length === 0)
        || isReadOnly
        || showBlockingError
        || isBusy
        || generationActive
        || confirmationPending
        || speech.isActive
        || isStartingNew
        || isSwitchingSession
        || uploadDrafts.length > 0;
    const uploadDisabled = showBlockingError
        || isReadOnly
        || isBusy
        || generationActive
        || confirmationPending
        || speech.isActive
        || isStartingNew
        || isSwitchingSession
        || uploadDrafts.length > 0
        || attachedFiles.length >= 10;
    const agentAvatarUrl = resolveAgentAvatarUrl(agent?.avatar_url, token);
    const quickMessageDisabled = generationActive
        || isReadOnly
        || confirmationPending
        || showBlockingError
        || isBusy
        || isStartingNew
        || isSwitchingSession;
    const quickActionUnavailable = (action: SceneManifestQuickAction) => (
        isSceneQuickActionUnavailable(action, {
            confirmationPending,
            sendMessageUnavailable: quickMessageDisabled,
        })
    );
    const filteredQuickActions = useMemo(() => {
        const query = quickActionSearch.trim().toLocaleLowerCase();
        const actions = activeQuickActions;
        if (!query) return actions;
        return actions.filter((action) => (
            `${action.label} ${action.message || ''} ${action.uri || ''}`
                .toLocaleLowerCase()
                .includes(query)
        ));
    }, [activeQuickActions, quickActionSearch]);
    const activateQuickAction = async (snapshot: SceneManifestQuickAction) => {
        if (
            quickActionActivationRef.current
            || confirmationPending
            || (snapshot.type === 'send_message'
                && (messageRuntimeBlockedRef.current || messageDispatchLockedRef.current))
        ) return;

        quickActionActivationRef.current = true;
        try {
            const latestManifest = await refreshSceneManifest();
            const action = findMenuVisibleSceneQuickAction(
                latestManifest?.quick_actions,
                snapshot.id,
            );
            if (!action) {
                toast.info('功能配置已更新，请重新选择');
                return;
            }
            if (action.type === 'open_uri' && action.uri) {
                closeQuickActionsMenu();
                if (!handleMarkdownLinkClick(action.uri)) {
                    window.location.assign(action.uri);
                }
                return;
            }
            if (
                action.type !== 'send_message'
                || !action.message
                || messageRuntimeBlockedRef.current
                || messageDispatchLockedRef.current
            ) return;
            closeQuickActionsMenu();
            await dispatchMessage(action.message, [], false);
        } finally {
            quickActionActivationRef.current = false;
        }
    };

    return {
        activateQuickAction,
        agentAvatarUrl,
        attachedImagePreviews,
        autoFollowInteractionProps,
        connectionLabel,
        conversationEntries,
        filteredQuickActions,
        handleMarkdownLinkClick,
        handleMessagesScroll,
        isBusy,
        generationActive,
        preventProtectedContentAction,
        preventProtectedImageDrag,
        quickActionUnavailable,
        renderConversationEntry,
        renderWaitingMessage,
        rowVirtualizer,
        resumeAutoFollow,
        sendDisabled,
        showBlockingError,
        showScrollToBottom,
        uploadDisabled,
        virtualizeMessages,
    };
}
