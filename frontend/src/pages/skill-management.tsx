import { useCallback, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import FileBrowser, { type FileBrowserApi } from '../components/FileBrowser';
import { Drawer, useDialog } from '../components/Dialog/DialogProvider';
import { useToast } from '../components/Toast/ToastProvider';
import Button from '../components/ui/Button';
import TextInput from '../components/ui/TextInput';
import SearchInput from '../components/ui/SearchInput';
import SelectDropdown from '../components/SelectDropdown';
import ToggleSwitch from '../components/ToggleSwitch';
import { IconClock, IconEdit, IconRefresh, IconUpload, IconCopy, IconArchive, IconTrash } from '@tabler/icons-react';
import { useAuthStore } from '../stores';
import SkillUploadDrawer from './SkillUploadDrawer';
import { skillApi, type MarketSkill } from '../services/api';
import './SkillMarket.css';

export function SkillUpdatedAt({ value }: { value?: string | null }) {
    const { t, i18n } = useTranslation();
    return <time className="skill-updated-at" dateTime={value || undefined} title={t('skillManagement.updatedAt')}>
        <IconClock size={13} aria-hidden="true" />
        {t('skillManagement.updatedAt')}: {value ? new Date(value).toLocaleString(i18n.language) : '—'}
    </time>;
}

function SkillEditor({ skill, onClose, onChange }: { skill: MarketSkill; onClose: () => void; onChange: () => void }) {
    const { t } = useTranslation();
    const toast = useToast();
    const [name, setName] = useState(skill.name);
    const [description, setDescription] = useState(skill.description);
    const [category, setCategory] = useState(skill.category);
    const [visibility, setVisibility] = useState(skill.visibility);
    const [saving, setSaving] = useState(false);
    const readOnly = !skill.capabilities?.edit;
    const api = useMemo<FileBrowserApi>(() => {
        let loaded: MarketSkill | undefined;
        const read = async () => { loaded = await skillApi.manage.detail(skill.id); return loaded; };
        const save = async (files: NonNullable<MarketSkill['files']>) => {
            await skillApi.manage.update(skill.id, { files, expected_version: loaded?.version });
            await read();
            onChange();
        };
        return {
            list: async (path) => {
                const detail = await read();
                const prefix = path ? `${path}/` : '';
                const entries = new Map<string, { name: string; path: string; is_dir: boolean }>();
                for (const file of detail.files || []) {
                    if (!file.path.startsWith(prefix)) continue;
                    const tail = file.path.slice(prefix.length);
                    const name = tail.split('/')[0];
                    if (name) entries.set(name, { name, path: `${prefix}${name}`, is_dir: tail.includes('/') });
                }
                return [...entries.values()];
            },
            read: async (path) => {
                const detail = await read();
                return { content: detail.files?.find(file => file.path === path)?.content || '' };
            },
            write: async (path, content) => {
                const detail = loaded || await read();
                await save([...(detail.files || []).filter(file => file.path !== path), { path, content }]);
            },
            delete: async (path) => {
                const detail = loaded || await read();
                await save((detail.files || []).filter(file => file.path !== path && !file.path.startsWith(`${path}/`)));
            },
        };
    }, [skill.id, onChange]);
    const save = async () => {
        setSaving(true);
        try {
            await skillApi.manage.update(skill.id, { name, description, category, visibility });
            onChange();
            toast.success(t('common.save'));
        } catch (error) { toast.error(t('skillManagement.failed'), { details: String(error) }); }
        finally { setSaving(false); }
    };
    return <Drawer open onClose={onClose} ariaLabelledBy="skill-editor-title">
        <div className="skill-market-form-body">
            <h2 id="skill-editor-title">{skill.name}</h2>
            <Button variant="secondary" onClick={onClose}>{t('common.close')}</Button>
            <label>{t('skillManagement.name')}<TextInput disabled={readOnly} value={name} onChange={event => setName(event.target.value)} /></label>
            <label>{t('skillManagement.description')}<TextInput disabled={readOnly} value={description} onChange={event => setDescription(event.target.value)} /></label>
            <label>{t('skillMarket.category')}<TextInput disabled={readOnly} value={category} onChange={event => setCategory(event.target.value)} /></label>
            {!skill.is_builtin && <label>{t('skillMarket.visibility')}<SelectDropdown value={visibility} onChange={value => setVisibility(value as 'tenant' | 'public')}
                options={[{ value: 'tenant', label: t('skillMarket.company') }, { value: 'public', label: t('skillMarket.public') }]} ariaLabel={t('skillMarket.visibility')} /></label>}
            {!readOnly && <Button variant="primary" onClick={save} disabled={saving || !name.trim()}>{t('common.save')}</Button>}
            <FileBrowser api={api} readOnly={readOnly} features={{ newFile: !readOnly, newFolder: !readOnly, edit: !readOnly, delete: !readOnly, directoryNavigation: true }} />
        </div>
    </Drawer>;
}

export default function SkillManagement() {
    const { t } = useTranslation();
    const dialog = useDialog();
    const toast = useToast();
    const client = useQueryClient();
    const user = useAuthStore(state => state.user);
    const platformAdmin = user?.role === 'platform_admin' || !!user?.is_platform_admin;
    const canUpload = platformAdmin || user?.role === 'org_admin';
    const [uploading, setUploading] = useState(false);
    const [search, setSearch] = useState('');
    const [editing, setEditing] = useState<MarketSkill | null>(null);
    const [busy, setBusy] = useState(false);
    const [jobId, setJobId] = useState<string | null>(null);
    const { data: skills = [], isLoading, error } = useQuery({ queryKey: ['skill-management'], queryFn: skillApi.manage.list });
    const { data: job } = useQuery({
        queryKey: ['skill-update-job', jobId], queryFn: () => skillApi.manage.progress(jobId!), enabled: !!jobId,
        refetchInterval: query => query.state.data?.pending ? 1500 : false,
    });
    const refresh = useCallback(() => {
        void client.invalidateQueries({ queryKey: ['skill-management'] });
        void client.invalidateQueries({ queryKey: ['skill-market'] });
    }, [client]);
    const action = async (operation: () => Promise<unknown>) => {
        setBusy(true);
        try { await operation(); refresh(); }
        catch (error) { toast.error(t('skillManagement.failed'), { details: String(error) }); }
        finally { setBusy(false); }
    };
    const updateAll = async (skill: MarketSkill) => {
        if (!await dialog.confirm(t('skillManagement.updateConfirm', { count: skill.active_installs }), { danger: true })) return;
        await action(async () => { const result = await skillApi.manage.updateAll(skill.id); setJobId(result.id); });
    };
    const remove = async (skill: MarketSkill) => {
        if (!await dialog.confirm(t('skillMarket.deleteConfirm', { name: skill.name }), { danger: true })) return;
        await action(() => skillApi.market.deleteOffline(skill.id));
    };
    const rows = skills.filter(skill => `${skill.name} ${skill.description} ${skill.folder_name}`.toLowerCase().includes(search.toLowerCase()));
    return <section className="skill-management">
        <div className="skill-management-toolbar">
            <SearchInput value={search} onChange={event => setSearch(event.target.value)} placeholder={t('skillMarket.searchPlaceholder')} aria-label={t('skillMarket.searchAria')} />
            {canUpload && <Button variant="primary" onClick={() => setUploading(true)}><IconUpload size={16} />{t('skillManagement.upload')}</Button>}
        </div>
        {isLoading && <p>{t('common.loading')}</p>}
        {error && <p role="alert">{t('skillManagement.failed')}</p>}
        {job && <div role="status" className="card">
            <p>{t('skillManagement.progress', job)}</p>
            {(job.failed > 0 || job.pending > 0) && <Button variant="secondary" disabled={busy} onClick={() => action(async () => {
                await skillApi.manage.retry(job.id);
                await client.invalidateQueries({ queryKey: ['skill-update-job', job.id] });
            })}>{t('skillManagement.retry')}</Button>}
        </div>}
        <div className="skill-market-grid">
            {rows.map(skill => <article className="skill-market-card" key={skill.id}>
                <h3>{skill.icon} {skill.name}</h3>
                <p>{skill.description}</p>
                <p>{t(`skillManagement.status.${skill.status}`)} · {t(skill.is_builtin ? 'skillManagement.builtin' : 'skillManagement.custom')}</p>
                <SkillUpdatedAt value={skill.updated_at} />
                <p>{t('skillManagement.installs', { count: skill.active_installs })}</p>
                <div className="skill-management-settings">
                    {skill.capabilities?.default && <label><span>{t('skillManagement.defaultInstall')}</span><ToggleSwitch checked={!!skill.is_default} disabled={busy}
                        ariaLabel={t('skillManagement.defaultInstall')} onChange={value => { void action(() => skillApi.manage.update(skill.id, { is_default: value })); }} /></label>}
                    {skill.capabilities?.hide && <label><span>{t('skillManagement.hide')}</span><ToggleSwitch checked={!!skill.hidden} disabled={busy}
                        ariaLabel={t('skillManagement.hide')} onChange={value => { void action(() => skillApi.manage.hide(skill.id, value)); }} /></label>}
                </div>
                <footer>
                    {!skill.capabilities?.edit && <Button variant="secondary" onClick={() => setEditing(skill)}>{t('skillMarket.viewDetails')}</Button>}
                    {skill.capabilities?.edit && <>
                        <Button variant="primary" disabled={busy} onClick={() => setEditing(skill)}><IconEdit size={14} />{t('skillManagement.edit')}</Button>
                        <Button variant="secondary" disabled={busy} onClick={() => action(() => skillApi.manage.update(skill.id, { status: skill.status === 'published' ? 'offline' : 'published' }))}>
                            <IconArchive size={14} />
                            {t(skill.status === 'published' ? 'skillMarket.takeOffline' : 'skillMarket.relist')}
                        </Button>
                        {skill.status === 'offline' && <Button variant="danger" disabled={busy} onClick={() => remove(skill)}><IconTrash size={14} />{t('skillMarket.deleteOffline')}</Button>}
                    </>}
                    {skill.capabilities?.copy && <Button variant="secondary" disabled={busy} onClick={() => action(() => skillApi.manage.copy(skill.id))}><IconCopy size={14} />{t('skillManagement.copy')}</Button>}
                    {skill.capabilities?.update_installs && <Button variant="secondary" disabled={busy || !skill.active_installs} onClick={() => updateAll(skill)}><IconRefresh size={14} />{t('skillManagement.updateAll')}</Button>}
                    {skill.latest_update_id && <Button variant="secondary" onClick={() => setJobId(skill.latest_update_id!)}>{t('skillManagement.updateProgress')}</Button>}
                </footer>
            </article>)}
        </div>
        {!isLoading && !rows.length && <p>{t('skillManagement.empty')}</p>}
        {editing && <SkillEditor skill={editing} onClose={() => setEditing(null)} onChange={refresh} />}
        {uploading && <SkillUploadDrawer platformAdmin={platformAdmin} onClose={() => setUploading(false)} onUploaded={refresh} />}
    </section>;
}
