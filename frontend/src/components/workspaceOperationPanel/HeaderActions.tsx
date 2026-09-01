import { fileApi } from '../../services/api';

export default function HeaderActions({
    activePath,
    agentId,
    canEdit,
    doneLabel,
    editLabel,
    editing,
    finishEditing,
    focusPreviewLabel,
    htmlPreviewSrc,
    isHtml,
    locked,
    onToggleLock,
    openInNewTabLabel,
    saveState,
    setEditing,
    shouldRenderLiveDraft,
}: {
    activePath?: string | null;
    agentId: string;
    canEdit: boolean;
    doneLabel: string;
    editLabel: string;
    editing: boolean;
    finishEditing: () => void;
    focusPreviewLabel: string;
    htmlPreviewSrc: string;
    isHtml: boolean;
    locked: boolean;
    onToggleLock?: () => void;
    openInNewTabLabel: string;
    saveState: 'idle' | 'saving' | 'saved' | 'error';
    setEditing: (value: boolean) => void;
    shouldRenderLiveDraft: boolean;
}) {
    return (
        <>
            {saveState !== 'idle' && <span className={`workspace-op-save ${saveState}`}>{saveState}</span>}
            {activePath && isHtml && !shouldRenderLiveDraft && (
                <a
                    className="live-panel-icon-btn"
                    href={htmlPreviewSrc || fileApi.downloadUrl(agentId, activePath, { inline: true })}
                    target="_blank"
                    rel="noreferrer"
                    title={openInNewTabLabel}
                    aria-label={openInNewTabLabel}
                >
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <path d="M14 4h6v6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                        <path d="M20 4l-9 9" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                        <path d="M11 5H7a3 3 0 00-3 3v9a3 3 0 003 3h9a3 3 0 003-3v-4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                </a>
            )}
            {activePath && onToggleLock && (
                <button
                    type="button"
                    className={`live-panel-icon-btn ${locked ? 'active' : ''}`}
                    onClick={onToggleLock}
                    title={focusPreviewLabel}
                    aria-label={focusPreviewLabel}
                >
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <path d="M4 9V6.5A2.5 2.5 0 016.5 4H9" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                        <path d="M15 4h2.5A2.5 2.5 0 0120 6.5V9" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                        <path d="M20 15v2.5a2.5 2.5 0 01-2.5 2.5H15" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                        <path d="M9 20H6.5A2.5 2.5 0 014 17.5V15" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                        <circle cx="12" cy="12" r="2.6" stroke="currentColor" strokeWidth="1.8" />
                    </svg>
                </button>
            )}
            {activePath && canEdit && !editing && (
                <button
                    type="button"
                    className="live-panel-icon-btn"
                    onClick={() => setEditing(true)}
                    title={editLabel}
                    aria-label={editLabel}
                >
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <path d="M12 20h9" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                        <path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4 12.5-12.5z" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                </button>
            )}
            {editing && (
                <button
                    type="button"
                    className="live-panel-icon-btn active"
                    onClick={finishEditing}
                    title={doneLabel}
                    aria-label={doneLabel}
                >
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <path d="M20 6L9 17l-5-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                </button>
            )}
        </>
    );
}
