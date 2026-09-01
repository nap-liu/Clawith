import React, { useState } from 'react';
import { fetchJson } from '../../services/api';
import { useDialog } from '../../components/Dialog/DialogProvider';
import {
    deriveStatus,
    objectiveProgress,
    progressPercent,
    STATUS_COLOR,
    STATUS_LABELS,
    type KeyResult,
    type Objective,
    type Period,
} from './model';

export function StatusBadge({ status, isChinese }: { status: string; isChinese: boolean }) {
    const color = STATUS_COLOR[status] ?? 'var(--text-tertiary)';
    const label = isChinese ? (STATUS_LABELS[status]?.zh ?? status) : (STATUS_LABELS[status]?.en ?? status);
    return (
        <span style={{
            display: 'inline-flex', alignItems: 'center', gap: '4px',
            padding: '2px 8px', borderRadius: '100px',
            background: `${color}18`,
            border: `1px solid ${color}40`,
            color, fontSize: '11px', fontWeight: 500,
        }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: color, flexShrink: 0 }} />
            {label}
        </span>
    );
}

export function ProgressBar({ pct, status }: { pct: number; status: string }) {
    const color = STATUS_COLOR[status] ?? 'var(--accent-primary)';
    return (
        <div style={{ height: 4, background: 'var(--bg-tertiary)', borderRadius: 2, overflow: 'hidden', flexGrow: 1 }}>
            <div style={{
                height: '100%', borderRadius: 2,
                background: color, width: `${pct}%`,
                transition: 'width 0.6s ease',
            }} />
        </div>
    );
}

function KRCard({
    kr,
    isChinese,
    onUpdateProgress,
    onDelete,
    canEdit,
}: {
    kr: KeyResult;
    isChinese: boolean;
    onUpdateProgress: (krId: string, value: number, status: string, note: string) => void;
    onDelete?: (krId: string) => void;
    canEdit: boolean;
}) {
    const dialog = useDialog();
    const pct = progressPercent(kr);
    const [editing, setEditing] = useState(false);
    const [editValue, setEditValue] = useState(String(kr.current_value));
    const [editStatus, setEditStatus] = useState('auto');
    const [editNote, setEditNote] = useState('');
    const [saving, setSaving] = useState(false);

    async function handleSave() {
        const val = parseFloat(editValue);
        if (isNaN(val)) return;
        setSaving(true);
        try {
            await onUpdateProgress(kr.id, val, editStatus, editNote);
            setEditing(false);
            setEditNote('');
        } finally {
            setSaving(false);
        }
    }

    return (
        <div style={{
            padding: editing ? '12px 14px' : '10px 14px',
            background: 'var(--bg-secondary)',
            border: `1px solid ${editing ? 'var(--accent-primary)40' : 'var(--border-subtle)'}`,
            borderRadius: '8px',
            display: 'flex', flexDirection: 'column', gap: '8px',
            transition: 'border-color 0.15s',
        }}>
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '8px' }}>
                <span style={{ fontSize: '13px', color: 'var(--text-primary)', flex: 1 }}>{kr.title}</span>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
                    <StatusBadge status={kr.status} isChinese={isChinese} />
                    {canEdit && !editing && (
                        <button
                            id={`kr-edit-${kr.id}`}
                            onClick={() => { setEditing(true); setEditValue(String(kr.current_value)); }}
                            style={{
                                background: 'none', border: '1px solid var(--border-subtle)',
                                borderRadius: '4px', padding: '2px 8px',
                                fontSize: '11px', color: 'var(--text-tertiary)',
                                cursor: 'pointer', transition: 'all 0.15s',
                                whiteSpace: 'nowrap',
                            }}
                            onMouseEnter={e => {
                                (e.currentTarget as HTMLButtonElement).style.borderColor = 'var(--accent-primary)';
                                (e.currentTarget as HTMLButtonElement).style.color = 'var(--accent-primary)';
                            }}
                            onMouseLeave={e => {
                                (e.currentTarget as HTMLButtonElement).style.borderColor = 'var(--border-subtle)';
                                (e.currentTarget as HTMLButtonElement).style.color = 'var(--text-tertiary)';
                            }}
                        >
                            {isChinese ? '更新进度' : 'Update'}
                        </button>
                    )}
                    {canEdit && !editing && onDelete && (
                        <button
                            onClick={async () => {
                                const ok = await dialog.confirm(
                                    isChinese ? '确定要删除这个 Key Result 吗？此操作不可恢复。' : 'Are you sure you want to delete this Key Result?',
                                    { title: isChinese ? '删除 Key Result' : 'Delete Key Result', danger: true, confirmLabel: isChinese ? '删除' : 'Delete' },
                                );
                                if (ok) {
                                    onDelete(kr.id);
                                }
                            }}
                            title={isChinese ? '删除' : 'Delete'}
                            style={{
                                background: 'none', border: '1px solid var(--border-subtle)',
                                borderRadius: '4px', padding: '2px 6px',
                                display: 'flex', alignItems: 'center', justifyContent: 'center',
                                color: 'var(--text-tertiary)', cursor: 'pointer', transition: 'all 0.15s',
                            }}
                            onMouseEnter={e => {
                                (e.currentTarget as HTMLButtonElement).style.borderColor = '#ef4444';
                                (e.currentTarget as HTMLButtonElement).style.color = '#ef4444';
                            }}
                            onMouseLeave={e => {
                                (e.currentTarget as HTMLButtonElement).style.borderColor = 'var(--border-subtle)';
                                (e.currentTarget as HTMLButtonElement).style.color = 'var(--text-tertiary)';
                            }}
                        >
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                        </button>
                    )}
                </div>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <ProgressBar pct={pct} status={kr.status} />
                <span style={{ fontSize: '11px', color: 'var(--text-secondary)', whiteSpace: 'nowrap', minWidth: 64, textAlign: 'right' }}>
                    {kr.current_value} / {kr.target_value}
                    {kr.unit ? ` ${kr.unit}` : ''} ({pct}%)
                </span>
            </div>

            {editing && (
                <div style={{
                    borderTop: '1px solid var(--border-subtle)',
                    paddingTop: '10px',
                    display: 'flex', flexDirection: 'column', gap: '8px',
                }}>
                    <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                            <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                                {isChinese ? '当前值' : 'Value'}
                            </span>
                            <input
                                type="number"
                                value={editValue}
                                onChange={e => setEditValue(e.target.value)}
                                style={{
                                    width: 80, padding: '4px 8px',
                                    background: 'var(--bg-primary)',
                                    border: '1px solid var(--border-subtle)',
                                    borderRadius: '4px', color: 'var(--text-primary)',
                                    fontSize: '13px',
                                }}
                            />
                            {kr.unit && <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{kr.unit}</span>}
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                            <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                                {isChinese ? '状态' : 'Status'}
                            </span>
                            <select
                                value={editStatus}
                                onChange={e => setEditStatus(e.target.value)}
                                style={{
                                    padding: '4px 8px',
                                    background: 'var(--bg-primary)',
                                    border: '1px solid var(--border-subtle)',
                                    borderRadius: '4px', color: 'var(--text-primary)',
                                    fontSize: '12px',
                                }}
                            >
                                <option value="auto">{isChinese ? '自动计算' : 'Auto'}</option>
                                <option value="on_track">{isChinese ? '按计划' : 'On Track'}</option>
                                <option value="at_risk">{isChinese ? '有风险' : 'At Risk'}</option>
                                <option value="behind">{isChinese ? '落后' : 'Behind'}</option>
                                <option value="completed">{isChinese ? '已完成' : 'Completed'}</option>
                            </select>
                        </div>
                    </div>
                    <input
                        type="text"
                        value={editNote}
                        onChange={e => setEditNote(e.target.value)}
                        placeholder={isChinese ? '更新说明（可选）' : 'Update note (optional)'}
                        style={{
                            padding: '6px 10px',
                            background: 'var(--bg-primary)',
                            border: '1px solid var(--border-subtle)',
                            borderRadius: '4px', color: 'var(--text-primary)',
                            fontSize: '12px',
                        }}
                    />
                    <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end' }}>
                        <button
                            onClick={() => setEditing(false)}
                            style={{
                                padding: '5px 12px', borderRadius: '4px',
                                border: '1px solid var(--border-subtle)',
                                background: 'none', color: 'var(--text-secondary)',
                                fontSize: '12px', cursor: 'pointer',
                            }}
                        >
                            {isChinese ? '取消' : 'Cancel'}
                        </button>
                        <button
                            onClick={handleSave}
                            disabled={saving}
                            style={{
                                padding: '5px 12px', borderRadius: '4px',
                                border: 'none',
                                background: 'var(--accent-primary)', color: '#fff',
                                fontSize: '12px', cursor: saving ? 'wait' : 'pointer',
                                opacity: saving ? 0.7 : 1,
                            }}
                        >
                            {saving ? (isChinese ? '保存中...' : 'Saving...') : (isChinese ? '保存' : 'Save')}
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}

function AddKRForm({
    objectiveId,
    periodStart,
    periodEnd,
    isChinese,
    onCreated,
    onCancel,
}: {
    objectiveId: string;
    periodStart: string;
    periodEnd: string;
    isChinese: boolean;
    onCreated: () => void;
    onCancel: () => void;
}) {
    const [title, setTitle] = useState('');
    const [targetValue, setTargetValue] = useState('100');
    const [unit, setUnit] = useState('');
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');

    async function handleSubmit() {
        if (!title.trim()) { setError(isChinese ? '请输入 Key Result 描述' : 'Please enter a description'); return; }
        setSaving(true);
        setError('');
        try {
            await fetchJson(`/okr/objectives/${objectiveId}/key-results`, {
                method: 'POST',
                body: JSON.stringify({
                    title: title.trim(),
                    target_value: parseFloat(targetValue) || 100,
                    unit: unit.trim() || undefined,
                }),
            });
            onCreated();
        } catch (e: any) {
            setError(e.message ?? 'Error');
        } finally {
            setSaving(false);
        }
    }

    return (
        <div style={{
            padding: '12px 14px',
            background: 'var(--bg-tertiary)',
            border: '1px dashed var(--border-subtle)',
            borderRadius: '8px',
            display: 'flex', flexDirection: 'column', gap: '8px',
        }}>
            <input
                type="text"
                value={title}
                onChange={e => setTitle(e.target.value)}
                placeholder={isChinese ? 'Key Result 描述，例如：用户满意度达到 4.5 分' : 'e.g. Increase NPS to 50'}
                autoFocus
                style={{
                    padding: '6px 10px',
                    background: 'var(--bg-primary)',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: '4px', color: 'var(--text-primary)',
                    fontSize: '13px',
                }}
                onKeyDown={e => { if (e.key === 'Enter') handleSubmit(); if (e.key === 'Escape') onCancel(); }}
            />
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                    {isChinese ? '目标值' : 'Target'}
                </span>
                <input
                    type="number"
                    value={targetValue}
                    onChange={e => setTargetValue(e.target.value)}
                    style={{
                        width: 80, padding: '4px 8px',
                        background: 'var(--bg-primary)',
                        border: '1px solid var(--border-subtle)',
                        borderRadius: '4px', color: 'var(--text-primary)',
                        fontSize: '13px',
                    }}
                />
                <input
                    type="text"
                    value={unit}
                    onChange={e => setUnit(e.target.value)}
                    placeholder={isChinese ? '单位（可选，如 %、万元）' : 'Unit (e.g. %, pts)'}
                    style={{
                        flex: 1, padding: '4px 8px',
                        background: 'var(--bg-primary)',
                        border: '1px solid var(--border-subtle)',
                        borderRadius: '4px', color: 'var(--text-primary)',
                        fontSize: '12px',
                    }}
                />
            </div>
            {error && <div style={{ fontSize: '12px', color: '#ef4444' }}>{error}</div>}
            <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end' }}>
                <button onClick={onCancel} style={{ padding: '5px 12px', borderRadius: '4px', border: '1px solid var(--border-subtle)', background: 'none', color: 'var(--text-secondary)', fontSize: '12px', cursor: 'pointer' }}>
                    {isChinese ? '取消' : 'Cancel'}
                </button>
                <button onClick={handleSubmit} disabled={saving} style={{ padding: '5px 12px', borderRadius: '4px', border: 'none', background: 'var(--accent-primary)', color: '#fff', fontSize: '12px', cursor: saving ? 'wait' : 'pointer', opacity: saving ? 0.7 : 1 }}>
                    {saving ? (isChinese ? '创建中...' : 'Creating...') : (isChinese ? '添加 KR' : 'Add KR')}
                </button>
            </div>
        </div>
    );
}

export function ObjectiveCard({
    obj,
    isChinese,
    canEdit,
    onInvalidate,
    onDelete,
}: {
    obj: Objective;
    isChinese: boolean;
    canEdit: boolean;
    onInvalidate: () => void;
    onDelete?: (objId: string) => void;
}) {
    const dialog = useDialog();
    const [expanded, setExpanded] = useState(true);
    const [addingKR, setAddingKR] = useState(false);
    const pct = objectiveProgress(obj);
    const overallStatus = obj.status === 'completed' ? 'completed' : deriveStatus(pct);

    async function handleKRProgressUpdate(krId: string, value: number, status: string, note: string) {
        await fetchJson(`/okr/key-results/${krId}/progress`, {
            method: 'POST',
            body: JSON.stringify({ value, status: status === 'auto' ? undefined : status, note: note || undefined }),
        });
        onInvalidate();
    }

    return (
        <div style={{
            border: '1px solid var(--border-subtle)',
            borderRadius: '10px',
            overflow: 'hidden',
            background: 'var(--bg-primary)',
        }}>
            <div
                role="button"
                tabIndex={0}
                style={{
                    padding: '14px 16px',
                    display: 'flex', alignItems: 'flex-start', gap: '12px',
                    cursor: 'pointer',
                    borderBottom: expanded ? '1px solid var(--border-subtle)' : 'none',
                }}
                onClick={() => setExpanded(v => !v)}
                onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') setExpanded(v => !v); }}
            >
                <svg
                    width="14" height="14" viewBox="0 0 24 24" fill="none"
                    stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                    style={{ flexShrink: 0, color: 'var(--text-tertiary)', transform: expanded ? 'rotate(0)' : 'rotate(-90deg)', transition: 'transform 0.2s', marginTop: '4px' }}
                >
                    <polyline points="6 9 12 15 18 9" />
                </svg>

                <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ paddingRight: '12px' }}>
                        <div style={{ fontSize: '15px', fontWeight: 600, color: 'var(--text-primary)', lineHeight: 1.5, wordBreak: 'break-word', whiteSpace: 'normal' }}>
                            {obj.title}
                        </div>
                        {obj.description && (
                            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '4px', lineHeight: 1.5 }}>{obj.description}</div>
                        )}
                    </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexShrink: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 80 }}>
                        <ProgressBar pct={pct} status={overallStatus} />
                        <span style={{ fontSize: '12px', color: 'var(--text-secondary)', fontWeight: 500, minWidth: 30 }}>{pct}%</span>
                    </div>
                    <StatusBadge status={overallStatus} isChinese={isChinese} />
                    {canEdit && onDelete && (
                        <button
                            onClick={async e => {
                                e.stopPropagation();
                                const ok = await dialog.confirm(
                                    isChinese ? '确定要删除这个目标及其所有相关的 Key Results 吗？（此操作实际上是将目标归档）' : 'Are you sure you want to delete this Objective and all its Key Results? (This will archive the objective)',
                                    { title: isChinese ? '删除 / 归档目标' : 'Delete / Archive Objective', danger: true, confirmLabel: isChinese ? '删除 / 归档' : 'Delete / Archive' },
                                );
                                if (ok) {
                                    onDelete(obj.id);
                                }
                            }}
                            title={isChinese ? '删除 / 归档' : 'Delete / Archive'}
                            style={{
                                background: 'none', border: '1px solid transparent',
                                borderRadius: '4px', padding: '4px',
                                display: 'flex', alignItems: 'center', justifyContent: 'center',
                                color: 'var(--text-tertiary)', cursor: 'pointer', transition: 'all 0.15s',
                                marginLeft: '8px',
                            }}
                            onMouseEnter={e => {
                                (e.currentTarget as HTMLButtonElement).style.borderColor = '#ef4444';
                                (e.currentTarget as HTMLButtonElement).style.color = '#ef4444';
                            }}
                            onMouseLeave={e => {
                                (e.currentTarget as HTMLButtonElement).style.borderColor = 'transparent';
                                (e.currentTarget as HTMLButtonElement).style.color = 'var(--text-tertiary)';
                            }}
                        >
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                        </button>
                    )}
                </div>
            </div>

            {expanded && (
                <div style={{ padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {obj.key_results.map(kr => (
                        <KRCard
                            key={kr.id}
                            kr={kr}
                            isChinese={isChinese}
                            onUpdateProgress={handleKRProgressUpdate}
                            onDelete={async (krId) => {
                                await fetchJson(`/okr/key-results/${krId}`, { method: 'DELETE' });
                                onInvalidate();
                            }}
                            canEdit={canEdit}
                        />
                    ))}
                    {obj.key_results.length === 0 && !addingKR && (
                        <div style={{ color: 'var(--text-tertiary)', fontSize: '13px', textAlign: 'center', padding: '8px 0' }}>
                            {isChinese ? '暂无 Key Results' : 'No Key Results yet'}
                        </div>
                    )}
                    {addingKR && (
                        <AddKRForm
                            objectiveId={obj.id}
                            periodStart={obj.period_start}
                            periodEnd={obj.period_end}
                            isChinese={isChinese}
                            onCreated={() => { setAddingKR(false); onInvalidate(); }}
                            onCancel={() => setAddingKR(false)}
                        />
                    )}
                    {canEdit && !addingKR && (
                        <button
                            id={`add-kr-${obj.id}`}
                            onClick={e => { e.stopPropagation(); setAddingKR(true); }}
                            style={{
                                display: 'flex', alignItems: 'center', gap: '6px',
                                padding: '6px 10px', borderRadius: '6px',
                                border: '1px dashed var(--border-subtle)',
                                background: 'none', color: 'var(--text-tertiary)',
                                fontSize: '12px', cursor: 'pointer',
                                transition: 'all 0.15s', alignSelf: 'flex-start',
                            }}
                            onMouseEnter={e => {
                                (e.currentTarget as HTMLButtonElement).style.borderColor = 'var(--accent-primary)';
                                (e.currentTarget as HTMLButtonElement).style.color = 'var(--accent-primary)';
                            }}
                            onMouseLeave={e => {
                                (e.currentTarget as HTMLButtonElement).style.borderColor = 'var(--border-subtle)';
                                (e.currentTarget as HTMLButtonElement).style.color = 'var(--text-tertiary)';
                            }}
                        >
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                            {isChinese ? '添加 Key Result' : 'Add Key Result'}
                        </button>
                    )}
                </div>
            )}
        </div>
    );
}

export function CreateObjectiveForm({
    isChinese,
    isAdmin,
    userId,
    selectedPeriod,
    onCreated,
    onCancel,
}: {
    isChinese: boolean;
    isAdmin: boolean;
    userId: string;
    selectedPeriod: Period;
    onCreated: () => void;
    onCancel: () => void;
}) {
    const [title, setTitle] = useState('');
    const [description, setDescription] = useState('');
    const [ownerType, setOwnerType] = useState<'company' | 'user'>(isAdmin ? 'company' : 'user');
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');

    async function handleSubmit() {
        if (!title.trim()) { setError(isChinese ? '请输入目标标题' : 'Please enter a title'); return; }
        setSaving(true);
        setError('');
        try {
            await fetchJson('/okr/objectives', {
                method: 'POST',
                body: JSON.stringify({
                    title: title.trim(),
                    description: description.trim() || undefined,
                    user_id: ownerType === 'user' ? userId : undefined,
                    period_start: selectedPeriod.start,
                    period_end: selectedPeriod.end,
                }),
            });
            onCreated();
        } catch (e: any) {
            setError(e.message ?? 'Error');
        } finally {
            setSaving(false);
        }
    }

    return (
        <div style={{
            padding: '16px',
            background: 'var(--bg-primary)',
            border: '1px solid var(--accent-primary)40',
            borderRadius: '10px',
            display: 'flex', flexDirection: 'column', gap: '12px',
        }}>
            <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                {isChinese ? '新建目标' : 'New Objective'}
            </div>
            <input
                type="text"
                value={title}
                onChange={e => setTitle(e.target.value)}
                placeholder={isChinese ? '目标标题，例如：提升用户体验' : 'e.g. Improve customer experience'}
                autoFocus
                style={{
                    padding: '8px 12px',
                    background: 'var(--bg-secondary)',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: '6px', color: 'var(--text-primary)',
                    fontSize: '14px',
                }}
                onKeyDown={e => { if (e.key === 'Enter') handleSubmit(); if (e.key === 'Escape') onCancel(); }}
            />
            <textarea
                value={description}
                onChange={e => setDescription(e.target.value)}
                placeholder={isChinese ? '说明（可选）' : 'Description (optional)'}
                rows={2}
                style={{
                    padding: '8px 12px',
                    background: 'var(--bg-secondary)',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: '6px', color: 'var(--text-primary)',
                    fontSize: '13px', resize: 'vertical',
                    fontFamily: 'inherit',
                }}
            />
            {isAdmin && (
                <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                    <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                        {isChinese ? '层级' : 'Level'}
                    </span>
                    <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer' }}>
                        <input type="radio" name="ownerType" value="company" checked={ownerType === 'company'} onChange={() => setOwnerType('company')} />
                        <span style={{ fontSize: '12px', color: 'var(--text-primary)' }}>{isChinese ? '公司级' : 'Company'}</span>
                    </label>
                    <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer' }}>
                        <input type="radio" name="ownerType" value="user" checked={ownerType === 'user'} onChange={() => setOwnerType('user')} />
                        <span style={{ fontSize: '12px', color: 'var(--text-primary)' }}>{isChinese ? '个人' : 'Personal'}</span>
                    </label>
                </div>
            )}
            {error && <div style={{ fontSize: '12px', color: '#ef4444' }}>{error}</div>}
            <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                <button onClick={onCancel} style={{ padding: '7px 14px', borderRadius: '6px', border: '1px solid var(--border-subtle)', background: 'none', color: 'var(--text-secondary)', fontSize: '13px', cursor: 'pointer' }}>
                    {isChinese ? '取消' : 'Cancel'}
                </button>
                <button onClick={handleSubmit} disabled={saving} style={{ padding: '7px 14px', borderRadius: '6px', border: 'none', background: 'var(--accent-primary)', color: '#fff', fontSize: '13px', cursor: saving ? 'wait' : 'pointer', opacity: saving ? 0.7 : 1 }}>
                    {saving ? (isChinese ? '创建中...' : 'Creating...') : (isChinese ? '创建目标' : 'Create')}
                </button>
            </div>
        </div>
    );
}
