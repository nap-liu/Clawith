import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconPlus, IconTrash } from '@tabler/icons-react';

import Button from '../../../components/ui/Button';
import TextInput from '../../../components/ui/TextInput';
import OrgMemberAccessPicker, { type AgentAccessUser } from '../../../components/OrgMemberAccessPicker';
import SelectDropdown from '../../../components/SelectDropdown';
import ToggleSwitch from '../../../components/ToggleSwitch';
import { createClientId } from '../../../utils/clientId';
import { groupPolicyApi, type GroupMember, type GroupPolicy, type GroupRule, type GroupTarget } from '../../../services/api/groupPolicy';

function MemberPicker({ agentId, members, users, rule, disabled, onChange, onRemoveLegacy }: {
    agentId: string; members: GroupMember[]; users: GroupMember[]; rule: GroupRule; disabled: boolean;
    onChange: (users: AgentAccessUser[]) => void; onRemoveLegacy: (id: string) => void;
}) {
    const { t } = useTranslation();
    const [open, setOpen] = useState(false);
    const selected = rule.user_ids.map(id => ({
        ...users.find(user => user.id === id), id,
        name: users.find(user => user.id === id)?.name || t('groupPolicy.unnamedMember'),
        access_level: 'use' as const,
    }));
    return <div>
        <Button variant="secondary" disabled={disabled} onClick={() => setOpen(true)}
            aria-label={t('groupPolicy.ruleMembers', { name: rule.name })}>
            {selected.length ? t('groupPolicy.selectedMembers', { count: selected.length }) : t('groupPolicy.selectMembers')}
        </Button>
        {selected.length > 0 && <p>{selected.map(user => user.name).join('、')}</p>}
        {rule.member_ids.length > 0 && <div>
            <p>{t('groupPolicy.legacyMembers')}</p>
            {rule.member_ids.map(id => <Button key={id} variant="ghost" disabled={disabled} onClick={() => onRemoveLegacy(id)}
                aria-label={t('groupPolicy.removeNamed', { name: members.find(member => member.id === id)?.name || t('groupPolicy.unnamedMember') })}>
                {members.find(member => member.id === id)?.name || t('groupPolicy.unnamedMember')} <IconTrash size={14} aria-hidden="true" />
            </Button>)}
        </div>}
        {open && <OrgMemberAccessPicker open agentId={agentId} membersOnly
            title={t('groupPolicy.selectMembers')} description={t('groupPolicy.memberDiscovery')}
            confirmLabel={t('accessPicker.confirmSelection')} users={selected} departments={[]}
            onClose={() => setOpen(false)} onSave={async selectedUsers => onChange(selectedUsers)} />}
    </div>;
}

export default function GroupPolicyEditor({ agentId, group, onDirtyChange, onSaved, onBusyChange }: {
    agentId: string; group: GroupTarget; onDirtyChange: (dirty: boolean) => void; onSaved: (group: GroupTarget) => void;
    onBusyChange: (busy: boolean) => void;
}) {
    const { t } = useTranslation();
    const [saved, setSaved] = useState<GroupPolicy | null>(null);
    const [rules, setRules] = useState<GroupRule[]>([]);
    const [users, setUsers] = useState<GroupMember[]>([]);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState(false);
    const requestId = useRef(0);
    const dirty = !!saved && JSON.stringify(rules) !== JSON.stringify(saved.rules);
    const invalid = rules.some(rule => !rule.name.trim() || (!rule.all_members && !rule.member_ids.length && !rule.user_ids.length));
    useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false); }, [dirty, onDirtyChange]);
    useEffect(() => { onBusyChange(saving); return () => onBusyChange(false); }, [saving, onBusyChange]);
    const load = async () => {
        const id = ++requestId.current;
        setError('');
        try {
            const result = await groupPolicyApi.get(agentId, group.id, group.target_ref);
            if (id !== requestId.current) return;
            // Migrated unnamed rules receive localized editable names.
            result.rules = result.rules.map((rule, index) => ({ ...rule, name: rule.name || t('groupPolicy.defaultName', { count: index + 1 }) }));
            setSaved(result);
            setRules(result.rules);
            setUsers(result.users);
            onSaved(result.group);
        } catch { if (id === requestId.current) setError('groupPolicy.loadError'); }
    };
    useEffect(() => { void load(); return () => { requestId.current += 1; }; }, [agentId, group.id]);
    const update = (id: string, change: Partial<GroupRule>) => {
        setRules(current => current.map(rule => rule.id === id ? { ...rule, ...change } : rule));
        setNotice(false);
    };
    const save = async () => {
        if (!saved) return;
        setSaving(true);
        setError('');
        try {
            const result = await groupPolicyApi.save(agentId, group.id, rules, saved.revision, group.target_ref);
            setSaved(result);
            setRules(result.rules);
            setUsers(result.users);
            onSaved(result.group);
            setNotice(true);
        } catch (reason) {
            const key = reason instanceof Error ? reason.message : '';
            setError(key.startsWith('groupPolicy.') && t(key) !== key ? key : 'groupPolicy.saveError');
        } finally { setSaving(false); }
    };
    const disabled = saving || !group.available;
    return <div className="card group-policy__editor">
        <div className="group-policy__row">
            <div><h4>{group.name || t('groupPolicy.unnamedGroup')}</h4>
                <p><span className="badge">{t(`groupPolicy.channels.${group.channel}`)}</span> {t('groupPolicy.enabledCount', { count: rules.filter(rule => rule.enabled).length })}</p>
            </div>
            <Button variant="primary" disabled={!dirty || disabled || invalid} onClick={() => void save()}>{t(saving ? 'groupPolicy.saving' : 'groupPolicy.save')}</Button>
        </div>
        <p className="group-policy__semantics">{t('groupPolicy.semantics')}</p>
        {!group.available && <p className="group-policy__warning" role="status">{t('groupPolicy.unavailable')}</p>}
        {error && <div className="group-policy__error" role="alert">{t(error)}
            {(!saved || error === 'groupPolicy.conflict') && <Button variant="ghost" onClick={() => void load()}>{t('groupPolicy.reload')}</Button>}
        </div>}
        {notice && !dirty && <p className="group-policy__success" role="status">{t('groupPolicy.saved')}</p>}
        {!saved ? <p role="status">{t(error ? 'groupPolicy.loadError' : 'groupPolicy.loading')}</p> : <>
            <div className="group-policy__rules">
                {rules.map((rule, index) => <div key={rule.id} className={`group-policy__rule${rule.enabled ? '' : ' is-disabled'}`}>
                    <div className="group-policy__row">
                        <span className={`badge ${rule.effect === 'deny' ? 'badge-warning' : 'badge-success'}`}>{t(`groupPolicy.effects.${rule.effect}`)}</span>
                        <div className="group-policy__rule-actions">
                            <span>{t(rule.enabled ? 'groupPolicy.enabled' : 'groupPolicy.disabled')}</span>
                            <ToggleSwitch checked={rule.enabled} onChange={enabled => update(rule.id, { enabled })}
                                ariaLabel={t('groupPolicy.toggleRule', { name: rule.name })} disabled={disabled} />
                            <Button variant="ghost" disabled={disabled} aria-label={t('groupPolicy.removeNamed', { name: rule.name })}
                                onClick={() => setRules(current => current.filter(item => item.id !== rule.id))}><IconTrash size={16} aria-hidden="true" /></Button>
                        </div>
                    </div>
                    <div className="group-policy__rule-fields">
                        <label>{t('groupPolicy.ruleName')}<TextInput value={rule.name} maxLength={80} disabled={disabled}
                            aria-label={t('groupPolicy.ruleNameNumber', { count: index + 1 })} onChange={event => update(rule.id, { name: event.target.value })} /></label>
                        <label>{t('groupPolicy.action')}<SelectDropdown value={rule.effect} options={(['allow', 'deny'] as const).map(value => ({ value, label: t(`groupPolicy.effects.${value}`) }))}
                            onChange={effect => update(rule.id, { effect })} disabled={disabled} ariaLabel={t('groupPolicy.ruleAction', { name: rule.name })} /></label>
                        <label>{t('groupPolicy.memberScope')}<SelectDropdown value={rule.all_members ? 'all' : 'selected'}
                            options={[{ value: 'selected', label: t('groupPolicy.specificMembers') }, { value: 'all', label: t('groupPolicy.allMembers') }]}
                            onChange={value => update(rule.id, { all_members: value === 'all', member_ids: [], user_ids: [] })} disabled={disabled}
                            ariaLabel={t('groupPolicy.ruleScope', { name: rule.name })} /></label>
                    </div>
                    {!rule.all_members && <MemberPicker agentId={agentId} members={saved.members} users={users} rule={rule} disabled={disabled}
                        onChange={selectedUsers => {
                            setUsers(current => [...new Map([...current, ...selectedUsers].map(user => [user.id, user])).values()]);
                            update(rule.id, { user_ids: selectedUsers.map(user => user.id) });
                        }} onRemoveLegacy={id => update(rule.id, { member_ids: rule.member_ids.filter(member => member !== id) })} />}
                    {!rule.all_members && !rule.member_ids.length && !rule.user_ids.length && <p className="group-policy__warning">{t('groupPolicy.chooseMembers')}</p>}
                </div>)}
                {!rules.length && <div className="group-policy__empty"><p>{t('groupPolicy.noRules')}</p></div>}
            </div>
            <Button variant="secondary" disabled={disabled || rules.length >= 100} onClick={() => setRules(current => [...current, {
                id: createClientId(), name: t('groupPolicy.defaultName', { count: current.length + 1 }),
                effect: 'deny', enabled: true, all_members: false, member_ids: [], user_ids: [],
            }])}><IconPlus size={16} aria-hidden="true" />{t('groupPolicy.addRule')}</Button>
            <p>{t('groupPolicy.memberDiscovery')}</p>
            <p>{t('groupPolicy.effective')}</p>
        </>}
    </div>;
}
