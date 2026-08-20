import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { createPortal } from 'react-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
    IconArchive,
    IconBuilding,
    IconChevronDown,
    IconChevronRight,
    IconDownload,
    IconFile,
    IconFolder,
    IconFolderOpen,
    IconPackage,
    IconUpload,
    IconWorld,
    IconX,
} from '@tabler/icons-react';

import { useDialog } from '../components/Dialog/DialogProvider';
import MarkdownRenderer from '../components/MarkdownRenderer';
import { useToast } from '../components/Toast/ToastProvider';
import SelectDropdown from '../components/SelectDropdown';
import Button from '../components/ui/Button';
import SearchInput from '../components/ui/SearchInput';
import TextInput from '../components/ui/TextInput';
import { agentApi, fileApi, skillApi, type MarketSkill, type PublishMarketSkillInput } from '../services/api';
import { useAuthStore } from '../stores';
import type { Agent } from '../types';
import './SkillMarket.css';

type MarketTab = 'discover' | 'mine';
type MarketSkillFile = NonNullable<MarketSkill['files']>[number];

type SkillTreeNode = {
    name: string;
    path: string;
    isDirectory: boolean;
    children: SkillTreeNode[];
};

function sortSkillTree(nodes: SkillTreeNode[]): SkillTreeNode[] {
    nodes.sort((left, right) => {
        if (left.isDirectory !== right.isDirectory) return left.isDirectory ? -1 : 1;
        return left.name.localeCompare(right.name);
    });
    nodes.forEach((node) => sortSkillTree(node.children));
    return nodes;
}

function buildSkillTree(files: MarketSkillFile[]): SkillTreeNode[] {
    const roots: SkillTreeNode[] = [];

    files.forEach((file) => {
        const parts = file.path.split('/').filter(Boolean);
        let siblings = roots;
        let currentPath = '';

        parts.forEach((part, index) => {
            currentPath = currentPath ? `${currentPath}/${part}` : part;
            const isDirectory = index < parts.length - 1;
            let node = siblings.find((candidate) => candidate.name === part && candidate.isDirectory === isDirectory);
            if (!node) {
                node = { name: part, path: currentPath, isDirectory, children: [] };
                siblings.push(node);
            }
            siblings = node.children;
        });
    });

    return sortSkillTree(roots);
}

function collectDirectoryPaths(nodes: SkillTreeNode[]): string[] {
    return nodes.flatMap((node) => (
        node.isDirectory ? [node.path, ...collectDirectoryPaths(node.children)] : []
    ));
}

function SkillPreviewDrawer({
    detail,
    loading,
    onClose,
}: {
    detail?: MarketSkill;
    loading: boolean;
    onClose: () => void;
}) {
    const { t } = useTranslation();
    const files = useMemo<MarketSkillFile[]>(() => {
        if (detail?.files?.length) return detail.files;
        if (detail?.skill_md !== undefined) return [{ path: 'SKILL.md', content: detail.skill_md }];
        return [];
    }, [detail?.files, detail?.skill_md]);
    const tree = useMemo(() => buildSkillTree(files), [files]);
    const [selectedPath, setSelectedPath] = useState('');
    const [expandedDirs, setExpandedDirs] = useState<Set<string>>(new Set());
    const selectedFile = files.find((file) => file.path === selectedPath);

    useEffect(() => {
        if (!detail?.id) return;
        setSelectedPath(files.find((file) => file.path === 'SKILL.md')?.path || files[0]?.path || '');
        setExpandedDirs(new Set(collectDirectoryPaths(tree)));
    }, [detail?.id, files, tree]);

    useEffect(() => {
        const previousOverflow = document.body.style.overflow;
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') onClose();
        };
        document.body.style.overflow = 'hidden';
        window.addEventListener('keydown', onKeyDown);
        return () => {
            document.body.style.overflow = previousOverflow;
            window.removeEventListener('keydown', onKeyDown);
        };
    }, [onClose]);

    const toggleDirectory = (path: string) => {
        setExpandedDirs((current) => {
            const next = new Set(current);
            if (next.has(path)) next.delete(path);
            else next.add(path);
            return next;
        });
    };

    const renderNodes = (nodes: SkillTreeNode[], depth = 0) => nodes.map((node) => {
        if (node.isDirectory) {
            const expanded = expandedDirs.has(node.path);
            return (
                <div key={node.path} className="skill-preview-tree-branch">
                    <Button
                        type="button"
                        variant="ghost"
                        className="skill-preview-tree-row is-directory"
                        style={{ paddingLeft: `${12 + depth * 14}px` }}
                        onClick={() => toggleDirectory(node.path)}
                        aria-expanded={expanded}
                    >
                        {expanded ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
                        {expanded ? <IconFolderOpen size={15} /> : <IconFolder size={15} />}
                        <span>{node.name}</span>
                    </Button>
                    {expanded && renderNodes(node.children, depth + 1)}
                </div>
            );
        }

        const selected = node.path === selectedPath;
        return (
            <Button
                key={node.path}
                type="button"
                variant="ghost"
                className={`skill-preview-tree-row is-file${selected ? ' is-selected' : ''}`}
                style={{ paddingLeft: `${30 + depth * 14}px` }}
                onClick={() => setSelectedPath(node.path)}
                aria-pressed={selected}
                title={node.path}
            >
                <IconFile size={14} />
                <span>{node.name}</span>
            </Button>
        );
    });

    if (typeof document === 'undefined') return null;

    return createPortal(
        <div className="skill-preview-overlay" onMouseDown={(event) => {
            if (event.target === event.currentTarget) onClose();
        }}>
            <aside
                className="skill-preview-drawer"
                role="dialog"
                aria-modal="true"
                aria-labelledby="skill-preview-title"
            >
                <header className="skill-preview-header">
                    <div className="skill-preview-heading">
                        <span className="skill-preview-kicker">{t('skillMarket.preview.package')}</span>
                        <h2 id="skill-preview-title">{detail?.name || t('common.loading')}</h2>
                        <div className="skill-preview-meta">
                            <span>{detail?.is_builtin ? t('skillMarket.platformPublisher') : detail?.publisher_name || '—'}</span>
                            <span>{detail?.version ? t('skillMarket.version', { version: detail.version }) : '—'}</span>
                            <span>{t('skillMarket.installCount', { count: detail?.downloads || 0 })}</span>
                        </div>
                    </div>
                    <Button type="button" variant="ghost" onClick={onClose} aria-label={t('common.close')}>
                        <IconX size={18} />
                    </Button>
                </header>

                <div className="skill-preview-body">
                    <nav className="skill-preview-tree" aria-label={t('skillMarket.preview.fileTree')}>
                        <div className="skill-preview-pane-title">
                            <span>{detail?.folder_name || t('skillMarket.preview.files')}</span>
                            <span>{files.length}</span>
                        </div>
                        <div className="skill-preview-tree-scroll">
                            {loading ? (
                                <div className="skill-preview-placeholder">{t('common.loading')}</div>
                            ) : tree.length ? renderNodes(tree) : (
                                <div className="skill-preview-placeholder">{t('skillMarket.preview.noFiles')}</div>
                            )}
                        </div>
                    </nav>

                    <section className="skill-preview-content">
                        <div className="skill-preview-content-head">
                            <IconFile size={14} />
                            <span>{selectedFile?.path || t('skillMarket.preview.selectFile')}</span>
                        </div>
                        <div className="skill-preview-content-scroll">
                            {selectedFile ? (
                                selectedFile.path.toLowerCase().endsWith('.md') ? (
                                    <MarkdownRenderer content={selectedFile.content} className="skill-preview-markdown" />
                                ) : (
                                    <pre className="skill-preview-source"><code>{selectedFile.content}</code></pre>
                                )
                            ) : (
                                <div className="skill-preview-placeholder">{t('skillMarket.preview.selectFile')}</div>
                            )}
                        </div>
                    </section>
                </div>
            </aside>
        </div>,
        document.body,
    );
}

function SkillCard({
    skill,
    mine,
    busy,
    onDetail,
    onInstall,
    onOffline,
}: {
    skill: MarketSkill;
    mine?: boolean;
    busy: boolean;
    onDetail: () => void;
    onInstall: () => void;
    onOffline: () => void;
}) {
    const { t } = useTranslation();
    const published = skill.status === 'published';

    return (
        <article className={`skill-market-card${published ? '' : ' is-offline'}`}>
            <div className="skill-market-folder-tab" aria-hidden />
            <div className="skill-market-card-head">
                <span className="skill-market-glyph" aria-hidden>{skill.icon || '✦'}</span>
                <div className="skill-market-card-heading">
                    <div className="skill-market-card-title-row">
                        <h3>{skill.name}</h3>
                        <span className="skill-market-version">{t('skillMarket.version', { version: skill.version })}</span>
                    </div>
                    <div className="skill-market-publisher">
                        {t('skillMarket.by')} · {skill.is_builtin ? t('skillMarket.platformPublisher') : skill.publisher_name}
                    </div>
                </div>
            </div>
            <p>{skill.description || t('skillMarket.noDescription')}</p>
            <div className="skill-market-card-meta">
                <span>{skill.visibility === 'public' ? <IconWorld size={13} /> : <IconBuilding size={13} />}
                    {skill.visibility === 'public' ? t('skillMarket.public') : t('skillMarket.company')}
                </span>
                <span><IconDownload size={13} />{t('skillMarket.installCount', { count: skill.downloads })}</span>
                <span className="skill-market-category">{skill.category || t('skillMarket.generalCategory')}</span>
            </div>
            <footer>
                <Button variant="ghost" type="button" className="skill-market-detail-button" onClick={onDetail}>
                    {t('skillMarket.viewDetails')} <IconChevronRight size={14} />
                </Button>
                {mine ? (
                    published && <Button variant="secondary" type="button" onClick={onOffline} disabled={busy}>
                        <IconArchive size={14} />{busy ? t('skillMarket.working') : t('skillMarket.takeOffline')}
                    </Button>
                ) : (
                    <Button variant="primary" type="button" onClick={onInstall} disabled={busy}>
                        <IconDownload size={14} />{t('skillMarket.install')}
                    </Button>
                )}
            </footer>
        </article>
    );
}

export default function SkillMarket() {
    const { t } = useTranslation();
    const user = useAuthStore((state) => state.user);
    const toast = useToast();
    const dialog = useDialog();
    const queryClient = useQueryClient();
    const [tab, setTab] = useState<MarketTab>('discover');
    const [searchDraft, setSearchDraft] = useState('');
    const [search, setSearch] = useState('');
    const [detailId, setDetailId] = useState<string | null>(null);
    const [installSkill, setInstallSkill] = useState<MarketSkill | null>(null);
    const [installAgentId, setInstallAgentId] = useState('');
    const [showPublish, setShowPublish] = useState(false);
    const [publishAgentId, setPublishAgentId] = useState('');
    const [publishFolder, setPublishFolder] = useState('');
    const [publishName, setPublishName] = useState('');
    const [publishDescription, setPublishDescription] = useState('');
    const [publishCategory, setPublishCategory] = useState('general');
    const [publishVisibility, setPublishVisibility] = useState<'tenant' | 'public'>('tenant');
    const [busyKey, setBusyKey] = useState('');

    const closeWorkbench = () => {
        setDetailId(null);
        setInstallSkill(null);
        setShowPublish(false);
    };

    const { data: agents = [] } = useQuery({
        queryKey: ['skill-market', 'agents', user?.tenant_id],
        queryFn: () => agentApi.list(user?.tenant_id),
    });
    const manageableAgents = useMemo(
        () => agents.filter((agent: Agent) => agent.access_level === 'manage' || agent.creator_id === user?.id),
        [agents, user?.id],
    );
    const agentOptions = useMemo(
        () => manageableAgents.map((agent) => ({ value: agent.id, label: agent.name })),
        [manageableAgents],
    );

    const { data: marketSkills = [], isLoading: marketLoading } = useQuery({
        queryKey: ['skill-market', 'discover', search],
        queryFn: () => skillApi.market.list(search),
    });
    const { data: mySkills = [], isLoading: mineLoading } = useQuery({
        queryKey: ['skill-market', 'mine'],
        queryFn: skillApi.market.mine,
        enabled: tab === 'mine',
    });
    const { data: detail, isLoading: detailLoading } = useQuery({
        queryKey: ['skill-market', 'detail', detailId],
        queryFn: () => skillApi.market.detail(detailId!),
        enabled: Boolean(detailId),
    });
    const { data: skillFolders = [], isFetching: foldersLoading } = useQuery({
        queryKey: ['skill-market', 'folders', publishAgentId],
        queryFn: async () => {
            try {
                const entries = await fileApi.list(publishAgentId, 'skills');
                return entries.filter((entry: any) => entry.is_dir);
            } catch (error: any) {
                if (error?.status === 404) return [];
                throw error;
            }
        },
        enabled: showPublish && Boolean(publishAgentId),
    });
    const folderOptions = useMemo(
        () => skillFolders.map((folder: any) => ({ value: String(folder.path), label: String(folder.path) })),
        [skillFolders],
    );

    useEffect(() => {
        const firstAgentId = manageableAgents[0]?.id || '';
        if (!installAgentId && firstAgentId) setInstallAgentId(firstAgentId);
        if (!publishAgentId && firstAgentId) setPublishAgentId(firstAgentId);
    }, [manageableAgents, installAgentId, publishAgentId]);

    useEffect(() => {
        setPublishFolder(folderOptions[0]?.value || '');
    }, [publishAgentId, folderOptions]);

    useEffect(() => {
        if (!publishFolder) return;
        const folderName = publishFolder.replace(/^skills\//, '');
        setPublishName(folderName.replace(/[-_]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()));
    }, [publishFolder]);

    const refresh = async () => {
        await Promise.all([
            queryClient.invalidateQueries({ queryKey: ['skill-market', 'discover'] }),
            queryClient.invalidateQueries({ queryKey: ['skill-market', 'mine'] }),
            queryClient.invalidateQueries({ queryKey: ['skill-market', 'detail'] }),
        ]);
    };

    const submitSearch = (event: FormEvent) => {
        event.preventDefault();
        setSearch(searchDraft.trim());
    };

    const openInstall = (skill: MarketSkill) => {
        setDetailId(null);
        setShowPublish(false);
        setInstallSkill(skill);
    };

    const openDetail = (skillId: string) => {
        setInstallSkill(null);
        setShowPublish(false);
        setDetailId(skillId);
    };

    const install = async () => {
        if (!installSkill || !installAgentId) return;
        setBusyKey(`install:${installSkill.id}`);
        try {
            await skillApi.market.install(installAgentId, installSkill.id);
            toast.success(t('skillMarket.toast.installed', { name: installSkill.name }));
            setInstallSkill(null);
            await refresh();
        } catch (error: any) {
            toast.error(t('skillMarket.toast.installFailed'), { details: error?.message || String(error) });
        } finally {
            setBusyKey('');
        }
    };

    const publish = async (event: FormEvent) => {
        event.preventDefault();
        if (!publishAgentId || !publishFolder || !publishName.trim()) return;
        const payload: PublishMarketSkillInput = {
            path: publishFolder,
            name: publishName.trim(),
            description: publishDescription.trim(),
            category: publishCategory.trim() || 'general',
            visibility: publishVisibility,
        };
        setBusyKey('publish');
        try {
            await skillApi.market.publish(publishAgentId, payload);
            toast.success(t('skillMarket.toast.published', { name: payload.name }));
            setShowPublish(false);
            setTab('mine');
            setPublishDescription('');
            await refresh();
        } catch (error: any) {
            toast.error(t('skillMarket.toast.publishFailed'), { details: error?.message || String(error) });
        } finally {
            setBusyKey('');
        }
    };

    const takeOffline = async (skill: MarketSkill) => {
        const confirmed = await dialog.confirm(
            t('skillMarket.offlineConfirm', { name: skill.name }),
            { title: t('skillMarket.offlineTitle'), danger: true },
        );
        if (!confirmed) return;
        setBusyKey(`offline:${skill.id}`);
        try {
            await skillApi.market.offline(skill.id);
            toast.success(t('skillMarket.toast.offlineSuccess'));
            await refresh();
        } catch (error: any) {
            toast.error(t('skillMarket.toast.offlineFailed'), { details: error?.message || String(error) });
        } finally {
            setBusyKey('');
        }
    };

    const skills = tab === 'discover' ? marketSkills : mySkills;
    const loading = tab === 'discover' ? marketLoading : mineLoading;

    return (
        <>
        <main className="skill-market-page">
            <header className="skill-market-page-head">
                <div>
                    <div className="skill-market-eyebrow"><IconPackage size={14} /> {t('skillMarket.eyebrow')}</div>
                    <h1>{t('skillMarket.title')}</h1>
                    <p>{t('skillMarket.subtitle')}</p>
                </div>
                <Button
                    variant="primary"
                    type="button"
                    onClick={() => { setDetailId(null); setInstallSkill(null); setShowPublish(true); }}
                    disabled={!manageableAgents.length}
                >
                    <IconUpload size={15} />{t('skillMarket.publishSkill')}
                </Button>
            </header>

            <div className="skill-market-toolbar">
                <div className="skill-market-tabs" role="tablist">
                    <Button type="button" variant={tab === 'discover' ? 'secondary' : 'ghost'} onClick={() => setTab('discover')}>
                        {t('skillMarket.tabs.market')}
                    </Button>
                    <Button type="button" variant={tab === 'mine' ? 'secondary' : 'ghost'} onClick={() => setTab('mine')}>
                        {t('skillMarket.tabs.mine')}
                    </Button>
                </div>
                {tab === 'discover' && (
                    <form className="skill-market-search" onSubmit={submitSearch}>
                        <SearchInput value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder={t('skillMarket.searchPlaceholder')} aria-label={t('skillMarket.searchAria')} />
                        <Button type="submit" variant="secondary">{t('common.search')}</Button>
                        {search && <Button type="button" variant="ghost" onClick={() => { setSearchDraft(''); setSearch(''); }}>{t('common.reset')}</Button>}
                    </form>
                )}
            </div>

            {(installSkill || showPublish) && (
                <section className="skill-market-workbench" aria-live="polite">
                    <div className="skill-market-workbench-head">
                        <div>
                            <span>{showPublish ? t('skillMarket.publish') : t('skillMarket.install')}</span>
                            <h2>{showPublish ? t('skillMarket.publishFromAgent') : installSkill?.name}</h2>
                        </div>
                        <Button type="button" variant="ghost" onClick={closeWorkbench} aria-label={t('common.close')}><IconX size={17} /></Button>
                    </div>

                    {installSkill && (
                        <div className="skill-market-form-body">
                            <label>{t('skillMarket.targetAgent')}
                                {installAgentId && <SelectDropdown value={installAgentId} options={agentOptions} onChange={setInstallAgentId} ariaLabel={t('skillMarket.targetAgent')} />}
                            </label>
                            <p className="skill-market-form-note">{t('skillMarket.installNote', { folder: installSkill.folder_name })}</p>
                            <div className="skill-market-form-actions">
                                <Button variant="secondary" type="button" onClick={closeWorkbench}>{t('common.cancel')}</Button>
                                <Button variant="primary" type="button" onClick={install} disabled={!installAgentId || busyKey.startsWith('install:')}>
                                    <IconDownload size={14} />{busyKey ? t('skillMarket.installing') : t('skillMarket.confirmInstall')}
                                </Button>
                            </div>
                        </div>
                    )}

                    {showPublish && (
                        <form className="skill-market-form-body skill-market-publish-form" onSubmit={publish}>
                            <label>{t('skillMarket.sourceAgent')}
                                {publishAgentId && <SelectDropdown value={publishAgentId} options={agentOptions} onChange={setPublishAgentId} ariaLabel={t('skillMarket.sourceAgent')} />}
                            </label>
                            <label>{t('skillMarket.skillFolder')}
                                {foldersLoading ? <span className="skill-market-field-status">{t('common.loading')}</span> : folderOptions.length ? (
                                    <SelectDropdown value={publishFolder} options={folderOptions} onChange={setPublishFolder} ariaLabel={t('skillMarket.skillFolder')} />
                                ) : <span className="skill-market-field-status">{t('skillMarket.noFolders')}</span>}
                            </label>
                            <label>{t('skillMarket.marketName')}
                                <TextInput value={publishName} onChange={(event) => setPublishName(event.target.value)} maxLength={100} required />
                            </label>
                            <label>{t('skillMarket.shortDescription')}
                                <TextInput value={publishDescription} onChange={(event) => setPublishDescription(event.target.value)} maxLength={2000} />
                            </label>
                            <div className="skill-market-form-row">
                                <label>{t('skillMarket.category')}
                                    <TextInput value={publishCategory} onChange={(event) => setPublishCategory(event.target.value)} maxLength={50} />
                                </label>
                                <label>{t('skillMarket.visibility')}
                                    <SelectDropdown
                                        value={publishVisibility}
                                        options={[
                                            { value: 'tenant', label: t('skillMarket.companyOnly') },
                                            { value: 'public', label: t('skillMarket.platformPublic') },
                                        ]}
                                        onChange={setPublishVisibility}
                                        ariaLabel={t('skillMarket.visibility')}
                                    />
                                </label>
                            </div>
                            <p className="skill-market-form-note">{t('skillMarket.publishNote')}</p>
                            <div className="skill-market-form-actions">
                                <Button variant="secondary" type="button" onClick={closeWorkbench}>{t('common.cancel')}</Button>
                                <Button variant="primary" type="submit" disabled={!publishFolder || !publishName.trim() || busyKey === 'publish'}>
                                    <IconUpload size={14} />{busyKey === 'publish' ? t('skillMarket.publishing') : t('skillMarket.publish')}
                                </Button>
                            </div>
                        </form>
                    )}
                </section>
            )}

            {loading ? (
                <div className="skill-market-empty">{t('common.loading')}</div>
            ) : skills.length ? (
                <section className="skill-market-grid">
                    {skills.map((skill) => (
                        <SkillCard
                            key={skill.id}
                            skill={skill}
                            mine={tab === 'mine'}
                            busy={busyKey.endsWith(skill.id)}
                            onDetail={() => openDetail(skill.id)}
                            onInstall={() => openInstall(skill)}
                            onOffline={() => takeOffline(skill)}
                        />
                    ))}
                </section>
            ) : (
                <div className="skill-market-empty">
                    <IconPackage size={28} stroke={1.3} />
                    <strong>{tab === 'mine' ? t('skillMarket.emptyMineTitle') : t('skillMarket.emptyMarketTitle')}</strong>
                    <span>{tab === 'mine' ? t('skillMarket.emptyMineHint') : t('skillMarket.emptyMarketHint')}</span>
                </div>
            )}
        </main>
        {detailId && (
            <SkillPreviewDrawer
                detail={detail}
                loading={detailLoading}
                onClose={() => setDetailId(null)}
            />
        )}
        </>
    );
}
