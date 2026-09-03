import { useEffect, useState } from 'react';
import { IconChevronDown, IconChevronRight } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';

import Button from '../../../../components/ui/Button';
import { fetchJson } from '../../utils/fetchJson';

type Department = {
    id: string;
    name: string;
    member_count?: number;
    has_children?: boolean;
};

type DirectoryPage = {
    items: Department[];
    total_member: number;
};

const ROOT_KEY = '__root__';

export default function DepartmentTree({
    tenantId,
    providerId,
    selectedDepartmentId,
    onSelect,
    reloadKey,
}: {
    tenantId: string;
    providerId: string;
    selectedDepartmentId: string | null;
    onSelect: (id: string | null) => void;
    reloadKey?: string;
}) {
    const { t } = useTranslation();
    const [childrenByParent, setChildrenByParent] = useState<Record<string, Department[]>>({});
    const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
    const [loadingIds, setLoadingIds] = useState<Set<string>>(new Set());
    const [failedIds, setFailedIds] = useState<Set<string>>(new Set());
    const [totalMember, setTotalMember] = useState(0);

    const loadChildren = async (parentId: string | null, force = false) => {
        const key = parentId || ROOT_KEY;
        if (!force && (childrenByParent[key] || loadingIds.has(key))) return;
        setLoadingIds((current) => new Set(current).add(key));
        setFailedIds((current) => {
            const next = new Set(current);
            next.delete(key);
            return next;
        });
        try {
            const params = new URLSearchParams({ provider_id: providerId });
            if (tenantId) params.set('tenant_id', tenantId);
            if (parentId) params.set('parent_id', parentId);
            const page = await fetchJson<DirectoryPage>(`/enterprise/org/departments?${params}`);
            setChildrenByParent((current) => ({ ...current, [key]: page.items }));
            if (!parentId) setTotalMember(page.total_member || 0);
        } catch {
            setFailedIds((current) => new Set(current).add(key));
        } finally {
            setLoadingIds((current) => {
                const next = new Set(current);
                next.delete(key);
                return next;
            });
        }
    };

    useEffect(() => {
        setChildrenByParent({});
        setExpandedIds(new Set());
        setFailedIds(new Set());
        setTotalMember(0);
        void loadChildren(null, true);
        // A completed synchronization invalidates the provider tree snapshot.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [tenantId, providerId, reloadKey]);

    const toggle = (department: Department) => {
        onSelect(department.id);
        if (!department.has_children) return;
        const willExpand = !expandedIds.has(department.id);
        setExpandedIds((current) => {
            const next = new Set(current);
            if (willExpand) next.add(department.id);
            else next.delete(department.id);
            return next;
        });
        if (willExpand) void loadChildren(department.id);
    };

    const renderBranch = (parentId: string | null, depth: number): React.ReactNode => {
        const key = parentId || ROOT_KEY;
        return (childrenByParent[key] || []).map((department) => {
            const expanded = expandedIds.has(department.id);
            const selected = selectedDepartmentId === department.id;
            return (
                <div key={department.id}>
                    <Button
                        type="button"
                        variant="ghost"
                        aria-expanded={department.has_children ? expanded : undefined}
                        title={department.name}
                        onClick={() => toggle(department)}
                        style={{
                            width: '100%',
                            minHeight: '30px',
                            padding: '5px 8px',
                            paddingLeft: `${8 + depth * 16}px`,
                            borderRadius: '4px',
                            marginBottom: '1px',
                            background: selected ? 'rgba(224,238,238,0.12)' : 'transparent',
                            color: 'var(--text-primary)',
                            justifyContent: 'space-between',
                            textAlign: 'left',
                        }}
                    >
                        <span style={{ display: 'flex', alignItems: 'center', minWidth: 0 }}>
                            <span style={{ width: '16px', color: 'var(--text-tertiary)', display: 'inline-flex' }}>
                                {department.has_children && (expanded
                                    ? <IconChevronDown size={14} aria-hidden="true" />
                                    : <IconChevronRight size={14} aria-hidden="true" />)}
                            </span>
                            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                {department.name}
                            </span>
                        </span>
                        <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
                            {department.member_count || 0}
                        </span>
                    </Button>
                    {expanded && (
                        <>
                            {loadingIds.has(department.id) && (
                                <div style={{ padding: '5px 8px', paddingLeft: `${24 + depth * 16}px`, color: 'var(--text-tertiary)', fontSize: '11px' }}>
                                    {t('enterprise.org.directoryLoading')}
                                </div>
                            )}
                            {failedIds.has(department.id) && (
                                <Button
                                    type="button"
                                    variant="ghost"
                                    onClick={() => void loadChildren(department.id, true)}
                                    style={{ marginLeft: `${24 + depth * 16}px`, fontSize: '11px' }}
                                >
                                    {t('enterprise.org.directoryLoadRetry')}
                                </Button>
                            )}
                            {renderBranch(department.id, depth + 1)}
                        </>
                    )}
                </div>
            );
        });
    };

    return (
        <>
            <Button
                type="button"
                variant="ghost"
                onClick={() => onSelect(null)}
                style={{
                    width: '100%',
                    minHeight: '30px',
                    padding: '5px 8px',
                    justifyContent: 'space-between',
                    background: !selectedDepartmentId ? 'rgba(224,238,238,0.1)' : 'transparent',
                }}
            >
                <span>{t('common.all')}</span>
                {totalMember > 0 && <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>({totalMember})</span>}
            </Button>
            {loadingIds.has(ROOT_KEY) && (
                <div style={{ padding: '6px 8px', color: 'var(--text-tertiary)', fontSize: '11px' }}>
                    {t('enterprise.org.directoryLoading')}
                </div>
            )}
            {failedIds.has(ROOT_KEY) && (
                <Button type="button" variant="ghost" onClick={() => void loadChildren(null, true)}>
                    {t('enterprise.org.directoryLoadRetry')}
                </Button>
            )}
            {renderBranch(null, 0)}
        </>
    );
}
