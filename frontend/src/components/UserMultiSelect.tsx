/**
 * UserMultiSelect — searchable multi-select list of org members,
 * shared between AgentCreate Step 4 and AgentDetail Settings for the
 * "specific users" permission scope. Stays purely presentational —
 * the parent owns the selectedIds state and any save/submit flow.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

export interface UserMember {
    id: string;
    display_name?: string;
    email?: string;
    department_path?: string;
    title?: string;
}

export interface UserMultiSelectProps {
    members: UserMember[];
    selectedIds: string[];
    onSelectionChange: (next: string[]) => void;
    /** Wrapper container override (used by callers with different visual padding). */
    style?: React.CSSProperties;
    /** Override max-height of the scrollable list (default 240). */
    listMaxHeight?: number;
    /** When true (default), append email after display_name in each row. */
    showEmail?: boolean;
    /** When true (default), show "已选择 N 人" counter under the list. */
    showSelectedCount?: boolean;
}

export default function UserMultiSelect({
    members,
    selectedIds,
    onSelectionChange,
    style,
    listMaxHeight = 240,
    showEmail = true,
    showSelectedCount = true,
}: UserMultiSelectProps) {
    const { t } = useTranslation();
    const [query, setQuery] = useState('');

    const matches = (m: UserMember) => {
        if (!query) return true;
        const q = query.toLowerCase();
        return (m.display_name || '').toLowerCase().includes(q) ||
            (m.title || '').toLowerCase().includes(q) ||
            (m.department_path || '').toLowerCase().includes(q);
    };

    const toggle = (id: string) => {
        onSelectionChange(
            selectedIds.includes(id)
                ? selectedIds.filter(x => x !== id)
                : [...selectedIds, id]
        );
    };

    return (
        <div style={style}>
            <input
                type="text"
                className="form-input"
                placeholder={t('wizard.step4.searchUsers', '搜索用户...')}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                style={{ marginBottom: '12px', fontSize: '13px' }}
            />
            {members.length === 0 && (
                <div style={{ padding: '12px', background: 'var(--bg-elevated)', borderRadius: '8px', fontSize: '13px', color: 'var(--text-tertiary)', textAlign: 'center' }}>
                    {t('enterprise.org.noMembers', '暂无公司成员')}
                </div>
            )}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', maxHeight: `${listMaxHeight}px`, overflowY: 'auto' }}>
                {members.filter(matches).map((member) => {
                    const isChecked = selectedIds.includes(member.id);
                    const parts: string[] = [];
                    if (member.display_name) parts.push(member.display_name);
                    if (showEmail && member.email) parts.push(member.email);
                    if (member.department_path) parts.push(member.department_path);
                    if (member.title) parts.push(member.title);
                    return (
                        <label key={member.id} style={{
                            display: 'flex', alignItems: 'center', gap: '10px', padding: '10px 12px',
                            background: isChecked ? 'var(--accent-subtle)' : 'var(--bg-elevated)',
                            border: `1px solid ${isChecked ? 'var(--accent-primary)' : 'var(--border-default)'}`,
                            borderRadius: '8px', cursor: 'pointer',
                        }}>
                            <input type="checkbox" checked={isChecked} onChange={() => toggle(member.id)} />
                            <span style={{ fontSize: '13px' }}>{parts.join(' · ')}</span>
                        </label>
                    );
                })}
            </div>
            {showSelectedCount && selectedIds.length > 0 && (
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '8px' }}>
                    {t('wizard.step4.selectedCount', { count: selectedIds.length })}
                </div>
            )}
        </div>
    );
}
