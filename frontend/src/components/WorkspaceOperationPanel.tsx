import type { MouseEvent as ReactMouseEvent } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import PromptModal from './PromptModal';
import { useDialog } from './Dialog/DialogProvider';
import { fileApi, uploadFileWithProgress } from '../services/api';
import HeaderActions from './workspaceOperationPanel/HeaderActions';
import PreviewContent from './workspaceOperationPanel/PreviewContent';
import SidePanel from './workspaceOperationPanel/SidePanel';
import type {
    TreeScope,
    UploadItem,
    WorkspaceFileNode,
    WorkspaceOperationPanelProps,
} from './workspaceOperationPanel/types';
import {
    DEFAULT_HISTORY_WIDTH,
    DEFAULT_TREE_WIDTH,
    EDITABLE_EXTS,
    IMAGE_EXTS,
    MAX_SIDE_WIDTH,
    MIN_SAVING_VISIBLE_MS,
    MIN_SIDE_WIDTH,
    SAVED_VISIBLE_MS,
    WORKSPACE_ROOT,
    buildPreviewVersion,
    directoryOf,
    extOf,
    fileName,
    isEnterprisePath,
    isWorkspacePath,
    isWritableDir,
    normalizeWritableDir,
    parseCsv,
    parentDirs,
    removeWorkspaceExpansion,
    trimTrailingEmpty,
} from './workspaceOperationPanel/utils';

export type { WorkspaceActivity, WorkspaceLiveDraft } from './workspaceOperationPanel/types';

export default function WorkspaceOperationPanel({
    agentId,
    sessionId,
    activePath,
    activities,
    liveDraft,
    locked = false,
    canManageEnterpriseInfo = false,
    canManageWorkspace = false,
    onSelectPath,
    onToggleLock,
    onEditingChange,
    onPathDeleted,
    activityOpen: activityOpenProp,
    onActivityToggle,
    headerActionsTargetId,
}: WorkspaceOperationPanelProps) {
    const { t } = useTranslation();
    const dialog = useDialog();
    const [preview, setPreview] = useState<any>(null);
    const [content, setContent] = useState('');
    const [draft, setDraft] = useState('');
    const [previewState, setPreviewState] = useState<'idle' | 'loading' | 'ready' | 'deleted'>('idle');
    const [editing, setEditing] = useState(false);
    const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
    const [revisions, setRevisions] = useState<any[]>([]);
    const [fileTree, setFileTree] = useState<WorkspaceFileNode[]>([]);
    // Track which directories have already had their children fetched, and
    // which are currently fetching. Used so a large workspace (think: a
    // freshly-cloned ant-design repo) doesn't trigger thousands of
    // /api/agents/<id>/files/?path=... calls on first render.
    const [loadedDirs, setLoadedDirs] = useState<Set<string>>(() => new Set());
    const [loadingDirs, setLoadingDirs] = useState<Set<string>>(() => new Set());
    const [activityOpenLocal, setActivityOpenLocal] = useState(false);
    const activityOpen = activityOpenProp ?? activityOpenLocal;
    const setActivityOpen = onActivityToggle ?? setActivityOpenLocal;
    const [treeOpen, setTreeOpen] = useState(true);
    const [expandedDirs, setExpandedDirs] = useState<Set<string>>(() => new Set());
    const [treeScope, setTreeScope] = useState<TreeScope>('workspace');
    const [pendingSwitchPath, setPendingSwitchPath] = useState<string | null>(null);
    const [sideWidth, setSideWidth] = useState(DEFAULT_TREE_WIDTH);
    const [selectedDirPath, setSelectedDirPath] = useState(WORKSPACE_ROOT);
    const [createFolderModalOpen, setCreateFolderModalOpen] = useState(false);
    const [uploadItems, setUploadItems] = useState<UploadItem[]>([]);
    const [isSideResizing, setIsSideResizing] = useState(false);
    const [headerActionsTarget, setHeaderActionsTarget] = useState<HTMLElement | null>(null);
    const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const saveStateTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const lockTimer = useRef<ReturnType<typeof setInterval> | null>(null);
    const prevActivePathRef = useRef<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement | null>(null);
    const resizeRef = useRef<{ startX: number; startWidth: number } | null>(null);
    const previewScrollRef = useRef<HTMLDivElement | null>(null);
    const previewShouldFollowRef = useRef(true);
    const suppressNextAutoRevealRef = useRef(false);
    const manualTreeScopeRef = useRef<TreeScope | null>(null);

    const ext = activePath ? extOf(activePath) : '';
    // Workspace write/delete permission (upstream #660): enterprise paths need
    // canManageEnterpriseInfo; all other workspace paths need canManageWorkspace.
    const canModifyPath = (path?: string | null) =>
        isEnterprisePath(path) ? canManageEnterpriseInfo : canManageWorkspace;
    const isWritableTreeDir = (path?: string | null) => isWritableDir(path) && canModifyPath(path);
    const canEdit = !!activePath && EDITABLE_EXTS.has(ext) && canModifyPath(activePath);
    const isHtml = ext === '.html' || ext === '.htm';
    const isImage = IMAGE_EXTS.has(ext);
    const activityKey = activities.map((item) => `${item.action}:${item.path}`).join('|');
    const treeTargetDir = normalizeWritableDir(canModifyPath(selectedDirPath) ? selectedDirPath : directoryOf(activePath));
    const panelSideWidth = activityOpen ? Math.max(sideWidth, DEFAULT_HISTORY_WIDTH) : sideWidth;
    const draftMatchesActiveFile = !!(liveDraft?.path && activePath && liveDraft.path === activePath);
    const shouldRenderLiveDraft = !!liveDraft && (!activePath || !liveDraft.path || draftMatchesActiveFile);
    const liveDraftContent = liveDraft?.content || '';

    useEffect(() => {
        if (!headerActionsTargetId) {
            setHeaderActionsTarget(null);
            return;
        }
        setHeaderActionsTarget(document.getElementById(headerActionsTargetId));
    }, [headerActionsTargetId]);

    useEffect(() => {
        if (!editing || canModifyPath(activePath)) return;
        if (saveTimer.current) clearTimeout(saveTimer.current);
        setDraft(content);
        setEditing(false);
        onEditingChange?.(false);
    }, [activePath, canManageEnterpriseInfo, content, editing, onEditingChange]);

    const load = async () => {
        if (!activePath) {
            setPreviewState('idle');
            return;
        }
        setPreviewState('loading');
        try {
            const data = await fileApi.preview(agentId, activePath);
            setPreview(data);
            const text = data.content || '';
            setContent(text);
            setDraft(text);
            setRevisions(await fileApi.revisions(agentId, activePath).catch(() => []));
            setPreviewState('ready');
        } catch (err: any) {
            setPreview(null);
            setContent('');
            setDraft('');
            setRevisions([]);
            setPreviewState(err?.status === 404 ? 'deleted' : 'idle');
        }
    };

    // Load only the root level on mount / refresh. Subdirectories are fetched
    // lazily by `expandDir` when the user clicks a directory's chevron — this
    // avoids the prior behavior of recursively walking up to 4 levels deep on
    // first render, which produced thousands of file-list requests for large
    // repos cloned into workspace/.
    const loadFileTree = async () => {
        const rootPath = treeScope === 'workspace' ? WORKSPACE_ROOT : '';
        const items = await fileApi.list(agentId, rootPath).catch(() => []);
        setFileTree(items);
        setLoadedDirs(new Set([rootPath]));
        setLoadingDirs(new Set());
    };

    // Update a node in the (possibly nested) fileTree by path, attaching the
    // freshly-fetched children. Immutable update so React state diff'ing fires.
    const attachChildrenAt = (
        nodes: WorkspaceFileNode[],
        targetPath: string,
        children: WorkspaceFileNode[],
    ): WorkspaceFileNode[] => nodes.map((node) => {
        if (node.path === targetPath) return { ...node, children };
        if (node.is_dir && node.children) {
            return { ...node, children: attachChildrenAt(node.children, targetPath, children) };
        }
        return node;
    });

    // Lazy-load a single directory's immediate children. Idempotent: a second
    // call for the same path while a fetch is in flight is a no-op, and once a
    // directory is in `loadedDirs` we never refetch (use a manual refresh to
    // pick up new files).
    const expandDir = async (dirPath: string) => {
        if (loadedDirs.has(dirPath) || loadingDirs.has(dirPath)) return;
        setLoadingDirs((prev) => new Set(prev).add(dirPath));
        try {
            const children = await fileApi.list(agentId, dirPath).catch(() => []);
            setFileTree((tree) => attachChildrenAt(tree, dirPath, children));
            setLoadedDirs((prev) => new Set(prev).add(dirPath));
        } finally {
            setLoadingDirs((prev) => {
                const next = new Set(prev);
                next.delete(dirPath);
                return next;
            });
        }
    };

    useEffect(() => {
        if (activePath !== prevActivePathRef.current) {
            prevActivePathRef.current = activePath ?? null;
            manualTreeScopeRef.current = null;
            setEditing(false);
            onEditingChange?.(false);
        }
        if (!activePath) {
            setPreview(null);
            setContent('');
            setDraft('');
            setRevisions([]);
            setPreviewState('idle');
            return;
        }
        if (liveDraft && (!activePath || !liveDraft.path || liveDraft.path === activePath)) {
            setPreview(null);
            setContent(liveDraft.content || '');
            setDraft(liveDraft.content || '');
            setPreviewState('ready');
            return;
        }
        void load();
    }, [agentId, activePath, liveDraft?.id, liveDraft?.path, liveDraft?.content, onEditingChange]);

    useEffect(() => {
        if (!shouldRenderLiveDraft) return;
        if (!previewShouldFollowRef.current) return;
        const scrollToLatest = () => {
            const el = previewScrollRef.current;
            if (!el) return;
            el.scrollTop = el.scrollHeight;
        };
        requestAnimationFrame(scrollToLatest);
        const timer = window.setTimeout(scrollToLatest, 120);
        return () => window.clearTimeout(timer);
    }, [shouldRenderLiveDraft, liveDraftContent]);

    useEffect(() => {
        previewShouldFollowRef.current = true;
    }, [liveDraft?.id, liveDraft?.path]);

    const handlePreviewScroll = () => {
        const el = previewScrollRef.current;
        if (!el || !shouldRenderLiveDraft) return;
        const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
        previewShouldFollowRef.current = distFromBottom < 80;
    };

    useEffect(() => {
        if (!activePath) return;
        const latestActivity = activities.find((item) => item.path === activePath);
        if (latestActivity?.action !== 'delete' || latestActivity.ok === false || latestActivity.pendingApproval) return;
        setEditing(false);
        onEditingChange?.(false);
        setPreview(null);
        setContent('');
        setDraft('');
        setRevisions([]);
        setPreviewState('deleted');
    }, [activities, activePath, onEditingChange]);

    useEffect(() => {
        if (!activePath) return undefined;
        const latestActivity = activities.find((item) => item.path === activePath);
        if (latestActivity?.action !== 'delete' || !latestActivity.pendingApproval) return undefined;

        let cancelled = false;
        const pollForDeletion = async () => {
            try {
                await fileApi.preview(agentId, activePath);
            } catch (err: any) {
                if (cancelled || err?.status !== 404) return;
                setEditing(false);
                onEditingChange?.(false);
                setPreview(null);
                setContent('');
                setDraft('');
                setRevisions([]);
                setPreviewState('deleted');
                onPathDeleted?.(activePath);
                void loadFileTree();
            }
        };

        void pollForDeletion();
        const timer = window.setInterval(() => {
            void pollForDeletion();
        }, 4000);

        return () => {
            cancelled = true;
            window.clearInterval(timer);
        };
    }, [activities, activePath, agentId, onEditingChange, onPathDeleted]);

    useEffect(() => {
        loadFileTree();
    }, [agentId, activityKey, liveDraft?.path, treeScope]);

    useEffect(() => {
        if (!activePath || treeScope !== 'workspace') return;
        if (isWorkspacePath(activePath)) return;
        if (manualTreeScopeRef.current === 'workspace') return;
        setTreeScope('all');
    }, [activePath, treeScope]);

    useEffect(() => {
        const pathToReveal = activePath || liveDraft?.path;
        if (treeScope === 'workspace' && pathToReveal && !isWorkspacePath(pathToReveal)) return;
        if (suppressNextAutoRevealRef.current) {
            suppressNextAutoRevealRef.current = false;
            return;
        }
        const dirs = parentDirs(pathToReveal);
        setExpandedDirs((prev) => {
            const next = new Set(prev);
            dirs.forEach((dir) => next.add(dir));
            return next;
        });
    }, [activePath, liveDraft?.path, treeScope]);

    useEffect(() => {
        if (treeScope !== 'all') return;
        if (isWorkspacePath(activePath || liveDraft?.path)) return;
        setExpandedDirs((prev) => {
            if (!Array.from(prev).some((dir) => isWorkspacePath(dir))) return prev;
            return removeWorkspaceExpansion(prev);
        });
    }, [activePath, liveDraft?.path, treeScope]);

    useEffect(() => {
        if (activePath) {
            setSelectedDirPath(directoryOf(activePath));
        }
    }, [activePath]);

    useEffect(() => {
        setSideWidth((prev) => {
            const base = activityOpen ? DEFAULT_HISTORY_WIDTH : DEFAULT_TREE_WIDTH;
            if (prev < MIN_SIDE_WIDTH || prev > MAX_SIDE_WIDTH) return base;
            return activityOpen ? Math.max(prev, DEFAULT_HISTORY_WIDTH) : prev;
        });
    }, [activityOpen]);

    useEffect(() => {
        onEditingChange?.(editing);
        if (!activePath || !editing) return;
        fileApi.lock(agentId, activePath, sessionId).catch(() => {});
        lockTimer.current = setInterval(() => {
            fileApi.lock(agentId, activePath, sessionId).catch(() => {});
        }, 30_000);
        return () => {
            if (lockTimer.current) clearInterval(lockTimer.current);
            fileApi.unlock(agentId, activePath).catch(() => {});
        };
    }, [agentId, activePath, editing, sessionId]);

    const clearSaveStateTimer = () => {
        if (saveStateTimer.current) {
            clearTimeout(saveStateTimer.current);
            saveStateTimer.current = null;
        }
    };

    const runAutosaveWithFeedback = async (nextContent: string) => {
        if (!activePath) return;
        clearSaveStateTimer();
        const startedAt = Date.now();
        setSaveState('saving');
        try {
            await fileApi.autosave(agentId, activePath, nextContent, sessionId);
            const remainingSavingMs = Math.max(0, MIN_SAVING_VISIBLE_MS - (Date.now() - startedAt));
            await new Promise((resolve) => setTimeout(resolve, remainingSavingMs));
            setContent(nextContent);
            setSaveState('saved');
            setRevisions(await fileApi.revisions(agentId, activePath).catch(() => []));
            clearSaveStateTimer();
            saveStateTimer.current = setTimeout(() => {
                setSaveState('idle');
                saveStateTimer.current = null;
            }, SAVED_VISIBLE_MS);
        } catch {
            setSaveState('error');
        }
    };

    useEffect(() => {
        return () => clearSaveStateTimer();
    }, []);

    useEffect(() => {
        if (!editing || !activePath || draft === content) return;
        if (saveTimer.current) clearTimeout(saveTimer.current);
        saveTimer.current = setTimeout(async () => {
            await runAutosaveWithFeedback(draft);
        }, 900);
        return () => {
            if (saveTimer.current) clearTimeout(saveTimer.current);
        };
    }, [agentId, activePath, draft, content, editing, sessionId]);

    const previewType = preview?.type || preview?.kind;
    const htmlPreviewSrc = useMemo(() => {
        if (!activePath || !isHtml || editing || shouldRenderLiveDraft) return '';
        const base = fileApi.downloadUrl(agentId, activePath, { inline: true });
        const version = preview?.content_hash || buildPreviewVersion(content || '');
        return `${base}&v=${encodeURIComponent(version)}`;
    }, [activePath, agentId, isHtml, editing, preview?.content_hash, content, shouldRenderLiveDraft]);

    const csvRows = useMemo(() => {
        if (previewType === 'csv') return parseCsv(editing ? draft : content).slice(0, 200).map(trimTrailingEmpty).filter((row) => row.length);
        return [];
    }, [previewType, content, draft, editing]);

    const xlsxRows = previewType === 'xlsx' ? (preview.sheets?.[0]?.rows || []).map(trimTrailingEmpty).filter((row: string[]) => row.length) : [];

    const finishEditing = async () => {
        if (saveTimer.current) clearTimeout(saveTimer.current);
        if (activePath && !canModifyPath(activePath)) {
            setDraft(content);
            setEditing(false);
            onEditingChange?.(false);
            return;
        }
        if (activePath && draft !== content) {
            await runAutosaveWithFeedback(draft);
        }
        setEditing(false);
        onEditingChange?.(false);
        if (activePath) {
            await fileApi.unlock(agentId, activePath).catch(() => {});
            setRevisions(await fileApi.revisions(agentId, activePath).catch(() => []));
        }
    };

    const discardEditing = async () => {
        if (saveTimer.current) clearTimeout(saveTimer.current);
        setDraft(content);
        setEditing(false);
        onEditingChange?.(false);
        if (activePath) {
            await fileApi.unlock(agentId, activePath).catch(() => {});
        }
    };

    const switchToPath = (path: string) => {
        if (path === activePath) return;
        if (!editing) {
            suppressNextAutoRevealRef.current = true;
            onSelectPath(path);
            return;
        }
        setPendingSwitchPath(path);
    };

    const saveAndSwitch = async () => {
        if (!pendingSwitchPath) return;
        const nextPath = pendingSwitchPath;
        setPendingSwitchPath(null);
        await finishEditing();
        suppressNextAutoRevealRef.current = true;
        onSelectPath(nextPath);
    };

    const discardAndSwitch = async () => {
        if (!pendingSwitchPath) return;
        const nextPath = pendingSwitchPath;
        setPendingSwitchPath(null);
        await discardEditing();
        suppressNextAutoRevealRef.current = true;
        onSelectPath(nextPath);
    };

    const restore = async (revisionId: string) => {
        if (!activePath) return;
        await fileApi.restoreRevision(agentId, revisionId);
        await load();
    };

    const handleUploadClick = () => {
        fileInputRef.current?.click();
    };

    const switchTreeScope = (scope: TreeScope) => {
        manualTreeScopeRef.current = scope;
        setTreeScope(scope);
        if (scope === 'workspace') {
            setSelectedDirPath(WORKSPACE_ROOT);
            setExpandedDirs((prev) => {
                if (isWorkspacePath(activePath || liveDraft?.path)) return prev;
                return removeWorkspaceExpansion(prev);
            });
        }
    };

    const handleUploadFiles = async (files: FileList | null) => {
        if (!files?.length) return;
        const selectedFiles = Array.from(files);
        for (const file of selectedFiles) {
            const itemId = `${treeTargetDir}:${file.name}:${Date.now()}:${Math.random().toString(16).slice(2)}`;
            setExpandedDirs((prev) => {
                const next = new Set(prev);
                parentDirs(treeTargetDir).forEach((dir) => next.add(dir));
                next.add(treeTargetDir);
                return next;
            });
            setUploadItems((prev) => [...prev, {
                id: itemId,
                name: file.name,
                dir: treeTargetDir,
                progress: 0,
                status: 'uploading',
            }]);
            try {
                const { promise } = uploadFileWithProgress(
                    `/agents/${agentId}/files/upload?path=${encodeURIComponent(treeTargetDir)}`,
                    file,
                    (pct) => {
                        setUploadItems((prev) => prev.map((item) => item.id === itemId
                            ? {
                                ...item,
                                status: pct > 100 ? 'processing' : 'uploading',
                                progress: pct > 100 ? 100 : pct,
                            }
                            : item));
                    },
                );
                await promise;
                setUploadItems((prev) => prev.map((item) => item.id === itemId
                    ? { ...item, status: 'done', progress: 100 }
                    : item));
                await loadFileTree();
                window.setTimeout(() => {
                    setUploadItems((prev) => prev.filter((item) => item.id !== itemId));
                }, 900);
            } catch (err: any) {
                setUploadItems((prev) => prev.map((item) => item.id === itemId
                    ? { ...item, status: 'error', error: err?.message || 'Upload failed' }
                    : item));
            }
        }
    };

    const handleCreateFolder = async () => {
        if (!canModifyPath(treeTargetDir)) return;
        setCreateFolderModalOpen(true);
    };

    const confirmCreateFolder = async (name: string) => {
        const trimmed = name.trim().replace(/^\/+|\/+$/g, '');
        setCreateFolderModalOpen(false);
        if (!trimmed) return;
        const folderPath = `${treeTargetDir}/${trimmed}`;
        await fileApi.write(agentId, `${folderPath}/.gitkeep`, '');
        setSelectedDirPath(folderPath);
        setExpandedDirs((prev) => {
            const next = new Set(prev);
            parentDirs(folderPath).forEach((dir) => next.add(dir));
            next.add(folderPath);
            next.add(treeTargetDir);
            return next;
        });
        await loadFileTree();
    };

    const deleteTreePath = async (path: string, label: string, selected?: boolean) => {
        if (!canModifyPath(path)) return;
        const ok = await dialog.confirm(
            t('agent.workspace.confirmDelete', 'Are you sure you want to delete {{name}}?', { name: label }),
            { title: t('common.delete', 'Delete'), danger: true, confirmLabel: t('common.delete', 'Delete') },
        );
        if (!ok) return;
        try {
            await fileApi.delete(agentId, path);
            if (selected) {
                setEditing(false);
                onEditingChange?.(false);
                setPreview(null);
                setContent('');
                setDraft('');
                setRevisions([]);
                setPreviewState('deleted');
            }
            if (selectedDirPath === path || selectedDirPath.startsWith(`${path}/`)) {
                setSelectedDirPath(WORKSPACE_ROOT);
            }
            onPathDeleted?.(path);
            await loadFileTree();
        } catch (err: any) {
            await dialog.alert(t('agent.workspace.deleteFailed', 'Failed to delete'), {
                type: 'error',
                details: String(err?.message || err),
            });
        }
    };

    const startResize = (event: ReactMouseEvent<HTMLDivElement>) => {
        event.preventDefault();
        setIsSideResizing(true);
        resizeRef.current = { startX: event.clientX, startWidth: panelSideWidth };
        const onMove = (moveEvent: MouseEvent) => {
            const next = resizeRef.current
                ? resizeRef.current.startWidth + (resizeRef.current.startX - moveEvent.clientX)
                : panelSideWidth;
            setSideWidth(Math.max(MIN_SIDE_WIDTH, Math.min(MAX_SIDE_WIDTH, next)));
        };
        const onUp = () => {
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
            resizeRef.current = null;
            setIsSideResizing(false);
        };
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
    };

    const openInNewTabLabel = t('agent.workspace.openInNewTab', 'Open in new tab');
    const focusPreviewLabel = locked
        ? t('agent.workspace.unfocusPreview', 'Exit focus')
        : t('agent.workspace.focusPreview', 'Focus preview');
    const editLabel = t('agent.workspace.edit', 'Edit');
    const doneLabel = t('agent.workspace.done', 'Done');
    const fileTreeActions = (
        <div className="workspace-op-tree-primary-actions">
            {activePath && (
                <a
                    className="workspace-op-tree-action-btn"
                    href={fileApi.downloadUrl(agentId, activePath)}
                    download
                    title={`Download ${fileName(activePath)}`}
                    aria-label={`Download ${fileName(activePath)}`}
                >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <path d="M12 3v10" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
                        <path d="M8 10l4 4 4-4" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
                        <path d="M5 18h14" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
                    </svg>
                </a>
            )}
            <button
                className="workspace-op-tree-action-btn"
                type="button"
                onClick={handleUploadClick}
                title={`Upload into ${treeTargetDir}`}
                aria-label={`Upload into ${treeTargetDir}`}
            >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path d="M12 16V5" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
                    <path d="M8 9l4-4 4 4" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
                    <path d="M5 19h14" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
                </svg>
            </button>
            <button
                className="workspace-op-tree-action-btn"
                type="button"
                onClick={handleCreateFolder}
                title={`Create folder in ${treeTargetDir}`}
                aria-label={`Create folder in ${treeTargetDir}`}
            >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path d="M4 8.5A2.5 2.5 0 016.5 6H10l1.4 1.6H17.5A2.5 2.5 0 0120 10.1v6.4A2.5 2.5 0 0117.5 19h-11A2.5 2.5 0 014 16.5v-8Z" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
                    <path d="M12 10.5v5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                    <path d="M9.5 13h5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                </svg>
            </button>
        </div>
    );

    const hasHeaderActions = saveState !== 'idle' || !!(activePath && (canEdit || onToggleLock || isHtml));
    const headerActionsPortal = headerActionsTarget && hasHeaderActions
        ? createPortal(
            <HeaderActions
                activePath={activePath}
                agentId={agentId}
                canEdit={canEdit}
                doneLabel={doneLabel}
                editLabel={editLabel}
                editing={editing}
                finishEditing={finishEditing}
                focusPreviewLabel={focusPreviewLabel}
                htmlPreviewSrc={htmlPreviewSrc}
                isHtml={isHtml}
                locked={locked}
                onToggleLock={onToggleLock}
                openInNewTabLabel={openInNewTabLabel}
                saveState={saveState}
                setEditing={setEditing}
                shouldRenderLiveDraft={shouldRenderLiveDraft}
            />,
            headerActionsTarget,
        )
        : null;

    return (
        <div className="workspace-op">
            {headerActionsPortal}

            <div
                className={`workspace-op-body ${activityOpen ? 'activity-open' : ''} ${treeOpen ? '' : 'tree-closed'}`}
                style={treeOpen || activityOpen ? {
                    gridTemplateColumns: `minmax(0, 1fr) ${panelSideWidth}px`,
                    ['--workspace-side-width' as any]: `${panelSideWidth}px`,
                } : undefined}
            >
                <div className="workspace-op-main" ref={previewScrollRef} onScroll={handlePreviewScroll}>
                    <PreviewContent
                        activePath={activePath}
                        agentId={agentId}
                        content={content}
                        csvRows={csvRows}
                        draft={draft}
                        editing={editing}
                        ext={ext}
                        htmlPreviewSrc={htmlPreviewSrc}
                        isHtml={isHtml}
                        isImage={isImage}
                        isSideResizing={isSideResizing}
                        liveDraft={liveDraft}
                        preview={preview}
                        previewState={previewState}
                        previewType={previewType}
                        setDraft={setDraft}
                        shouldRenderLiveDraft={shouldRenderLiveDraft}
                        xlsxRows={xlsxRows}
                    />
                </div>
                <SidePanel
                    activePath={activePath}
                    activityOpen={activityOpen}
                    canModifyPath={canModifyPath}
                    deleteTreePath={deleteTreePath}
                    editing={editing}
                    expandDir={expandDir}
                    expandedDirs={expandedDirs}
                    fileInputRef={fileInputRef}
                    fileTree={fileTree}
                    fileTreeActions={fileTreeActions}
                    handleUploadFiles={handleUploadFiles}
                    isWritableTreeDir={isWritableTreeDir}
                    loadDirs={loadingDirs}
                    restore={restore}
                    revisions={revisions}
                    selectedDirPath={selectedDirPath}
                    setActivityOpen={setActivityOpen}
                    setExpandedDirs={setExpandedDirs}
                    setSelectedDirPath={setSelectedDirPath}
                    setTreeOpen={setTreeOpen}
                    startResize={startResize}
                    switchToPath={switchToPath}
                    switchTreeScope={switchTreeScope}
                    treeOpen={treeOpen}
                    treeScope={treeScope}
                    uploadItems={uploadItems}
                    workspaceLabel={t('agent.workspace.title', 'Workspace')}
                    allLabel={t('common.all', 'All')}
                />
            </div>
            {pendingSwitchPath && (
                <div className="workspace-op-modal-overlay" onClick={() => setPendingSwitchPath(null)}>
                    <div className="workspace-op-modal" onClick={(e) => e.stopPropagation()}>
                        <div className="workspace-op-modal-title">Switch files?</div>
                        <div className="workspace-op-modal-text">
                            You are editing <strong>{activePath ? fileName(activePath) : 'the current file'}</strong>.
                            {' '}Choose how to handle your changes before opening <strong>{fileName(pendingSwitchPath)}</strong>.
                        </div>
                        <div className="workspace-op-modal-actions">
                            <button className="btn btn-secondary" onClick={() => setPendingSwitchPath(null)}>Stay Here</button>
                            <button className="btn btn-secondary" onClick={discardAndSwitch}>Discard & Switch</button>
                            <button className="btn btn-primary" onClick={saveAndSwitch}>Save & Switch</button>
                        </div>
                    </div>
                </div>
            )}
            <PromptModal
                open={createFolderModalOpen}
                title={t('agent.workspace.newFolder', 'New Folder')}
                placeholder={t('agent.workspace.newFolderName', 'Folder name')}
                onCancel={() => setCreateFolderModalOpen(false)}
                onConfirm={(name) => void confirmCreateFolder(name)}
            />
        </div>
    );
}
