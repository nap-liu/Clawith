import type { Dispatch, MouseEvent as ReactMouseEvent, ReactNode, RefObject, SetStateAction } from 'react';
import type { TreeScope, UploadItem, WorkspaceFileNode } from './types';
import { buildRevisionDiff, formatRevisionTime, MEMORY_ROOT, ENTERPRISE_ROOT, SKILLS_ROOT, WORKSPACE_ROOT } from './utils';

function renderUploadRows(uploadItems: UploadItem[], dirPath: string, depth: number) {
    return uploadItems
        .filter((item) => item.dir === dirPath)
        .map((item) => (
            <div
                key={item.id}
                className={`workspace-op-tree-upload ${item.status}`}
                style={{ paddingLeft: `${18 + depth * 12}px` }}
                title={item.error || `${item.progress}%`}
            >
                <div className="workspace-op-tree-upload-main">
                    <span className="workspace-op-tree-upload-name">{item.name}</span>
                    <span className="workspace-op-tree-upload-status">
                        {item.status === 'error'
                            ? 'Failed'
                            : item.status === 'done'
                                ? 'Done'
                                : item.status === 'processing'
                                    ? 'Processing…'
                                    : `${item.progress}%`}
                    </span>
                </div>
                {item.status !== 'error' && (
                    <div className="workspace-op-tree-upload-bar">
                        <span style={{ width: `${Math.max(6, item.progress)}%` }} />
                    </div>
                )}
                {item.status === 'error' && <div className="workspace-op-tree-upload-error">{item.error}</div>}
            </div>
        ));
}

export default function SidePanel({
    activePath,
    activityOpen,
    canModifyPath,
    deleteTreePath,
    editing,
    expandDir,
    expandedDirs,
    fileInputRef,
    fileTree,
    fileTreeActions,
    handleUploadFiles,
    isWritableTreeDir,
    loadDirs,
    restore,
    revisions,
    selectedDirPath,
    setActivityOpen,
    setExpandedDirs,
    setSelectedDirPath,
    setTreeOpen,
    startResize,
    switchToPath,
    switchTreeScope,
    treeOpen,
    treeScope,
    uploadItems,
    workspaceLabel,
    allLabel,
}: {
    activePath?: string | null;
    activityOpen: boolean;
    canModifyPath: (path?: string | null) => boolean;
    deleteTreePath: (path: string, label: string, selected?: boolean) => Promise<void>;
    editing: boolean;
    expandDir: (dirPath: string) => Promise<void>;
    expandedDirs: Set<string>;
    fileInputRef: RefObject<HTMLInputElement | null>;
    fileTree: WorkspaceFileNode[];
    fileTreeActions: ReactNode;
    handleUploadFiles: (files: FileList | null) => Promise<void>;
    isWritableTreeDir: (path?: string | null) => boolean;
    loadDirs: Set<string>;
    restore: (revisionId: string) => Promise<void>;
    revisions: any[];
    selectedDirPath: string;
    setActivityOpen: (open: boolean) => void;
    setExpandedDirs: Dispatch<SetStateAction<Set<string>>>;
    setSelectedDirPath: Dispatch<SetStateAction<string>>;
    setTreeOpen: (open: boolean) => void;
    startResize: (event: ReactMouseEvent<HTMLDivElement>) => void;
    switchToPath: (path: string) => void;
    switchTreeScope: (scope: TreeScope) => void;
    treeOpen: boolean;
    treeScope: TreeScope;
    uploadItems: UploadItem[];
    workspaceLabel: string;
    allLabel: string;
}) {
    const renderFileTreeNodes = (nodes: WorkspaceFileNode[], depth = 0): ReactNode => nodes.map((node) => {
        const selected = node.path === activePath;
        if (node.is_dir) {
            const expanded = expandedDirs.has(node.path);
            const dirSelected = selectedDirPath === node.path;
            const isLoading = loadDirs.has(node.path);
            return (
                <div key={node.path || node.name}>
                    <div className={`workspace-op-tree-dir ${dirSelected ? 'active' : ''}`} style={{ paddingLeft: `${6 + depth * 12}px` }}>
                        <button
                            className="workspace-op-tree-dir-main"
                            onClick={() => {
                                setSelectedDirPath(node.path);
                                if (!expanded) {
                                    void expandDir(node.path);
                                }
                                setExpandedDirs((prev) => {
                                    const next = new Set(prev);
                                    if (next.has(node.path)) next.delete(node.path);
                                    else next.add(node.path);
                                    return next;
                                });
                            }}
                        >
                            <span className="workspace-op-tree-chevron">
                                {isLoading ? '◌' : (expanded ? '▾' : '▸')}
                            </span>
                            <span>{node.name}</span>
                        </button>
                        {node.path !== WORKSPACE_ROOT && node.path !== SKILLS_ROOT && node.path !== MEMORY_ROOT && node.path !== ENTERPRISE_ROOT && canModifyPath(node.path) && (
                            <button
                                className="workspace-op-tree-file-delete"
                                title="Delete folder"
                                onClick={(e) => {
                                    e.stopPropagation();
                                    void deleteTreePath(node.path, node.name);
                                }}
                            >
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                    <path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2M10 11v6M14 11v6"/>
                                </svg>
                            </button>
                        )}
                    </div>
                    {expanded && (
                        <>
                            {isWritableTreeDir(node.path) && renderUploadRows(uploadItems, node.path, depth + 1)}
                            {node.children && renderFileTreeNodes(node.children, depth + 1)}
                        </>
                    )}
                </div>
            );
        }
        return (
            <div
                key={node.path}
                className={`workspace-op-tree-file ${selected ? 'active' : ''}`}
                style={{ paddingLeft: `${18 + depth * 12}px` }}
                onClick={() => switchToPath(node.path)}
                title={node.path}
            >
                <div className="workspace-op-tree-file-name">{node.name}</div>
                {!editing && canModifyPath(node.path) && (
                    <button
                        className="workspace-op-tree-file-delete"
                        title="Delete file"
                        onClick={async (e) => {
                            e.stopPropagation();
                            void deleteTreePath(node.path, node.name, selected);
                        }}
                    >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2M10 11v6M14 11v6"/>
                        </svg>
                    </button>
                )}
            </div>
        );
    });

    return (
        <>
            {!treeOpen && !activityOpen && (
                <button className="workspace-op-tree-edge-toggle" onClick={() => { setTreeOpen(true); }} title="Show files" aria-label="Show files">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <rect x="4" y="5" width="16" height="14" rx="2" stroke="currentColor" strokeWidth="1.9" />
                        <path d="M10 5v14" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
                        <path d="M16 9l-3 3 3 3" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                </button>
            )}
            {(treeOpen || activityOpen) && <div className="workspace-op-side-resize" onMouseDown={startResize} />}
            {activityOpen ? (
                <aside className="workspace-op-side">
                    <div className="workspace-op-side-title">
                        <span>Version history</span>
                        <button
                            className="workspace-op-mini-btn workspace-op-mini-btn-icon"
                            type="button"
                            onClick={() => setActivityOpen(false)}
                            title="Hide history"
                            aria-label="Hide history"
                        >
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                <rect x="4" y="5" width="16" height="14" rx="2" stroke="currentColor" strokeWidth="1.9" />
                                <path d="M10 5v14" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
                                <path d="M14 9l3 3-3 3" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
                            </svg>
                        </button>
                    </div>
                    <div className="workspace-op-side-list">
                        {!activePath && <div className="workspace-op-side-empty">Open a file to view its history.</div>}
                        {activePath && revisions.length === 0 && <div className="workspace-op-side-empty">No versions recorded yet.</div>}
                        {activePath && revisions.map((rev) => {
                            const diffText = buildRevisionDiff(rev);
                            const isNote = diffText.startsWith('No ') || diffText.startsWith('Restored') || diffText.startsWith('Autosaved');
                            return (
                                <div className="workspace-op-revision" key={rev.id}>
                                    <div className="workspace-op-revision-head">
                                        <div className="workspace-op-revision-meta">
                                            <strong>{rev.operation}</strong>
                                            <span>{rev.actor_type}</span>
                                        </div>
                                        <time className="workspace-op-revision-time" dateTime={rev.created_at || undefined}>
                                            {formatRevisionTime(rev.created_at)}
                                        </time>
                                    </div>
                                    <pre className={isNote ? 'workspace-op-revision-note' : ''}>{diffText}</pre>
                                    {rev.after_content != null && <button className="btn btn-secondary" onClick={() => void restore(rev.id)}>Restore</button>}
                                </div>
                            );
                        })}
                    </div>
                </aside>
            ) : treeOpen ? (
                <aside className="workspace-op-tree">
                    <div className="workspace-op-side-title">
                        <div className="workspace-op-tree-tools workspace-op-tree-tools-full">
                            <div className="workspace-op-tree-scope" role="tablist" aria-label="File tree scope">
                                <button
                                    className={treeScope === 'workspace' ? 'active' : ''}
                                type="button"
                                role="tab"
                                aria-selected={treeScope === 'workspace'}
                                onClick={() => switchTreeScope('workspace')}
                            >
                                    {workspaceLabel}
                                </button>
                                <button
                                    className={treeScope === 'all' ? 'active' : ''}
                                    type="button"
                                    role="tab"
                                    aria-selected={treeScope === 'all'}
                                    onClick={() => switchTreeScope('all')}
                                >
                                    {allLabel}
                                </button>
                            </div>
                            <div className="workspace-op-tree-actions">
                                {fileTreeActions}
                                <button
                                    className="workspace-op-mini-btn workspace-op-mini-btn-icon"
                                    type="button"
                                    onClick={() => { setActivityOpen(false); setTreeOpen(false); }}
                                    title="Hide files"
                                    aria-label="Hide files"
                                >
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                        <rect x="4" y="5" width="16" height="14" rx="2" stroke="currentColor" strokeWidth="1.9" />
                                        <path d="M10 5v14" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
                                        <path d="M14 9l3 3-3 3" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
                                    </svg>
                                </button>
                            </div>
                        </div>
                    </div>
                    <div className="workspace-op-tree-list">
                        {treeScope === 'workspace' && renderUploadRows(uploadItems, WORKSPACE_ROOT, -1)}
                        {fileTree.length ? renderFileTreeNodes(fileTree, 0) : <div className="workspace-op-tree-empty">No files yet.</div>}
                    </div>
                    <input
                        ref={fileInputRef}
                        type="file"
                        multiple
                        style={{ display: 'none' }}
                        onChange={async (e) => {
                            await handleUploadFiles(e.target.files);
                            e.target.value = '';
                        }}
                    />
                </aside>
            ) : null}
        </>
    );
}
