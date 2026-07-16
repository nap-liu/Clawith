import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
    IconBuilding,
    IconCheck,
    IconChevronDown,
    IconChevronRight,
    IconSearch,
    IconUsers,
    IconX,
} from '@tabler/icons-react';

import { fetchJson } from '../services/api';
import './OrgMemberAccessPicker.css';

export type AgentAccessUser = {
    id: string;
    name: string;
    username?: string;
    email?: string;
    title?: string;
    avatar_url?: string | null;
    department_path?: string;
    access_level: 'use' | 'manage';
    is_required?: boolean;
    required_reason?: 'creator' | 'company_admin' | string | null;
};

export type AgentAccessDepartment = {
    id: string;
    name: string;
    path: string;
    access_level: 'use' | 'manage';
    include_descendants?: boolean;
};

type DirectoryDepartment = {
    id: string;
    name: string;
    parent_id: string | null;
    path: string;
    has_children: boolean;
    direct_member_count: number;
};

type DirectoryDepartmentsResponse = {
    items: DirectoryDepartment[];
    my_department: DirectoryDepartment | null;
};

type DirectoryMember = {
    id: string;
    member_id: string;
    name: string;
    department_id: string | null;
    department_path: string;
    title: string;
    avatar_url: string | null;
    email?: string | null;
};

type DirectoryMembersResponse = {
    items: DirectoryMember[];
    page: number;
    page_size: number;
    total: number;
    has_more: boolean;
};

type Props = {
    open: boolean;
    agentId: string;
    users: AgentAccessUser[];
    departments: AgentAccessDepartment[];
    onClose: () => void;
    onSave: (users: AgentAccessUser[], departments: AgentAccessDepartment[]) => Promise<void>;
};

const ROOT_KEY = '__root__';
const PAGE_SIZE = 50;

function useDebouncedValue(value: string, delayMs: number) {
    const [debounced, setDebounced] = useState(value);
    useEffect(() => {
        const timer = window.setTimeout(() => setDebounced(value), delayMs);
        return () => window.clearTimeout(timer);
    }, [value, delayMs]);
    return debounced;
}

function compactDepartmentPath(path?: string) {
    if (!path) return '';
    const parts = path.split('/').filter(part => part && part.toLowerCase() !== 'root');
    return parts.slice(-3).join(' / ');
}

function initials(name: string) {
    return name.trim().slice(-2) || '?';
}

export default function OrgMemberAccessPicker({ open, agentId, users, departments, onClose, onSave }: Props) {
    const { i18n } = useTranslation();
    const isChinese = i18n.language?.startsWith('zh');
    const labels = isChinese ? {
        title: '选择可访问成员',
        subtitle: '新增成员默认获得“使用”权限',
        search: '搜索姓名、拼音或部门路径...',
        organization: '组织架构',
        myDepartment: '我的部门',
        directOnly: '仅直属成员',
        includeDescendants: '包含下级成员',
        grantDepartment: '授权整个部门节点',
        grantDepartmentHint: '包含所有下级部门，人员入职、离职或调岗后自动生效',
        selectedDepartments: '已选部门',
        selectedMembers: '已选成员',
        individualMembers: '单独选择成员',
        selectDirect: '选择本部门直属成员',
        selectPage: '选择当前页成员',
        selected: '已选成员',
        systemManagers: '系统保留管理者',
        creator: 'Agent 创建者',
        companyAdmins: '公司管理员',
        use: '使用',
        manage: '管理',
        cancel: '取消',
        save: '保存设置',
        saving: '保存中...',
        noDepartments: '没有匹配的部门',
        noMembers: '没有匹配的成员',
        loading: '加载中...',
        previous: '上一页',
        next: '下一页',
        businessMembers: '名业务成员',
        page: '页',
    } : {
        title: 'Choose Members',
        subtitle: 'New members receive Use access by default',
        search: 'Search by name, transliteration, or department...',
        organization: 'Organization',
        myDepartment: 'My department',
        directOnly: 'Direct members',
        includeDescendants: 'Include descendants',
        grantDepartment: 'Grant this department node',
        grantDepartmentHint: 'Includes all descendant departments and follows future organization changes',
        selectedDepartments: 'Selected departments',
        selectedMembers: 'Selected members',
        individualMembers: 'Select individual members',
        selectDirect: 'Select direct members',
        selectPage: 'Select this page',
        selected: 'Selected',
        systemManagers: 'System managers',
        creator: 'Agent creator',
        companyAdmins: 'Company administrators',
        use: 'Use',
        manage: 'Manage',
        cancel: 'Cancel',
        save: 'Save',
        saving: 'Saving...',
        noDepartments: 'No matching departments',
        noMembers: 'No matching members',
        loading: 'Loading...',
        previous: 'Previous',
        next: 'Next',
        businessMembers: ' business members',
        page: 'page',
    };

    const requiredUsers = useMemo(() => users.filter(user => user.is_required), [users]);
    const requiredIds = useMemo(() => new Set(requiredUsers.map(user => user.id)), [requiredUsers]);
    const businessUsers = useMemo(() => users.filter(user => !user.is_required), [users]);

    const [draftUsers, setDraftUsers] = useState<Map<string, AgentAccessUser>>(new Map());
    const [draftDepartments, setDraftDepartments] = useState<Map<string, AgentAccessDepartment>>(new Map());
    const [departmentsById, setDepartmentsById] = useState<Record<string, DirectoryDepartment>>({});
    const [childrenByParent, setChildrenByParent] = useState<Record<string, string[]>>({});
    const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
    const [myDepartment, setMyDepartment] = useState<DirectoryDepartment | null>(null);
    const [selectedDepartmentId, setSelectedDepartmentId] = useState<string | null>(null);
    const [departmentSearch, setDepartmentSearch] = useState('');
    const [memberSearch, setMemberSearch] = useState('');
    const [includeDescendants, setIncludeDescendants] = useState(false);
    const [page, setPage] = useState(1);
    const [saving, setSaving] = useState(false);
    const [saveError, setSaveError] = useState<string | null>(null);
    const [treeError, setTreeError] = useState<string | null>(null);
    const loadedParentsRef = useRef<Set<string>>(new Set());
    const loadingParentsRef = useRef<Set<string>>(new Set());
    const searchInputRef = useRef<HTMLInputElement>(null);
    const wasOpenRef = useRef(false);

    const debouncedDepartmentSearch = useDebouncedValue(departmentSearch.trim(), 250);
    const debouncedMemberSearch = useDebouncedValue(memberSearch.trim(), 250);

    const mergeDepartments = useCallback((departments: DirectoryDepartment[]) => {
        setDepartmentsById(current => {
            const next = { ...current };
            departments.forEach(department => { next[department.id] = department; });
            return next;
        });
    }, []);

    const loadChildren = useCallback(async (parentId: string | null) => {
        const key = parentId || ROOT_KEY;
        if (loadedParentsRef.current.has(key) || loadingParentsRef.current.has(key)) return;
        loadingParentsRef.current.add(key);
        setTreeError(null);
        try {
            const params = new URLSearchParams();
            if (parentId) params.set('parent_id', parentId);
            const response = await fetchJson<DirectoryDepartmentsResponse>(
                `/agents/${agentId}/permissions/directory/departments${params.size ? `?${params}` : ''}`,
            );
            mergeDepartments(response.items);
            setChildrenByParent(current => ({ ...current, [key]: response.items.map(item => item.id) }));
            loadedParentsRef.current.add(key);
            if (!parentId && response.my_department) {
                setMyDepartment(response.my_department);
                mergeDepartments([response.my_department]);
                setSelectedDepartmentId(current => current || response.my_department?.id || null);
            } else if (!parentId && !response.my_department && response.items.length > 0) {
                setSelectedDepartmentId(current => current || response.items[0].id);
            }
        } catch (error) {
            setTreeError(error instanceof Error ? error.message : String(error));
        } finally {
            loadingParentsRef.current.delete(key);
        }
    }, [agentId, mergeDepartments]);

    useEffect(() => {
        if (!open) {
            wasOpenRef.current = false;
            return;
        }
        if (wasOpenRef.current) return;
        wasOpenRef.current = true;
        setDraftUsers(new Map(businessUsers.map(user => [user.id, { ...user }])));
        setDraftDepartments(new Map(departments.map(department => [department.id, { ...department }])));
        setDepartmentsById({});
        setChildrenByParent({});
        setExpandedIds(new Set());
        setMyDepartment(null);
        setSelectedDepartmentId(null);
        setDepartmentSearch('');
        setMemberSearch('');
        setIncludeDescendants(false);
        setPage(1);
        setSaveError(null);
        setTreeError(null);
        loadedParentsRef.current = new Set();
        loadingParentsRef.current = new Set();
        void loadChildren(null);
        const focusTimer = window.setTimeout(() => searchInputRef.current?.focus(), 80);
        return () => window.clearTimeout(focusTimer);
    }, [open, businessUsers, departments, loadChildren]);

    useEffect(() => {
        if (!open) return;
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape' && !saving) onClose();
        };
        window.addEventListener('keydown', onKeyDown);
        const previousOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        return () => {
            window.removeEventListener('keydown', onKeyDown);
            document.body.style.overflow = previousOverflow;
        };
    }, [open, saving, onClose]);

    useEffect(() => {
        setPage(1);
    }, [selectedDepartmentId, includeDescendants, debouncedMemberSearch]);

    const departmentSearchQuery = useQuery({
        queryKey: ['agent-permission-department-search', agentId, debouncedDepartmentSearch],
        queryFn: () => fetchJson<DirectoryDepartmentsResponse>(
            `/agents/${agentId}/permissions/directory/departments?search=${encodeURIComponent(debouncedDepartmentSearch)}`,
        ),
        enabled: open && !!debouncedDepartmentSearch,
        staleTime: 30_000,
    });

    const membersQuery = useQuery({
        queryKey: [
            'agent-permission-directory-members',
            agentId,
            selectedDepartmentId,
            includeDescendants,
            debouncedMemberSearch,
            page,
        ],
        queryFn: () => {
            const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
            if (debouncedMemberSearch) {
                params.set('search', debouncedMemberSearch);
            } else if (selectedDepartmentId) {
                params.set('department_id', selectedDepartmentId);
                params.set('include_descendants', String(includeDescendants));
            }
            return fetchJson<DirectoryMembersResponse>(
                `/agents/${agentId}/permissions/directory/members?${params}`,
            );
        },
        enabled: open && (!!debouncedMemberSearch || !!selectedDepartmentId),
        staleTime: 15_000,
    });

    const selectDepartment = (department: DirectoryDepartment) => {
        mergeDepartments([department]);
        setSelectedDepartmentId(department.id);
        setMemberSearch('');
        setIncludeDescendants(false);
    };

    const toggleExpanded = async (department: DirectoryDepartment) => {
        const willExpand = !expandedIds.has(department.id);
        setExpandedIds(current => {
            const next = new Set(current);
            if (willExpand) next.add(department.id);
            else next.delete(department.id);
            return next;
        });
        if (willExpand) await loadChildren(department.id);
    };

    const addMember = (member: DirectoryMember) => {
        if (requiredIds.has(member.id)) return;
        setDraftUsers(current => {
            const next = new Map(current);
            if (next.has(member.id)) next.delete(member.id);
            else {
                next.set(member.id, {
                    id: member.id,
                    name: member.name,
                    email: member.email || undefined,
                    title: member.title,
                    avatar_url: member.avatar_url,
                    department_path: member.department_path,
                    access_level: 'use',
                });
            }
            return next;
        });
    };

    const selectVisibleDirectMembers = () => {
        const items = membersQuery.data?.items || [];
        setDraftUsers(current => {
            const next = new Map(current);
            items.forEach(member => {
                if (!requiredIds.has(member.id) && !next.has(member.id)) {
                    next.set(member.id, {
                        id: member.id,
                        name: member.name,
                        email: member.email || undefined,
                        title: member.title,
                        avatar_url: member.avatar_url,
                        department_path: member.department_path,
                        access_level: 'use',
                    });
                }
            });
            return next;
        });
    };

    const updateLevel = (userId: string, accessLevel: 'use' | 'manage') => {
        setDraftUsers(current => {
            const next = new Map(current);
            const user = next.get(userId);
            if (user) next.set(userId, { ...user, access_level: accessLevel });
            return next;
        });
    };

    const removeUser = (userId: string) => {
        setDraftUsers(current => {
            const next = new Map(current);
            next.delete(userId);
            return next;
        });
    };

    const toggleDepartmentGrant = (department: DirectoryDepartment) => {
        setDraftDepartments(current => {
            const next = new Map(current);
            if (next.has(department.id)) next.delete(department.id);
            else {
                next.set(department.id, {
                    id: department.id,
                    name: department.name,
                    path: department.path,
                    access_level: 'use',
                    include_descendants: true,
                });
            }
            return next;
        });
    };

    const updateDepartmentLevel = (departmentId: string, accessLevel: 'use' | 'manage') => {
        setDraftDepartments(current => {
            const next = new Map(current);
            const department = next.get(departmentId);
            if (department) next.set(departmentId, { ...department, access_level: accessLevel });
            return next;
        });
    };

    const removeDepartment = (departmentId: string) => {
        setDraftDepartments(current => {
            const next = new Map(current);
            next.delete(departmentId);
            return next;
        });
    };

    const handleSave = async () => {
        setSaving(true);
        setSaveError(null);
        try {
            await onSave(Array.from(draftUsers.values()), Array.from(draftDepartments.values()));
            onClose();
        } catch (error) {
            setSaveError(error instanceof Error ? error.message : String(error));
        } finally {
            setSaving(false);
        }
    };

    const renderTree = (parentKey: string, depth = 0): React.ReactNode => {
        const childIds = childrenByParent[parentKey] || [];
        return childIds.map(id => {
            const department = departmentsById[id];
            if (!department) return null;
            const expanded = expandedIds.has(id);
            const selected = selectedDepartmentId === id;
            return (
                <div key={id}>
                    <div className={`org-access-picker__tree-row${selected ? ' is-selected' : ''}`} style={{ paddingLeft: `${depth * 14}px` }}>
                        {department.has_children ? (
                            <button
                                type="button"
                                className="org-access-picker__icon-button"
                                aria-label={expanded ? 'Collapse department' : 'Expand department'}
                                onClick={() => void toggleExpanded(department)}
                            >
                                {expanded ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
                            </button>
                        ) : <span className="org-access-picker__tree-spacer" />}
                        <button
                            type="button"
                            className="org-access-picker__tree-name"
                            onClick={() => selectDepartment(department)}
                            title={department.path}
                        >
                            <span>{department.name}</span>
                            {department.direct_member_count > 0 && (
                                <span className="org-access-picker__count">{department.direct_member_count}</span>
                            )}
                        </button>
                    </div>
                    {expanded && renderTree(id, depth + 1)}
                </div>
            );
        });
    };

    if (!open || typeof document === 'undefined') return null;

    const selectedDepartment = selectedDepartmentId ? departmentsById[selectedDepartmentId] : null;
    const selectedDepartmentGrant = selectedDepartmentId ? draftDepartments.get(selectedDepartmentId) : null;
    const memberData = membersQuery.data;
    const selectedCount = draftUsers.size;
    const selectedDepartmentCount = draftDepartments.size;
    const companyAdminCount = requiredUsers.filter(user => user.required_reason === 'company_admin').length;
    const hasCreator = requiredUsers.some(user => user.required_reason === 'creator');
    const searchResults = departmentSearchQuery.data?.items || [];

    return createPortal(
        <div
            className="org-access-picker__overlay"
            onMouseDown={event => {
                if (event.target === event.currentTarget && !saving) onClose();
            }}
        >
            <section className="org-access-picker" role="dialog" aria-modal="true" aria-labelledby="org-access-picker-title">
                <header className="org-access-picker__header">
                    <div>
                        <h3 id="org-access-picker-title"><IconUsers size={18} /> {labels.title}</h3>
                        <p>{labels.subtitle}</p>
                    </div>
                    <button type="button" className="org-access-picker__close" onClick={onClose} disabled={saving} aria-label={labels.cancel}>
                        <IconX size={18} />
                    </button>
                </header>

                <div className="org-access-picker__global-search">
                    <IconSearch size={16} />
                    <input
                        ref={searchInputRef}
                        value={memberSearch}
                        onChange={event => setMemberSearch(event.target.value)}
                        placeholder={labels.search}
                        aria-label={labels.search}
                    />
                    {memberSearch && (
                        <button type="button" onClick={() => setMemberSearch('')} aria-label="Clear search"><IconX size={14} /></button>
                    )}
                </div>

                <div className="org-access-picker__body">
                    <aside className="org-access-picker__departments">
                        <div className="org-access-picker__panel-title"><IconBuilding size={15} /> {labels.organization}</div>
                        {myDepartment && (
                            <button
                                type="button"
                                className={`org-access-picker__my-department${selectedDepartmentId === myDepartment.id ? ' is-selected' : ''}`}
                                onClick={() => selectDepartment(myDepartment)}
                                title={myDepartment.path}
                            >
                                <span>{labels.myDepartment}</span>
                                <small>{myDepartment.name}</small>
                            </button>
                        )}
                        <div className="org-access-picker__department-search">
                            <IconSearch size={13} />
                            <input
                                value={departmentSearch}
                                onChange={event => setDepartmentSearch(event.target.value)}
                                placeholder={isChinese ? '搜索部门...' : 'Search departments...'}
                                aria-label={isChinese ? '搜索部门' : 'Search departments'}
                            />
                        </div>
                        <div className="org-access-picker__tree">
                            {debouncedDepartmentSearch ? (
                                departmentSearchQuery.isLoading ? <div className="org-access-picker__empty">{labels.loading}</div>
                                    : searchResults.length > 0 ? searchResults.map(department => (
                                        <button
                                            key={department.id}
                                            type="button"
                                            className={`org-access-picker__search-result${selectedDepartmentId === department.id ? ' is-selected' : ''}`}
                                            onClick={() => selectDepartment(department)}
                                        >
                                            <span>{department.name}</span>
                                            <small>{compactDepartmentPath(department.path)}</small>
                                        </button>
                                    )) : <div className="org-access-picker__empty">{labels.noDepartments}</div>
                            ) : (
                                <>
                                    {renderTree(ROOT_KEY)}
                                    {treeError && <div className="org-access-picker__error">{treeError}</div>}
                                    {!treeError && !(childrenByParent[ROOT_KEY]?.length) && <div className="org-access-picker__empty">{labels.loading}</div>}
                                </>
                            )}
                        </div>
                    </aside>

                    <main className="org-access-picker__members">
                        <div className="org-access-picker__member-heading">
                            <div>
                                <div className="org-access-picker__path">
                                    {debouncedMemberSearch
                                        ? (isChinese ? '全公司搜索结果' : 'Company search results')
                                        : compactDepartmentPath(selectedDepartment?.path || myDepartment?.path)}
                                </div>
                                <strong>{memberData?.total ?? 0} {isChinese ? '人' : 'members'}</strong>
                            </div>
                            {!debouncedMemberSearch && selectedDepartmentId && (
                                <label className="org-access-picker__descendants-toggle">
                                    <input
                                        type="checkbox"
                                        checked={includeDescendants}
                                        onChange={event => setIncludeDescendants(event.target.checked)}
                                    />
                                    <span>{includeDescendants ? labels.includeDescendants : labels.directOnly}</span>
                                </label>
                            )}
                        </div>
                        {!debouncedMemberSearch && selectedDepartment && (
                            <label className={`org-access-picker__department-grant${selectedDepartmentGrant ? ' is-selected' : ''}`}>
                                <input
                                    type="checkbox"
                                    checked={!!selectedDepartmentGrant}
                                    onChange={() => toggleDepartmentGrant(selectedDepartment)}
                                />
                                <span>
                                    <strong>{labels.grantDepartment}</strong>
                                    <b>{selectedDepartment.name}</b>
                                    <small>{labels.grantDepartmentHint}</small>
                                </span>
                            </label>
                        )}
                        <div className="org-access-picker__subsection-title">{labels.individualMembers}</div>
                        {!debouncedMemberSearch && !includeDescendants && !!memberData?.items.length && (
                            <button type="button" className="org-access-picker__select-direct" onClick={selectVisibleDirectMembers}>
                                <IconCheck size={14} /> {memberData.has_more ? labels.selectPage : labels.selectDirect}
                            </button>
                        )}
                        <div className="org-access-picker__member-list">
                            {membersQuery.isLoading ? <div className="org-access-picker__empty">{labels.loading}</div>
                                : membersQuery.isError ? <div className="org-access-picker__error">{membersQuery.error instanceof Error ? membersQuery.error.message : String(membersQuery.error)}</div>
                                    : memberData?.items.length ? memberData.items.map(member => {
                                        const selected = draftUsers.has(member.id) || requiredIds.has(member.id);
                                        const required = requiredIds.has(member.id);
                                        return (
                                            <label key={member.id} className={`org-access-picker__member-row${selected ? ' is-selected' : ''}${required ? ' is-required' : ''}`}>
                                                <input
                                                    type="checkbox"
                                                    checked={selected}
                                                    disabled={required}
                                                    onChange={() => addMember(member)}
                                                />
                                                {member.avatar_url ? <img src={member.avatar_url} alt="" /> : <span className="org-access-picker__avatar">{initials(member.name)}</span>}
                                                <span className="org-access-picker__member-copy">
                                                    <strong>{member.name}</strong>
                                                    <small>{[compactDepartmentPath(member.department_path), member.title].filter(Boolean).join(' · ')}</small>
                                                </span>
                                                {required && <span className="badge">{isChinese ? '系统保留' : 'Required'}</span>}
                                            </label>
                                        );
                                    }) : <div className="org-access-picker__empty">{labels.noMembers}</div>}
                        </div>
                        {memberData && memberData.total > PAGE_SIZE && (
                            <div className="org-access-picker__pagination">
                                <button type="button" className="btn btn-secondary btn-sm" disabled={page <= 1} onClick={() => setPage(current => current - 1)}>{labels.previous}</button>
                                <span>{labels.page} {page} / {Math.ceil(memberData.total / PAGE_SIZE)}</span>
                                <button type="button" className="btn btn-secondary btn-sm" disabled={!memberData.has_more} onClick={() => setPage(current => current + 1)}>{labels.next}</button>
                            </div>
                        )}
                    </main>

                    <aside className="org-access-picker__selected">
                        <div className="org-access-picker__panel-title">{labels.selected} <span>{selectedDepartmentCount + selectedCount}</span></div>
                        <div className="org-access-picker__selected-section-title">{labels.selectedDepartments} <span>{selectedDepartmentCount}</span></div>
                        <div className="org-access-picker__selected-list">
                            {selectedDepartmentCount > 0 ? Array.from(draftDepartments.values()).map(department => (
                                <div key={department.id} className="org-access-picker__selected-row org-access-picker__selected-row--department">
                                    <IconBuilding size={15} />
                                    <div className="org-access-picker__selected-copy">
                                        <strong>{department.name}</strong>
                                        <small>{compactDepartmentPath(department.path)} · {labels.includeDescendants}</small>
                                    </div>
                                    <select
                                        value={department.access_level}
                                        onChange={event => updateDepartmentLevel(department.id, event.target.value as 'use' | 'manage')}
                                        aria-label={`${department.name} access`}
                                    >
                                        <option value="use">{labels.use}</option>
                                        <option value="manage">{labels.manage}</option>
                                    </select>
                                    <button type="button" onClick={() => removeDepartment(department.id)} aria-label={`${labels.cancel} ${department.name}`}><IconX size={14} /></button>
                                </div>
                            )) : <div className="org-access-picker__empty org-access-picker__empty--compact">{labels.noDepartments}</div>}
                        </div>
                        <div className="org-access-picker__selected-section-title">{labels.selectedMembers} <span>{selectedCount}</span></div>
                        <div className="org-access-picker__selected-list">
                            {selectedCount > 0 ? Array.from(draftUsers.values()).map(user => (
                                <div key={user.id} className="org-access-picker__selected-row">
                                    <div className="org-access-picker__selected-copy">
                                        <strong>{user.name}</strong>
                                        <small>{compactDepartmentPath(user.department_path) || user.email || ''}</small>
                                    </div>
                                    <select
                                        value={user.access_level}
                                        onChange={event => updateLevel(user.id, event.target.value as 'use' | 'manage')}
                                        aria-label={`${user.name} access`}
                                    >
                                        <option value="use">{labels.use}</option>
                                        <option value="manage">{labels.manage}</option>
                                    </select>
                                    <button type="button" onClick={() => removeUser(user.id)} aria-label={`${labels.cancel} ${user.name}`}><IconX size={14} /></button>
                                </div>
                            )) : <div className="org-access-picker__empty">{labels.noMembers}</div>}
                        </div>
                        <details className="org-access-picker__required">
                            <summary>{labels.systemManagers} {requiredUsers.length}</summary>
                            <div>
                                {hasCreator && <span>{labels.creator} · {labels.manage}</span>}
                                {companyAdminCount > 0 && <span>{labels.companyAdmins} {companyAdminCount} · {labels.manage}</span>}
                            </div>
                        </details>
                    </aside>
                </div>

                <footer className="org-access-picker__footer">
                    <div>
                        <span>{selectedDepartmentCount} {labels.selectedDepartments} · {selectedCount}{labels.businessMembers}</span>
                        {saveError && <span className="org-access-picker__error">{saveError}</span>}
                    </div>
                    <div>
                        <button type="button" className="btn btn-secondary" onClick={onClose} disabled={saving}>{labels.cancel}</button>
                        <button type="button" className="btn btn-primary" onClick={() => void handleSave()} disabled={saving}>{saving ? labels.saving : labels.save}</button>
                    </div>
                </footer>
            </section>
        </div>,
        document.body,
    );
}
