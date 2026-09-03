import {
    IconAlertTriangle,
    IconArrowLeft,
    IconHistory,
    IconLoader2,
    IconMenu2,
    IconMicrophone,
    IconPaperclip,
    IconPlayerStopFilled,
    IconPlus,
    IconRefresh,
    IconSearch,
    IconSend,
    IconX,
} from '@tabler/icons-react';
import ChatImageLightbox from '../../../components/ChatImageLightbox';
import ChatAttachmentIcon from '../../../components/ChatAttachmentIcon';
import SessionViewerDrawer from '../../../components/SessionViewerDrawer';
import Avatar from '../../../components/ui/Avatar';
import ConversationScrollToBottomButton from '../../../features/conversation/ConversationScrollToBottomButton';
import { horizontalSceneQuickActionStyle } from '../../../utils/sceneQuickActions';
import {
    formatFileSize,
    formatH5SessionTime,
    h5SessionChannelLabel,
} from './model';
import type { useH5AgentChatState } from './useH5AgentChatState';
import type { useH5AgentChatHistory } from './useH5AgentChatHistory';
import type { useH5AgentChatLifecycle } from './useH5AgentChatLifecycle';
import type { useH5AgentChatSocket } from './useH5AgentChatSocket';
import type { useH5AgentChatComposer } from './useH5AgentChatComposer';
import type { useH5AgentChatPresentation } from './useH5AgentChatPresentation';

export default function H5AgentChatView({
    state,
    history,
    lifecycle,
    socket,
    composer,
    presentation,
}: {
    state: ReturnType<typeof useH5AgentChatState>;
    history: ReturnType<typeof useH5AgentChatHistory>;
    lifecycle: ReturnType<typeof useH5AgentChatLifecycle>;
    socket: ReturnType<typeof useH5AgentChatSocket>;
    composer: ReturnType<typeof useH5AgentChatComposer>;
    presentation: ReturnType<typeof useH5AgentChatPresentation>;
}) {
    const {
        chatRootRef,
        closeQuickActionsMenu,
        resolvedTheme,
        themeMode,
        agent,
        connectionStatus,
        authStatus,
        isSwitchingSession,
        speech,
        sessionIdRef,
        sessionsPanelOpen,
        setSessionsPanelOpen,
        sessionsLoading,
        sessionsLoadingMore,
        sessions,
        sessionsError,
        sessionId,
        sessionsScrollerRef,
        authError,
        agentError,
        historyLoadingOlder,
        messagesScrollerRef,
        quickActionsMenuOpen,
        quickActionsMenuClosing,
        quickActionSearch,
        setQuickActionSearch,
        quickActionsOverflow,
        quickActionsRef,
        confirmationPending,
        openQuickActionsMenu,
        uploadDrafts,
        attachedFiles,
        uploadError,
        fileInputRef,
        setImagePreview,
        textareaRef,
        input,
        setInput,
        handleInputSelect,
        startSpeechInput,
        captureSpeechInsertionPoint,
        isReadOnly,
        isStartingNew,
        agentId,
        subagentSessionRun,
        closeSubagentSession,
        unavailableAttachmentKeys,
        handleAttachmentDownload,
        markAttachmentUnavailable,
        imagePreview,
    } = state;
    const { handleSessionsScroll, loadSessions } = history;
    const { activeQuickActions } = lifecycle;
    const { openSocket } = socket;
    const {
        activateSession,
        cancelUploadDraft,
        handleFileInputChange,
        handleInputKeyDown,
        handlePaste,
        openSessionPanel,
        removeAttachedFile,
        sendMessage,
        startNewSession,
        stopGeneration,
    } = composer;
    const {
        activateQuickAction,
        agentAvatarUrl,
        attachedImagePreviews,
        autoFollowInteractionProps,
        connectionLabel,
        conversationEntries,
        filteredQuickActions,
        generationActive,
        handleMessagesScroll,
        isBusy,
        preventProtectedContentAction,
        preventProtectedImageDrag,
        quickActionUnavailable,
        renderConversationEntry,
        renderWaitingMessage,
        resumeAutoFollow,
        rowVirtualizer,
        sendDisabled,
        showBlockingError,
        showScrollToBottom,
        uploadDisabled,
        virtualizeMessages,
    } = presentation;

    return (
        <main
            ref={chatRootRef}
            className={`h5-chat h5-chat--${resolvedTheme}`}
            data-theme={resolvedTheme}
            data-theme-mode={themeMode}
        >
            <header className="h5-chat__header">
                <div className="h5-chat__agent">
                    <Avatar
                        className="h5-chat__avatar"
                        src={agentAvatarUrl}
                        name={agent?.name || 'Agent'}
                        alt={agent?.name || 'Agent'}
                    />
                    <div className="h5-chat__agent-copy">
                        <div className="h5-chat__agent-name">{agent?.name || 'Agent'}</div>
                        <div className={`h5-chat__status h5-chat__status--${connectionStatus}`}>
                            <span />
                            {connectionLabel}
                        </div>
                    </div>
                </div>
                <div className="h5-chat__header-actions">
                    <button
                        type="button"
                        className="h5-chat__icon-button"
                        onClick={openSessionPanel}
                        disabled={authStatus !== 'ready' || isSwitchingSession || speech.isActive}
                        aria-label="历史会话"
                        title="历史会话"
                    >
                        <IconHistory size={18} />
                    </button>
                    <button
                        type="button"
                        className="h5-chat__icon-button"
                        onClick={startNewSession}
                        disabled={isStartingNew || authStatus !== 'ready' || speech.isActive}
                        aria-label="新会话"
                        title="新会话"
                    >
                        <IconPlus size={19} />
                    </button>
                    <button
                        type="button"
                        className="h5-chat__icon-button"
                        onClick={() => openSocket(sessionIdRef.current)}
                        disabled={authStatus !== 'ready' || speech.isActive}
                        aria-label="重连"
                        title="重连"
                    >
                        <IconRefresh size={18} />
                    </button>
                </div>
            </header>

            {sessionsPanelOpen ? (
                <section className="h5-chat__session-panel" role="dialog" aria-modal="true" aria-label="历史会话">
                    <div className="h5-chat__session-panel-header">
                        <button
                            type="button"
                            className="h5-chat__icon-button"
                            onClick={() => setSessionsPanelOpen(false)}
                            aria-label="返回"
                            title="返回"
                        >
                            <IconArrowLeft size={19} />
                        </button>
                        <div className="h5-chat__session-panel-title">历史会话</div>
                        <button
                            type="button"
                            className="h5-chat__icon-button"
                            onClick={loadSessions}
                            disabled={sessionsLoading || sessionsLoadingMore}
                            aria-label="刷新历史会话"
                            title="刷新历史会话"
                        >
                            <IconRefresh size={18} className={sessionsLoading ? 'h5-chat__spin' : undefined} />
                        </button>
                    </div>

                    {generationActive ? (
                        <div className="h5-chat__session-warning">当前回复进行中，请先终止后再切换会话</div>
                    ) : null}

                    <div
                        ref={sessionsScrollerRef}
                        className="h5-chat__session-list"
                        onScroll={handleSessionsScroll}
                    >
                        {sessionsLoading && sessions.length === 0 ? (
                            <div className="h5-chat__session-state">
                                <IconLoader2 size={20} className="h5-chat__spin" />
                                <span>加载中</span>
                            </div>
                        ) : sessionsError && sessions.length === 0 ? (
                            <div className="h5-chat__session-state h5-chat__session-state--error">
                                <IconAlertTriangle size={20} />
                                <span>{sessionsError}</span>
                            </div>
                        ) : sessions.length === 0 ? (
                            <div className="h5-chat__session-state">暂无历史会话</div>
                        ) : (
                            sessions.map((item) => {
                                const active = item.id === sessionId;
                                const timeLabel = formatH5SessionTime(item.last_message_at || item.created_at);
                                return (
                                    <button
                                        key={item.id}
                                        type="button"
                                        className={`h5-chat__session-item${active ? ' h5-chat__session-item--active' : ''}`}
                                        onClick={() => activateSession(item.id)}
                                        disabled={generationActive || isSwitchingSession}
                                    >
                                        <span className="h5-chat__session-main">
                                            <span className="h5-chat__session-title">{item.title}</span>
                                            <span className="h5-chat__session-meta">
                                                {timeLabel ? <span>{timeLabel}</span> : null}
                                                <span>{item.message_count} 条消息</span>
                                            </span>
                                        </span>
                                        <span className="h5-chat__session-side">
                                            <span className="h5-chat__session-channel">{h5SessionChannelLabel(item.source_channel)}</span>
                                            {item.is_primary ? <span className="h5-chat__session-badge">主会话</span> : null}
                                            {item.unread_count > 0 ? <span className="h5-chat__session-unread">{item.unread_count}</span> : null}
                                        </span>
                                    </button>
                                );
                            })
                        )}
                        {sessions.length > 0 && sessionsLoadingMore ? (
                            <div className="h5-chat__session-load-more" role="status">
                                <IconLoader2 size={16} className="h5-chat__spin" />
                                <span>正在加载更早会话…</span>
                            </div>
                        ) : null}
                        {sessions.length > 0 && sessionsError ? (
                            <div className="h5-chat__session-load-more h5-chat__session-load-more--error">
                                {sessionsError}
                            </div>
                        ) : null}
                    </div>
                </section>
            ) : null}

            {showBlockingError ? (
                <section className="h5-chat__state h5-chat__state--error">
                    <IconAlertTriangle size={28} />
                    <div>{authError || agentError}</div>
                </section>
            ) : isBusy ? (
                <section className="h5-chat__state">
                    <IconLoader2 size={28} className="h5-chat__spin" />
                    <div>{authStatus === 'exchanging' ? '正在登录' : '加载中'}</div>
                </section>
            ) : (
                <div className="h5-chat__messages-shell">
                    {historyLoadingOlder ? (
                        <div className="h5-chat__history-loading" role="status">正在加载更早消息…</div>
                    ) : null}
                    <div
                        ref={messagesScrollerRef}
                        data-conversation-scroller="h5"
                        className="h5-chat__messages-viewport"
                        aria-live="polite"
                        aria-label="会话消息"
                        tabIndex={0}
                        {...autoFollowInteractionProps}
                        onScroll={handleMessagesScroll}
                        onCopyCapture={preventProtectedContentAction}
                        onCutCapture={preventProtectedContentAction}
                        onContextMenuCapture={preventProtectedContentAction}
                        onDragStartCapture={preventProtectedImageDrag}
                    >
                        <section className={`h5-chat__messages${virtualizeMessages ? ' h5-chat__messages--virtual' : ''}`}>
                            {virtualizeMessages ? (
                                <div
                                    className="h5-chat__virtual-spacer"
                                    style={{ height: `${rowVirtualizer.getTotalSize()}px` }}
                                >
                                    {rowVirtualizer.getVirtualItems().map((virtualItem) => {
                                        const entry = conversationEntries[virtualItem.index];
                                        return (
                                            <div
                                                key={virtualItem.key}
                                                ref={rowVirtualizer.measureElement}
                                                data-index={virtualItem.index}
                                                className="h5-chat__virtual-row"
                                                style={{ transform: `translateY(${virtualItem.start}px)` }}
                                            >
                                                {entry ? renderConversationEntry(entry) : renderWaitingMessage()}
                                            </div>
                                        );
                                    })}
                                </div>
                            ) : (
                                <div className="h5-chat__flow-content">
                                    {conversationEntries.map((entry) => (
                                        <div key={entry.key} className="h5-chat__flow-row">
                                            {renderConversationEntry(entry)}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </section>
                    </div>
                    {showScrollToBottom ? (
                        <ConversationScrollToBottomButton
                            variant="h5"
                            onClick={resumeAutoFollow}
                        />
                    ) : null}
                </div>
            )}

            {quickActionsMenuOpen ? (
                <div
                    className={`h5-chat__quick-menu-backdrop${quickActionsMenuClosing ? ' is-closing' : ''}`}
                    role="presentation"
                    onClick={closeQuickActionsMenu}
                >
                    <section
                        className="h5-chat__quick-menu-sheet"
                        role="dialog"
                        aria-modal="true"
                        aria-label="选择功能"
                        onClick={(event) => event.stopPropagation()}
                    >
                        <div className="h5-chat__quick-menu-handle" />
                        <header className="h5-chat__quick-menu-header">
                            <div>
                                <strong>功能</strong>
                                <span>{activeQuickActions.length} 项</span>
                            </div>
                            <button
                                type="button"
                                className="h5-chat__quick-menu-close"
                                onClick={closeQuickActionsMenu}
                                aria-label="关闭"
                            >
                                <IconX size={20} />
                            </button>
                        </header>
                        <label className="h5-chat__quick-menu-search">
                            <IconSearch size={17} />
                            <input
                                type="search"
                                value={quickActionSearch}
                                onChange={(event) => setQuickActionSearch(event.target.value)}
                                placeholder="搜索功能"
                                aria-label="搜索功能"
                                autoFocus
                            />
                        </label>
                        <div className="h5-chat__quick-menu-list">
                            {filteredQuickActions.map((action) => (
                                <button
                                    key={action.id}
                                    type="button"
                                    className="h5-chat__quick-menu-item"
                                    disabled={quickActionUnavailable(action)}
                                    title={quickActionUnavailable(action) ? '当前操作完成后可用' : undefined}
                                    onClick={() => void activateQuickAction(action)}
                                >
                                    <span>{action.label}</span>
                                </button>
                            ))}
                            {filteredQuickActions.length === 0 ? (
                                <div className="h5-chat__quick-menu-empty">没有匹配的功能</div>
                            ) : null}
                        </div>
                    </section>
                </div>
            ) : null}

            <form
                className="h5-chat__composer"
                onSubmit={(event) => {
                    event.preventDefault();
                    sendMessage();
                }}
            >
                <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    className="h5-chat__file-input"
                    onChange={handleFileInputChange}
                />
                {uploadError ? <div className="h5-chat__upload-error">{uploadError}</div> : null}
                {activeQuickActions.length ? (
                    <div className={`h5-chat__quick-actions-shell ${quickActionsOverflow ? 'has-menu' : ''}`}>
                        <div ref={quickActionsRef} className="h5-chat__quick-actions" aria-label="功能">
                            {activeQuickActions.map((action) => (
                                <button
                                    key={action.id}
                                    type="button"
                                    className="h5-chat__quick-action"
                                    style={horizontalSceneQuickActionStyle(action.style)}
                                    disabled={quickActionUnavailable(action)}
                                    title={quickActionUnavailable(action) ? '当前操作完成后可用' : undefined}
                                    onClick={() => void activateQuickAction(action)}
                                >
                                    {action.label}
                                </button>
                            ))}
                        </div>
                        {quickActionsOverflow ? (
                            <button
                                type="button"
                                className="h5-chat__quick-actions-menu-button"
                                disabled={confirmationPending}
                                onClick={openQuickActionsMenu}
                                aria-label="查看全部功能"
                                title="查看全部功能"
                            >
                                <IconMenu2 size={18} />
                            </button>
                        ) : null}
                    </div>
                ) : null}
                {(uploadDrafts.length > 0 || attachedFiles.length > 0) ? (
                    <div className="h5-chat__attachments">
                        {uploadDrafts.map((draft) => (
                            <div key={draft.id} className="h5-chat__file-pill h5-chat__file-pill--uploading">
                                <div className="h5-chat__file-pill-fill" style={{ width: `${draft.percent}%` }} />
                                <div className="h5-chat__file-pill-row">
                                    {draft.previewUrl ? (
                                        <img className="h5-chat__file-thumb" src={draft.previewUrl} alt="" />
                                    ) : (
                                        <span className="h5-chat__file-icon"><ChatAttachmentIcon name={draft.name} size={16} /></span>
                                    )}
                                    <span className="h5-chat__file-name">{draft.name}</span>
                                    <span className="h5-chat__file-size">{formatFileSize(draft.sizeBytes)}</span>
                                    <span className="h5-chat__file-progress">{draft.percent}%</span>
                                    <button
                                        type="button"
                                        className="h5-chat__file-remove"
                                        onClick={() => cancelUploadDraft(draft)}
                                        aria-label="取消上传"
                                    >
                                        <IconX size={14} stroke={1.8} />
                                    </button>
                                </div>
                            </div>
                        ))}
                        {attachedFiles.map((file, index) => {
                            const imageIndex = attachedImagePreviews.findIndex((image) => image.src === file.imageUrl);
                            return (
                                <div key={`${file.name}-${index}`} className="h5-chat__file-pill" title={file.path || file.name}>
                                    <div className="h5-chat__file-pill-row">
                                        {file.imageUrl ? (
                                            <button
                                                type="button"
                                                className="h5-chat__file-thumb-button"
                                                onClick={() => {
                                                    if (imageIndex >= 0) setImagePreview({ images: attachedImagePreviews, index: imageIndex });
                                                }}
                                                aria-label="预览图片"
                                            >
                                                <img className="h5-chat__file-thumb" src={file.imageUrl} alt="" />
                                            </button>
                                        ) : (
                                            <span className="h5-chat__file-icon">
                                                <ChatAttachmentIcon name={file.name} mimeType={file.mimeType} size={16} />
                                            </span>
                                        )}
                                        <span className="h5-chat__file-name">{file.name}</span>
                                        <button
                                            type="button"
                                            className="h5-chat__file-remove"
                                            onClick={() => removeAttachedFile(index)}
                                            aria-label="移除附件"
                                        >
                                            <IconX size={14} stroke={1.8} />
                                        </button>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                ) : null}
                {speech.isActive || speech.error ? (
                    <div className={`h5-chat__speech-status${speech.error ? ' h5-chat__speech-status--error' : ''}`}>
                        <span>
                            {speech.error
                                || (speech.status === 'connecting'
                                    ? '正在连接语音识别…'
                                    : speech.status === 'stopping'
                                        ? '正在整理文字…'
                                        : `正在听${speech.transcript ? `：${speech.transcript}` : '…'}`)}
                        </span>
                    </div>
                ) : null}
                <div className="h5-chat__composer-row">
                    <button
                        type="button"
                        className="h5-chat__attach"
                        onClick={() => fileInputRef.current?.click()}
                        disabled={uploadDisabled}
                        aria-label="上传附件"
                        title="上传附件"
                    >
                        <IconPaperclip size={19} stroke={1.75} />
                    </button>
                    <div className="h5-chat__composer-input">
                        <textarea
                            ref={textareaRef}
                            value={input}
                            onChange={(event) => setInput(event.target.value)}
                            onSelect={handleInputSelect}
                            onKeyDown={handleInputKeyDown}
                            onPaste={handlePaste}
                            placeholder={isReadOnly ? '只读运行记录' : confirmationPending ? '请先完成上方确认' : '输入消息'}
                            rows={1}
                            onFocus={handleInputSelect}
                            disabled={isReadOnly || showBlockingError || isBusy || isStartingNew || speech.isActive || confirmationPending}
                        />
                        <button
                            type="button"
                            className={`h5-chat__speech-button${speech.status === 'recording' ? ' h5-chat__speech-button--recording' : ''}`}
                            onPointerDown={speech.status === 'recording' ? undefined : captureSpeechInsertionPoint}
                            onClick={speech.status === 'recording' ? speech.stop : startSpeechInput}
                            disabled={!speech.supported
                                || isReadOnly
                                || showBlockingError
                                || isBusy
                                || generationActive
                                || confirmationPending
                                || isStartingNew
                                || isSwitchingSession
                                || speech.status === 'connecting'
                                || speech.status === 'stopping'}
                            aria-label={speech.status === 'recording' ? '停止语音输入' : '开始语音输入'}
                            title={!speech.supported ? '当前浏览器不支持实时语音输入' : speech.status === 'recording' ? '停止语音输入' : '语音输入'}
                            aria-pressed={speech.status === 'recording'}
                        >
                            {speech.status === 'connecting' || speech.status === 'stopping'
                                ? <IconLoader2 size={19} className="h5-chat__spin" />
                                : speech.status === 'recording'
                                    ? <IconPlayerStopFilled size={17} />
                                    : <IconMicrophone size={19} stroke={1.8} />}
                        </button>
                    </div>
                    {generationActive ? (
                        <button
                            type="button"
                            className="h5-chat__send h5-chat__send--stop"
                            onClick={stopGeneration}
                            aria-label="停止"
                            title="停止"
                        >
                            <IconPlayerStopFilled size={18} />
                        </button>
                    ) : (
                        <button
                            type="submit"
                            className="h5-chat__send"
                            disabled={sendDisabled}
                            aria-label="发送"
                            title="发送"
                        >
                            {isStartingNew ? <IconLoader2 size={19} className="h5-chat__spin" /> : <IconSend size={19} />}
                        </button>
                    )}
                </div>
            </form>
            <SessionViewerDrawer
                agentId={agentId || ''}
                agentName={agent?.name || 'Agent'}
                target={subagentSessionRun?.sessionId ? {
                    sessionId: subagentSessionRun.sessionId,
                    agentId: subagentSessionRun.executionAgentId,
                    title: subagentSessionRun.name || subagentSessionRun.task,
                    status: subagentSessionRun.status,
                    mode: subagentSessionRun.mode,
                    model: subagentSessionRun.model,
                } : null}
                routeMode="h5"
                portalContainer={chatRootRef.current}
                onClose={closeSubagentSession}
                unavailableAttachmentKeys={unavailableAttachmentKeys}
                onAttachmentDownload={handleAttachmentDownload}
                onAttachmentUnavailable={markAttachmentUnavailable}
                onPreviewImages={(images, index) => setImagePreview({ images, index })}
            />
            <ChatImageLightbox
                open={!!imagePreview}
                images={imagePreview?.images || []}
                index={imagePreview?.index || 0}
                mode="mobile"
                allowDownload={false}
                protectImages
                onClose={() => setImagePreview(null)}
                onIndexChange={(index) => setImagePreview((prev) => prev ? { ...prev, index } : prev)}
            />
        </main>
    );
}
