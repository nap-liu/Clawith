import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconUsersGroup } from '@tabler/icons-react';

import Button from '../../../components/ui/Button';
import SearchInput from '../../../components/ui/SearchInput';
import SelectDropdown from '../../../components/SelectDropdown';
import { useDialog } from '../../../components/Dialog/DialogProvider';
import { groupPolicyApi, type GroupTarget } from '../../../services/api/groupPolicy';
import GroupPolicyEditor from './GroupPolicyEditor';
import './GroupPolicyTab.css';

export default function GroupPolicyTab({ agentId, onDirtyChange }: { agentId: string; onDirtyChange: (dirty: boolean) => void }) {
    const { t } = useTranslation();
    const { confirm } = useDialog();
    const [groups, setGroups] = useState<GroupTarget[]>([]);
    const [active, setActive] = useState<GroupTarget | null>(null);
    const [query, setQuery] = useState('');
    const [channel, setChannel] = useState('');
    const [channels, setChannels] = useState<string[]>([]);
    const [nextOffset, setNextOffset] = useState<number | null>(null);
    const [total, setTotal] = useState(0);
    const [loading, setLoading] = useState(true);
    const [failed, setFailed] = useState(false);
    const [busy, setBusy] = useState(false);
    const dirtyRef = useRef(false);
    const requestId = useRef(0);
    const handleDirty = useCallback((dirty: boolean) => {
        dirtyRef.current = dirty;
        onDirtyChange(dirty);
    }, [onDirtyChange]);
    const load = async (offset = 0) => {
        const id = ++requestId.current;
        setLoading(true);
        setFailed(false);
        try {
            const result = await groupPolicyApi.groups(agentId, query, channel, offset);
            if (id !== requestId.current) return;
            setGroups(current => offset ? [...current, ...result.items] : result.items);
            setTotal(result.total);
            setChannels(result.channels);
            setNextOffset(result.next_offset);
            setActive(current => current || result.items[0] || null);
        } catch { if (id === requestId.current) setFailed(true); }
        finally { if (id === requestId.current) setLoading(false); }
    };
    useEffect(() => {
        setActive(null);
        handleDirty(false);
    }, [agentId, handleDirty]);
    useEffect(() => {
        requestId.current += 1;
        setGroups([]);
        setNextOffset(null);
        const timer = window.setTimeout(() => void load(), 200);
        return () => { window.clearTimeout(timer); requestId.current += 1; };
    }, [agentId, query, channel]);
    const selectGroup = async (group: GroupTarget) => {
        if (active?.id === group.id) return;
        if (dirtyRef.current && !await confirm(t('groupPolicy.unsaved'), {
            title: t('unsavedChanges.title'), confirmLabel: t('unsavedChanges.continue'), danger: true,
        })) return;
        handleDirty(false);
        setActive(group);
    };
    const saved = (group: GroupTarget) => {
        setGroups(current => current.map(item => item.id === group.id ? group : item));
        setActive(group);
    };
    return <section className="group-policy" aria-label={t('agent.tabs.groupPolicy')}>
        <div className="group-policy__header">
            <h3>{t('agent.tabs.groupPolicy')}</h3>
            <p>{t('groupPolicy.description')}</p>
        </div>
        <div className="group-policy__workspace">
            <aside className="card group-policy__directory" aria-label={t('groupPolicy.groups')}>
                <div className="group-policy__filters">
                    <SearchInput value={query} onChange={event => setQuery(event.target.value)}
                        placeholder={t('groupPolicy.search')} aria-label={t('groupPolicy.search')} />
                    <SelectDropdown value={channel} options={[{ value: '', label: t('groupPolicy.allChannels') },
                        ...channels.map(value => ({ value, label: t(`groupPolicy.channels.${value}`) }))]}
                        onChange={setChannel} ariaLabel={t('groupPolicy.channel')} />
                </div>
                <div className="group-policy__directory-label">{t('groupPolicy.groupCount', { count: total })}</div>
                {failed && <div role="alert"><p>{t('groupPolicy.loadError')}</p><Button variant="ghost" onClick={() => void load()}>{t('groupPolicy.reload')}</Button></div>}
                <div className="group-policy__groups">
                    {groups.map(group => <Button key={group.id} variant="ghost"
                        disabled={busy}
                        className={`group-policy__group${active?.id === group.id ? ' is-active' : ''}`}
                        aria-pressed={active?.id === group.id} onClick={() => void selectGroup(group)}>
                        <IconUsersGroup size={18} aria-hidden="true" />
                        <span className="group-policy__group-info">
                            <strong>{group.name || t('groupPolicy.unnamedGroup')}</strong>
                            <span className="group-policy__group-meta">
                                <span className="badge">{t(`groupPolicy.channels.${group.channel}`)}</span>
                                <span>{t('groupPolicy.ruleCount', { count: group.rule_count })}</span>
                            </span>
                            {!group.available && <span className="group-policy__warning">{t('groupPolicy.unavailableLabel')}</span>}
                        </span>
                    </Button>)}
                </div>
                {loading && <p role="status">{t('groupPolicy.loading')}</p>}
                {!loading && !failed && !groups.length && <p className="group-policy__empty">{t(query || channel ? 'groupPolicy.noMatches' : 'groupPolicy.noGroups')}</p>}
                {nextOffset !== null && <Button variant="ghost" disabled={loading} onClick={() => void load(nextOffset)}>{t('groupPolicy.loadMore')}</Button>}
                <p className="group-policy__discovery">{t('groupPolicy.discovery')}</p>
            </aside>
            {active ? <GroupPolicyEditor key={`${agentId}:${active.id}`} agentId={agentId} group={active}
                onDirtyChange={handleDirty} onSaved={saved} onBusyChange={setBusy} /> : <div className="card group-policy__empty">
                <IconUsersGroup size={30} aria-hidden="true" />
                <p>{t('groupPolicy.selectGroup')}</p>
            </div>}
        </div>
    </section>;
}
