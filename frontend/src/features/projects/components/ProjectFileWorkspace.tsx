import {
    IconAlertTriangle,
    IconBrandGit,
    IconChevronRight,
    IconCode,
    IconDeviceFloppy,
    IconDownload,
    IconFile,
    IconFileCode,
    IconFileUnknown,
    IconFolder,
    IconFolderOpen,
    IconLoader2,
    IconMusic,
    IconPhoto,
    IconPlayerPlay,
    IconPlus,
    IconRefresh,
} from '@tabler/icons-react';
import { lazy, Suspense, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';

import { projectsApi, type ProjectFileContent, type ProjectFileKind } from '../../../services/projects';
import { formatFileSize } from '../../../utils/formatFileSize';
import { Button, TextInput } from './ProjectUI';
import './ProjectFileWorkspace.css';

const ProjectCodeEditor = lazy(() => import('./ProjectCodeEditor'));

type RecordValue = Record<string, unknown>;

type Props = {
    projectId: string;
    files: RecordValue[];
    runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>;
    busyAction: string;
};

type TreeNode = {
    name: string;
    path: string;
    kind: 'folder' | 'file';
    children: TreeNode[];
    entry?: RecordValue;
};

const text = (source: RecordValue, ...keys: string[]) => {
    for (const key of keys) {
        const value = source[key];
        if (typeof value === 'string' || typeof value === 'number') return String(value);
    }
    return '';
};

const number = (source: RecordValue, ...keys: string[]) => {
    for (const key of keys) {
        const value = Number(source[key]);
        if (Number.isFinite(value)) return value;
    }
    return 0;
};

const filePath = (entry: RecordValue) => text(entry, 'path', 'id');

function treeFromFiles(files: RecordValue[]): TreeNode[] {
    const root: TreeNode = { name: '', path: '', kind: 'folder', children: [] };
    for (const entry of files) {
        const path = filePath(entry).replace(/^\/+|\/+$/g, '');
        if (!path) continue;
        const segments = path.split('/').filter(Boolean);
        let parent = root;
        segments.forEach((segment, index) => {
            const currentPath = segments.slice(0, index + 1).join('/');
            const isFile = index === segments.length - 1;
            let child = parent.children.find((candidate) => candidate.name === segment && candidate.kind === (isFile ? 'file' : 'folder'));
            if (!child) {
                child = { name: segment, path: currentPath, kind: isFile ? 'file' : 'folder', children: [], entry: isFile ? entry : undefined };
                parent.children.push(child);
            } else if (isFile) {
                child.entry = entry;
            }
            parent = child;
        });
    }
    const sort = (nodes: TreeNode[]) => {
        nodes.sort((left, right) => left.kind === right.kind
            ? left.name.localeCompare(right.name, 'zh-CN', { numeric: true, sensitivity: 'base' })
            : left.kind === 'folder' ? -1 : 1);
        nodes.forEach((node) => sort(node.children));
    };
    sort(root.children);
    return root.children;
}

function folderPaths(nodes: TreeNode[]): string[] {
    return nodes.flatMap((node) => node.kind === 'folder' ? [node.path, ...folderPaths(node.children)] : []);
}

function kindOf(entry: RecordValue | undefined): ProjectFileKind {
    const kind = text(entry || {}, 'kind');
    return ['text', 'image', 'video', 'audio', 'binary'].includes(kind) ? kind as ProjectFileKind : 'binary';
}

function FileKindIcon({ kind, size = 16 }: { kind: ProjectFileKind; size?: number }) {
    if (kind === 'image') return <IconPhoto size={size} />;
    if (kind === 'video') return <IconPlayerPlay size={size} />;
    if (kind === 'audio') return <IconMusic size={size} />;
    if (kind === 'text') return <IconFileCode size={size} />;
    return <IconFile size={size} />;
}

function TreeRows({
    nodes,
    depth,
    expanded,
    selectedPath,
    onToggle,
    onSelect,
}: {
    nodes: TreeNode[];
    depth: number;
    expanded: Set<string>;
    selectedPath: string;
    onToggle: (path: string) => void;
    onSelect: (path: string) => void;
}) {
    return <>{nodes.map((node) => {
        const isFolder = node.kind === 'folder';
        const isExpanded = isFolder && expanded.has(node.path);
        return <div key={`${node.kind}:${node.path}`} role="treeitem" aria-expanded={isFolder ? isExpanded : undefined}>
            <button
                type="button"
                className={['project-file-workspace__tree-row', node.path === selectedPath ? 'is-active' : ''].filter(Boolean).join(' ')}
                style={{ paddingInlineStart: `${8 + depth * 16}px` }}
                title={node.path}
                onClick={() => isFolder ? onToggle(node.path) : onSelect(node.path)}
            >
                {isFolder
                    ? <><IconChevronRight className={isExpanded ? 'is-expanded' : ''} size={14} /><span className="project-file-workspace__folder-icon">{isExpanded ? <IconFolderOpen size={16} /> : <IconFolder size={16} />}</span></>
                    : <><span className="project-file-workspace__tree-spacer" /><FileKindIcon kind={kindOf(node.entry)} /></>}
                <span>{node.name}</span>
            </button>
            {isFolder && isExpanded ? <div role="group"><TreeRows nodes={node.children} depth={depth + 1} expanded={expanded} selectedPath={selectedPath} onToggle={onToggle} onSelect={onSelect} /></div> : null}
        </div>;
    })}</>;
}

function MetaLine({ content }: { content: ProjectFileContent }) {
    return (
        <div className="project-file-workspace__meta" aria-label="文件元数据">
            <span>{formatFileSize(content.size) || '0 B'}</span>
            <span>{content.mime_type || 'application/octet-stream'}</span>
            <span title={content.commit}>{content.commit ? `Commit ${content.commit.slice(0, 10)}` : '未提交'}</span>
        </div>
    );
}

function MediaPreview({ content, onUnavailable }: { content: ProjectFileContent; onUnavailable: () => void }) {
    if (content.kind === 'image') {
        return <div className="project-file-workspace__image-stage"><img src={content.raw_url} alt={content.name} onError={onUnavailable} /></div>;
    }
    if (content.kind === 'video') {
        return <div className="project-file-workspace__media-stage"><video key={content.raw_url} src={content.raw_url} controls playsInline preload="metadata" onError={onUnavailable} /></div>;
    }
    if (content.kind === 'audio') {
        return <div className="project-file-workspace__media-stage project-file-workspace__media-stage--audio"><div className="project-file-workspace__audio-art"><IconMusic size={34} /></div><strong>{content.name}</strong><audio key={content.raw_url} src={content.raw_url} controls preload="metadata" onError={onUnavailable} /></div>;
    }
    return (
        <div className="project-file-workspace__binary-state">
            <span><IconFileUnknown size={30} /></span>
            <h3>此文件不能在线预览</h3>
            <p>文件保持原样存储在项目 Git 仓库中，可下载到本地使用对应应用打开。</p>
            <a className="btn btn-secondary" href={content.download_url} download={content.name}><IconDownload size={16} />下载文件</a>
        </div>
    );
}

export default function ProjectFileWorkspace({ projectId, files, runAction, busyAction }: Props) {
    const firstPath = filePath(files[0] || {});
    const [selectedPath, setSelectedPath] = useState(firstPath);
    const [creating, setCreating] = useState(!firstPath);
    const [draftPath, setDraftPath] = useState(firstPath);
    const [content, setContent] = useState<ProjectFileContent | null>(null);
    const [draftContent, setDraftContent] = useState('');
    const [loading, setLoading] = useState(Boolean(firstPath));
    const [loadError, setLoadError] = useState('');
    const [reloadKey, setReloadKey] = useState(0);
    const tree = useMemo(() => treeFromFiles(files), [files]);
    const allFolders = useMemo(() => folderPaths(tree), [tree]);
    const knownFoldersRef = useRef(new Set(allFolders));
    const mediaRecoveryPathRef = useRef('');
    const [expanded, setExpanded] = useState<Set<string>>(() => new Set(allFolders));

    useEffect(() => {
        const nextFolders = allFolders.filter((path) => !knownFoldersRef.current.has(path));
        if (nextFolders.length) setExpanded((current) => new Set([...current, ...nextFolders]));
        knownFoldersRef.current = new Set(allFolders);
    }, [allFolders]);

    useEffect(() => {
        if (creating) return;
        if (selectedPath && files.some((entry) => filePath(entry) === selectedPath)) return;
        const next = filePath(files[0] || {});
        setSelectedPath(next);
        setDraftPath(next);
        if (!next) setCreating(true);
    }, [creating, files, selectedPath]);

    useEffect(() => {
        if (creating || !selectedPath) return;
        let disposed = false;
        setLoading(true);
        setLoadError('');
        setContent(null);
        void projectsApi.getFileContent(projectId, selectedPath).then((next) => {
            if (disposed) return;
            setContent(next);
            setDraftPath(next.path);
            setDraftContent(next.content || '');
        }).catch((error: unknown) => {
            if (disposed) return;
            setLoadError(error instanceof Error ? error.message : '文件加载失败');
        }).finally(() => {
            if (!disposed) setLoading(false);
        });
        return () => { disposed = true; };
    }, [creating, projectId, reloadKey, selectedPath]);

    useEffect(() => {
        mediaRecoveryPathRef.current = '';
    }, [selectedPath]);

    const selectFile = (path: string) => {
        setCreating(false);
        setSelectedPath(path);
        setDraftPath(path);
        setLoadError('');
        const segments = path.split('/');
        setExpanded((current) => new Set([...current, ...segments.slice(0, -1).map((_, index) => segments.slice(0, index + 1).join('/'))]));
    };

    const startNewFile = () => {
        setCreating(true);
        setSelectedPath('');
        setDraftPath('');
        setDraftContent('');
        setContent(null);
        setLoadError('');
    };

    const save = async (event: FormEvent) => {
        event.preventDefault();
        const path = draftPath.trim().replace(/^\/+/, '');
        if (!path) return;
        const ok = await runAction('save-file', () => projectsApi.writeFile(projectId, { path, content: draftContent }), '文件已保存并创建 Git 提交');
        if (ok) {
            setCreating(false);
            setSelectedPath(path);
            setDraftPath(path);
            setReloadKey((value) => value + 1);
        }
    };

    const textReadOnly = Boolean(content && (!content.is_editable || content.truncated));
    const canEditText = creating || Boolean(content?.is_text);
    const activeEntry = files.find((entry) => filePath(entry) === selectedPath);
    const refreshMediaTicket = () => {
        if (!selectedPath || mediaRecoveryPathRef.current === selectedPath) return;
        mediaRecoveryPathRef.current = selectedPath;
        setReloadKey((value) => value + 1);
    };

    return (
        <div className="project-file-workspace">
            <aside className="project-file-workspace__sidebar" aria-label="项目目录">
                <header>
                    <IconBrandGit size={16} />
                    <span title="项目工作树">项目工作树</span>
                    <em>{files.length}</em>
                    <Button type="button" variant="ghost" onClick={startNewFile} title="新建文件" aria-label="新建文件"><IconPlus size={15} /></Button>
                </header>
                <div className="project-file-workspace__tree" role="tree" aria-label="项目文件树">
                    {tree.length ? <TreeRows nodes={tree} depth={0} expanded={expanded} selectedPath={selectedPath} onToggle={(path) => setExpanded((current) => { const next = new Set(current); if (next.has(path)) next.delete(path); else next.add(path); return next; })} onSelect={selectFile} /> : <div className="project-file-workspace__tree-empty"><IconFolder size={22} /><span>暂无项目文件</span></div>}
                </div>
            </aside>

            <section className="project-file-workspace__viewer">
                <header className="project-file-workspace__viewer-header">
                    <div>
                        <span>{creating ? 'NEW FILE' : content?.is_text ? 'EDIT FILE' : 'FILE PREVIEW'}</span>
                        {creating ? <TextInput form="project-file-editor-form" value={draftPath} onChange={(event) => setDraftPath(event.target.value)} placeholder="docs/deliverable.md" aria-label="项目内路径" required autoFocus /> : <h3 title={draftPath || selectedPath}>{draftPath || selectedPath || '创建项目文件'}</h3>}
                    </div>
                    <div className="project-file-workspace__viewer-actions">
                        {content ? <MetaLine content={content} /> : activeEntry ? <span>{formatFileSize(number(activeEntry, 'size'))}</span> : null}
                        {content?.download_url ? <a className="btn btn-ghost" href={content.download_url} download={content.name} title="下载文件" aria-label={`下载 ${content.name}`}><IconDownload size={17} /></a> : null}
                        {canEditText && !textReadOnly ? <Button type="submit" form="project-file-editor-form" variant="primary" disabled={!draftPath.trim() || busyAction === 'save-file'}>{busyAction === 'save-file' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconDeviceFloppy size={16} />}保存并提交</Button> : null}
                    </div>
                </header>

                {loading ? <div className="project-file-workspace__state"><IconLoader2 className="project-workspace__spinner" size={24} /><span>正在读取 Git HEAD 文件…</span></div> : loadError ? <div className="project-file-workspace__state is-error"><IconAlertTriangle size={24} /><strong>文件加载失败</strong><span>{loadError}</span><Button variant="secondary" onClick={() => setReloadKey((value) => value + 1)}><IconRefresh size={15} />重试</Button></div> : canEditText ? (
                    <form id="project-file-editor-form" className="project-file-workspace__editor" onSubmit={(event) => void save(event)}>
                        {textReadOnly ? <div className="project-file-workspace__readonly-note" role="status"><IconAlertTriangle size={16} /><div><strong>{content?.truncated ? '当前仅显示文件前部内容' : '此文件只允许在线查看'}</strong><p>为避免使用截断内容覆盖原文件，请在 Agent 工作区修改后提交。</p></div></div> : null}
                        <Suspense fallback={<div className="project-file-workspace__editor-loading"><IconLoader2 className="project-workspace__spinner" size={20} /><span>正在载入代码编辑器…</span></div>}>
                            <ProjectCodeEditor path={draftPath} value={draftContent} readOnly={textReadOnly} onChange={setDraftContent} />
                        </Suspense>
                        <footer>
                            <span>{creating ? '保存后会创建普通 Git Commit' : textReadOnly ? '只读预览不会修改项目文件' : '改动仅作用于当前项目仓库'}</span>
                        </footer>
                    </form>
                ) : content ? <div className="project-file-workspace__preview"><MediaPreview content={content} onUnavailable={refreshMediaTicket} /></div> : <div className="project-file-workspace__state"><IconCode size={24} /><span>选择一个文件查看内容</span></div>}
            </section>
        </div>
    );
}
