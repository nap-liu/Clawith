import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { IconBolt, IconDownload, IconFolder, IconTools } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';

import { useDialog } from '../../../components/Dialog/DialogProvider';
import type { FileBrowserApi } from '../../../components/FileBrowser';
import FileBrowser from '../../../components/FileBrowser';
import ToolCatalogPanel from '../../../components/tools/ToolCatalogPanel';
import { useToast } from '../../../components/Toast/ToastProvider';
import { fileApi, skillApi } from '../../../services/api';
import type { ProjectCapabilityOption } from '../../../features/projects/types';
import { projectUserFacingCopy } from '../../../features/projects/projectUserFacingCopy';
import { getLocalizedToolPresentation } from '../../../utils/toolPresentation';

interface Props {
    agentId: string;
    canManage: boolean;
    scope?: 'agent' | 'project';
    draftCapabilities?: ProjectCapabilityOption[];
    selectedCapabilityIds?: string[];
    onSelectedCapabilityIdsChange?: (ids: string[]) => void;
}

export default function SkillsTab({
    agentId,
    canManage,
    scope = 'agent',
    draftCapabilities,
    selectedCapabilityIds = [],
    onSelectedCapabilityIdsChange,
}: Props) {
    const { t, i18n } = useTranslation();
    const dialog = useDialog();
    const toast = useToast();
    const queryClient = useQueryClient();
    const [showAgentClawhub, setShowAgentClawhub] = useState(false);
    const [agentClawhubQuery, setAgentClawhubQuery] = useState('');
    const [agentClawhubResults, setAgentClawhubResults] = useState<any[]>([]);
    const [agentClawhubSearching, setAgentClawhubSearching] = useState(false);
    const [agentClawhubInstalling, setAgentClawhubInstalling] = useState<string | null>(null);
    const [showAgentUrlImport, setShowAgentUrlImport] = useState(false);
    const [agentUrlInput, setAgentUrlInput] = useState('');
    const [agentUrlImporting, setAgentUrlImporting] = useState(false);
    const [showImportSkillModal, setShowImportSkillModal] = useState(false);
    const [importingSkillId, setImportingSkillId] = useState<string | null>(null);
    const [selectionSearch, setSelectionSearch] = useState('');
    const [selectionSelectedOnly, setSelectionSelectedOnly] = useState(false);
    const [expandedSelectionGroups, setExpandedSelectionGroups] = useState<Set<string>>(
        () => new Set(['skill:market', 'skill:digital_employee']),
    );
    const { data: globalSkillsForImport } = useQuery({
        queryKey: ['global-skills-for-import'],
        queryFn: () => skillApi.list(),
        enabled: showImportSkillModal,
    });
    useEffect(() => {
        setSelectionSearch('');
        setSelectionSelectedOnly(false);
        setExpandedSelectionGroups(new Set(['skill:market', 'skill:digital_employee']));
    }, [agentId]);
    const safeDisplayIcon = (icon?: string | null, fallback = <IconTools size={20} stroke={1.8} />) =>
        icon && !/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}]/u.test(icon) ? icon : fallback;
    const adapter: FileBrowserApi = {
        list: (path) => fileApi.list(agentId, path),
        read: (path) => fileApi.read(agentId, path),
        write: (path, content) => fileApi.write(agentId, path, content),
        delete: (path) => fileApi.delete(agentId, path),
        upload: (file, path, onProgress) => fileApi.upload(agentId, file, path, onProgress),
        downloadUrl: (path) => fileApi.downloadUrl(agentId, path),
    };

    const searchClawHub = () => {
        setAgentClawhubSearching(true);
        skillApi.clawhub.search(agentClawhubQuery)
            .then((results) => {
                setAgentClawhubResults(results);
                setAgentClawhubSearching(false);
            })
            .catch(() => setAgentClawhubSearching(false));
    };

    if (draftCapabilities) {
        const selectedKeys = new Set(selectedCapabilityIds);
        const visibleCapabilities = selectionSelectedOnly
            ? draftCapabilities.filter((capability) =>
                selectedKeys.has(capability.capability_id || capability.id),
            )
            : draftCapabilities;
        const getPresentation = (capability: ProjectCapabilityOption) => {
            const base = getLocalizedToolPresentation(t, {
                name: projectUserFacingCopy(capability.name, t),
                description: capability.description,
                type: capability.kind,
            });
            const origin = capability.origin ||
                (capability.source === 'agent' ? 'digital_employee' : 'market');
            return {
                ...base,
                groupKey: `skill:${origin}`,
                groupLabel: t(`projectCreate.capabilities.groups.skill.${origin}`),
                groupDescription: '',
            };
        };
        return (
            <ToolCatalogPanel
                className="pm-capability-catalog"
                items={visibleCapabilities}
                allItems={draftCapabilities}
                getKey={(capability) => capability.capability_id || capability.id}
                getPresentation={getPresentation}
                searchValue={selectionSearch}
                onSearchChange={setSelectionSearch}
                searchPlaceholder={t('projectCreate.capabilities.searchSkills')}
                emptyLabel={t(selectionSelectedOnly
                    ? 'projectCreate.capabilities.noSelected'
                    : 'projectCreate.capabilities.noSkills')}
                ariaLabel={t('projectAgents.capabilityPackage.sections.skill')}
                expandedGroups={expandedSelectionGroups}
                onExpandedGroupsChange={setExpandedSelectionGroups}
                selectedKeys={selectedKeys}
                prioritizeSelected
                groupOrder={['skill:market', 'skill:digital_employee']}
                onToggle={canManage && onSelectedCapabilityIdsChange
                    ? (capabilityId) => onSelectedCapabilityIdsChange(
                        selectedKeys.has(capabilityId)
                            ? selectedCapabilityIds.filter((id) => id !== capabilityId)
                            : [...selectedCapabilityIds, capabilityId],
                    )
                    : undefined}
                renderGroupIcon={() => <IconBolt size={15} />}
                renderGroupSummary={(group) => t('projectCreate.capabilities.groupCount', {
                    selected: group.allItems.filter((capability) =>
                        selectedKeys.has(capability.capability_id || capability.id),
                    ).length,
                    total: group.allItems.length,
                })}
                toolbar={
                    <button
                        type="button"
                        className={`btn btn-secondary tool-catalog-panel__filter${selectionSelectedOnly ? ' is-active' : ''}`}
                        onClick={() => setSelectionSelectedOnly((current) => !current)}
                        aria-pressed={selectionSelectedOnly}
                    >
                        {t(selectionSelectedOnly
                            ? 'projectCreate.capabilities.showAll'
                            : 'projectCreate.capabilities.selectedOnly')}
                    </button>
                }
            />
        );
    }

    return (
        <div>
            <div style={{ marginBottom: '16px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                        <h3 style={{ marginBottom: '4px' }}>{t('agent.skills.title')}</h3>
                        <p style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>{t('agent.skills.description')}</p>
                    </div>
                    {canManage && <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
                        <button
                            className="btn btn-secondary"
                            style={{ fontSize: '13px' }}
                            onClick={() => { setShowAgentUrlImport(true); setAgentUrlInput(''); }}
                        >
                            {t('agent.skills.importFromUrl')}
                        </button>
                        <button
                            className="btn btn-secondary"
                            style={{ fontSize: '13px' }}
                            onClick={() => { setShowAgentClawhub(true); setAgentClawhubQuery(''); setAgentClawhubResults([]); }}
                        >
                            {t('agent.skills.browseClawhub')}
                        </button>
                        <button
                            className="btn btn-primary"
                            style={{ display: 'flex', alignItems: 'center', gap: '6px', whiteSpace: 'nowrap' }}
                            onClick={() => setShowImportSkillModal(true)}
                        >
                            {t('agent.skills.importPreset')}
                        </button>
                    </div>}
                </div>
            </div>

            <FileBrowser api={adapter} rootPath="skills" features={{ newFile: canManage, edit: canManage, delete: canManage, newFolder: canManage, upload: canManage, directoryNavigation: true }} title={t('agent.skills.skillFiles')} />

            {showAgentClawhub && (
                <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.5)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={() => setShowAgentClawhub(false)}>
                    <div onClick={(e) => e.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', maxWidth: '600px', width: '90%', maxHeight: '70vh', display: 'flex', flexDirection: 'column', boxShadow: '0 20px 60px rgba(0,0,0,0.3)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                            <h3>{t('agent.skills.browseClawhub')}</h3>
                            <button
                                aria-label={t('agent.skills.close')}
                                title={t('agent.skills.close')}
                                onClick={() => setShowAgentClawhub(false)}
                                style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)', padding: '4px 8px' }}
                            >×</button>
                        </div>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: '0 0 12px' }}>
                            {t('agent.skills.browseClawhubDescription')}
                        </p>
                        <div style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}>
                            <input
                                className="input"
                                placeholder={t('agent.skills.searchSkills')}
                                aria-label={t('agent.skills.searchSkills')}
                                value={agentClawhubQuery}
                                onChange={(e) => setAgentClawhubQuery(e.target.value)}
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter' && agentClawhubQuery.trim()) searchClawHub();
                                }}
                                style={{ flex: 1, fontSize: '13px' }}
                            />
                            <button
                                className="btn btn-primary"
                                style={{ fontSize: '13px' }}
                                disabled={!agentClawhubQuery.trim() || agentClawhubSearching}
                                onClick={searchClawHub}
                            >
                                {agentClawhubSearching ? t('agent.skills.searching') : t('agent.skills.search')}
                            </button>
                        </div>
                        <div style={{ flex: 1, overflowY: 'auto' }}>
                            {agentClawhubResults.length === 0 && !agentClawhubSearching && (
                                <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)', fontSize: '13px' }}>{t('agent.skills.searchClawhubHint')}</div>
                            )}
                            {agentClawhubResults.map((result: any) => (
                                <div key={result.slug} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 12px', borderRadius: '8px', marginBottom: '6px', border: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)' }}>
                                    <div style={{ flex: 1 }}>
                                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                                            <span style={{ fontWeight: 600, fontSize: '13px' }}>{result.displayName || result.slug}</span>
                                            {result.version && <span style={{ fontSize: '10px', color: 'var(--accent-text)', background: 'var(--accent-subtle)', padding: '1px 5px', borderRadius: '4px' }}>v{result.version}</span>}
                                        </div>
                                        <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{result.summary?.substring(0, 100)}{result.summary?.length > 100 ? '...' : ''}</div>
                                        {result.updatedAt && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px', opacity: 0.7 }}>{t('agent.skills.updated', { date: new Date(result.updatedAt).toLocaleDateString(i18n.language) })}</div>}
                                    </div>
                                    <button
                                        className="btn btn-secondary"
                                        style={{ fontSize: '12px', padding: '5px 12px', marginLeft: '12px' }}
                                        disabled={agentClawhubInstalling === result.slug}
                                        onClick={async () => {
                                            if (!canManage) return;
                                            setAgentClawhubInstalling(result.slug);
                                            try {
                                                const response = await skillApi.agentImport.fromClawhub(agentId, result.slug);
                                                toast.success(t('common.file.skillInstalled', { name: result.displayName || result.slug }));
                                                queryClient.invalidateQueries({ queryKey: ['files', agentId, 'skills'] });
                                            } catch (err: any) {
                                                await dialog.alert(t('common.error.installFailed'), { type: 'error', details: scope === 'project' ? undefined : String(err?.message || err) });
                                            } finally {
                                                setAgentClawhubInstalling(null);
                                            }
                                        }}
                                    >
                                        {agentClawhubInstalling === result.slug ? t('agent.skills.installing') : t('agent.skills.install')}
                                    </button>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            )}

            {showAgentUrlImport && (
                <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.5)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={() => setShowAgentUrlImport(false)}>
                    <div onClick={(e) => e.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', maxWidth: '500px', width: '90%', boxShadow: '0 20px 60px rgba(0,0,0,0.3)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                            <h3>{t('agent.skills.importFromGithub')}</h3>
                            <button
                                aria-label={t('agent.skills.close')}
                                title={t('agent.skills.close')}
                                onClick={() => setShowAgentUrlImport(false)}
                                style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)', padding: '4px 8px' }}
                            >×</button>
                        </div>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: '0 0 12px' }}>
                            {t('agent.skills.githubUrlDesc')}
                        </p>
                        <input
                            className="input"
                            placeholder={t('agent.skills.githubUrlPlaceholder')}
                            aria-label={t('agent.skills.githubUrlPlaceholder')}
                            value={agentUrlInput}
                            onChange={(e) => setAgentUrlInput(e.target.value)}
                            style={{ width: '100%', fontSize: '13px', marginBottom: '12px', boxSizing: 'border-box' }}
                        />
                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
                            <button className="btn btn-secondary" onClick={() => setShowAgentUrlImport(false)}>{t('agent.skills.cancel')}</button>
                            <button
                                className="btn btn-primary"
                                disabled={!agentUrlInput.trim() || agentUrlImporting}
                                onClick={async () => {
                                    if (!canManage) return;
                                    setAgentUrlImporting(true);
                                    try {
                                        const response = await skillApi.agentImport.fromUrl(agentId, agentUrlInput.trim());
                                        toast.success(t('common.file.filesImported', { count: response.files_written }));
                                        queryClient.invalidateQueries({ queryKey: ['files', agentId, 'skills'] });
                                        setShowAgentUrlImport(false);
                                    } catch (err: any) {
                                        await dialog.alert(t('common.error.importFailed'), { type: 'error', details: scope === 'project' ? undefined : String(err?.message || err) });
                                    } finally {
                                        setAgentUrlImporting(false);
                                    }
                                }}
                            >
                                {agentUrlImporting ? t('agent.skills.importing') : t('agent.skills.importBtn')}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {showImportSkillModal && (
                <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.5)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={() => setShowImportSkillModal(false)}>
                    <div onClick={(e) => e.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', maxWidth: '600px', width: '90%', maxHeight: '70vh', display: 'flex', flexDirection: 'column', boxShadow: '0 20px 60px rgba(0,0,0,0.3)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                            <h3>{t('agent.skills.importPreset', 'Import from Presets')}</h3>
                            <button
                                aria-label={t('agent.skills.close')}
                                title={t('agent.skills.close')}
                                onClick={() => setShowImportSkillModal(false)}
                                style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)', padding: '4px 8px' }}
                            >×</button>
                        </div>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: '0 0 16px' }}>
                            {t('agent.skills.importDesc', 'Select a preset skill to import into this agent. All skill files will be copied to the agent\'s skills folder.')}
                        </p>
                        <div style={{ flex: 1, overflowY: 'auto' }}>
                            {!globalSkillsForImport ? (
                                <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)' }}>{t('agent.skills.loading')}</div>
                            ) : globalSkillsForImport.length === 0 ? (
                                <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)' }}>{t('agent.skills.noPresetSkills')}</div>
                            ) : (
                                globalSkillsForImport.map((skill: any) => (
                                    <div
                                        key={skill.id}
                                        style={{
                                            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                                            padding: '12px 14px', borderRadius: '8px', marginBottom: '8px',
                                            border: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)',
                                            transition: 'border-color 0.15s',
                                        }}
                                        onMouseEnter={(e) => (e.currentTarget.style.borderColor = 'var(--accent-primary)')}
                                        onMouseLeave={(e) => (e.currentTarget.style.borderColor = 'var(--border-subtle)')}
                                    >
                                        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flex: 1 }}>
                                            <span style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-tertiary)' }}>
                                                {safeDisplayIcon(skill.icon, <IconTools size={20} stroke={1.8} />)}
                                            </span>
                                            <div>
                                                <div style={{ fontWeight: 600, fontSize: '14px' }}>{skill.name}</div>
                                                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                                                    {skill.description?.substring(0, 100)}{skill.description?.length > 100 ? '...' : ''}
                                                </div>
                                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                                                    <IconFolder size={12} stroke={1.8} /> {skill.folder_name}
                                                    {skill.is_default && <span style={{ marginLeft: '8px', color: 'var(--accent-primary)', fontWeight: 600 }}>✓ {t('agent.skills.default')}</span>}
                                                </div>
                                            </div>
                                        </div>
                                        <button
                                            className="btn btn-secondary"
                                            style={{ whiteSpace: 'nowrap', fontSize: '12px', padding: '6px 14px', display: 'inline-flex', alignItems: 'center', gap: '5px' }}
                                            disabled={importingSkillId === skill.id}
                                            onClick={async () => {
                                                if (!canManage) return;
                                                setImportingSkillId(skill.id);
                                                try {
                                                    const response = await fileApi.importSkill(agentId, skill.id);
                                                    toast.success(t('common.file.skillImported', { name: skill.name }));
                                                    queryClient.invalidateQueries({ queryKey: ['files', agentId, 'skills'] });
                                                    setShowImportSkillModal(false);
                                                } catch (err: any) {
                                                    await dialog.alert(t('common.error.importFailed'), { type: 'error', details: scope === 'project' ? undefined : String(err?.message || err) });
                                                } finally {
                                                    setImportingSkillId(null);
                                                }
                                            }}
                                        >
                                            {importingSkillId === skill.id ? t('agent.skills.importing') : <><IconDownload size={13} stroke={1.8} /> {t('agent.skills.importBtn')}</>}
                                        </button>
                                    </div>
                                ))
                            )}
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
