import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams } from 'react-router-dom';
import {
    IconActivityHeartbeat,
    IconAlertTriangle,
    IconArchive,
    IconArrowRight,
    IconBolt,
    IconBox,
    IconBrandGit,
    IconBroadcast,
    IconChecklist,
    IconChevronRight,
    IconCircleCheck,
    IconClock,
    IconCodeDots,
    IconFile,
    IconFileText,
    IconFilter,
    IconGitBranch,
    IconHistory,
    IconLoader2,
    IconLock,
    IconMessageCircle,
    IconPlayerPause,
    IconPlayerPlay,
    IconPlus,
    IconDeviceFloppy,
    IconFlag,
    IconRefresh,
    IconRestore,
    IconSettings,
    IconShieldCheck,
    IconSparkles,
    IconTargetArrow,
    IconTool,
    IconTrash,
    IconUsers,
    IconX,
} from '@tabler/icons-react';

import { useToast } from '../../components/Toast/ToastProvider';
import MultiSelectDropdown from '../../components/ui/MultiSelectDropdown';
import SessionViewerDrawer, { type SessionViewerGroupConfig, type SessionViewerTarget } from '../../components/SessionViewerDrawer';
import { projectsApi } from '../../services/projects';
import { useAuthStore } from '../../stores';
import {
    Button,
    ProjectCountBadge,
    ProjectDataTable,
    ProjectDataTableBody,
    ProjectDataTableCell,
    ProjectDataTableHead,
    ProjectDataTableHeader,
    ProjectDataTableRow,
    ProjectDialog,
    ProjectEmptyState,
    ProjectField,
    ProjectIconButton,
    ProjectProgressBar,
    ProjectSegmentedControl,
    ProjectSelect,
    ProjectStatusBadge,
    ProjectTextarea,
    SearchInput,
    TextInput,
    ToggleSwitch,
} from './components/ProjectUI';
import type { ProjectSummary } from './types';
import {
    A2AMeshGraph,
    ProjectGraphLegend,
    SnapshotLineageGraph,
    WorkDependencyGraph,
} from './components/ProjectGraphs';
import ProjectFileWorkspace from './components/ProjectFileWorkspace';
import './projectWorkspace.css';

type RecordValue = Record<string, unknown>;
type ProjectSessionTarget = SessionViewerTarget & { agentName: string; kind?: 'group' | 'session' };
type OpenSession = (source: RecordValue, title?: string) => void;
type WorkspaceTab =
    | 'cockpit'
    | 'work'
    | 'group'
    | 'mesh'
    | 'detail'
    | 'files'
    | 'runs'
    | 'members'
    | 'capabilities'
    | 'matrix'
    | 'policies'
    | 'git'
    | 'audit';

type WorkspaceData = {
    project: ProjectSummary;
    members: RecordValue[];
    capabilities: RecordValue[];
    workItems: RecordValue[];
    runs: RecordValue[];
    events: RecordValue[];
    commits: RecordValue[];
    gitRepository: RecordValue;
    files: RecordValue[];
    policies: RecordValue | null;
    groupSession: RecordValue | null;
};

const NAV_GROUPS: Array<{ label: string; items: Array<{ id: WorkspaceTab; label: string; icon: typeof IconBolt }> }> = [
    {
        label: '运行与协作',
        items: [
            { id: 'cockpit', label: '项目驾驶舱', icon: IconActivityHeartbeat },
            { id: 'work', label: '目标与任务', icon: IconTargetArrow },
            { id: 'group', label: '项目群聊', icon: IconMessageCircle },
            { id: 'mesh', label: 'A2A Mesh', icon: IconBroadcast },
            { id: 'detail', label: '工作项详情', icon: IconChecklist },
            { id: 'files', label: '文件与交付', icon: IconFileText },
            { id: 'runs', label: '运行控制', icon: IconBolt },
        ],
    },
    {
        label: '治理与追溯',
        items: [
            { id: 'members', label: '成员与快照', icon: IconUsers },
            { id: 'capabilities', label: '能力中心', icon: IconTool },
            { id: 'matrix', label: '能力矩阵', icon: IconCodeDots },
            { id: 'policies', label: '运行与安全策略', icon: IconShieldCheck },
            { id: 'git', label: 'Git 历史与恢复', icon: IconBrandGit },
            { id: 'audit', label: '项目事件审计', icon: IconHistory },
        ],
    },
];

const obj = (value: unknown): RecordValue => (value && typeof value === 'object' ? value as RecordValue : {});
const arr = (value: unknown): RecordValue[] => Array.isArray(value) ? value.map(obj) : Array.isArray(obj(value).items) ? (obj(value).items as unknown[]).map(obj) : [];
const text = (source: RecordValue, ...keys: string[]): string => {
    for (const key of keys) {
        const value = source[key];
        if (typeof value === 'string' || typeof value === 'number') return String(value);
    }
    return '';
};
const bool = (source: RecordValue, ...keys: string[]): boolean => keys.some((key) => source[key] === true);
const num = (source: RecordValue, ...keys: string[]): number => {
    for (const key of keys) {
        const value = Number(source[key]);
        if (Number.isFinite(value)) return value;
    }
    return 0;
};
const listText = (source: RecordValue, key: string): string => Array.isArray(source[key])
    ? (source[key] as unknown[]).map((value) => String(value)).join('；')
    : text(source, key);
const dateLabel = (value: unknown): string => {
    if (!value) return '—';
    const date = new Date(String(value));
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date);
};
const compactId = (value: string): string => value.length > 12 ? value.slice(0, 8) : value;
const statusLabel = (status: string): string => ({
    planning: '规划中', initializing: '初始化中', running: '运行中', queued: '排队中', waiting: '等待中', waiting_approval: '待审批', paused: '已暂停', completed: '已完成', success: '已完成', succeeded: '已完成', cancelled: '已取消', failed: '失败', blocked: '阻塞', review: '待评审', backlog: '待规划', todo: '待处理', doing: '进行中', in_progress: '进行中', done: '已完成',
}[status] || status || '未知');

type ProjectToolDefinition = {
    name: string;
    label: string;
    description: string;
    participant: boolean;
};

const PROJECT_TOOL_REGISTRY: readonly ProjectToolDefinition[] = [
    { name: 'project_get_context', label: '读取项目上下文', description: '读取项目目标、规划信号、状态与 Git HEAD。', participant: true },
    { name: 'project_list_work_items', label: '查看工作项', description: '列出项目工作项，可限定为本人负责的工作。', participant: true },
    { name: 'project_list_files', label: '查看文件列表', description: '查看项目 Git 仓库中的产物路径与提交标识。', participant: true },
    { name: 'project_read_file', label: '读取项目文件', description: '读取一个已提交的项目文本产物。', participant: true },
    { name: 'project_update_work_item', label: '更新工作项', description: '参与者仅更新本人工作项；Leader 可编辑和指派。', participant: true },
    { name: 'project_write_file', label: '写入项目文件', description: '写入项目相对路径，并原子创建 Git 提交。', participant: true },
    { name: 'project_message_agent', label: '定向联系 Agent', description: '只向一个项目 Agent 发送消息，不做广播。', participant: true },
    { name: 'project_update_plan', label: '更新项目计划', description: '更新目标、成功标准和当前推进信号。', participant: false },
    { name: 'project_create_work_item', label: '创建工作项', description: '创建可追溯工作项，但不会自动唤醒负责人。', participant: false },
    { name: 'project_set_member_enabled', label: '启停项目成员', description: '启用或停用非 Leader 项目成员。', participant: false },
    { name: 'project_set_capability_enabled', label: '启停项目能力', description: '启用或停用现有项目能力绑定。', participant: false },
    { name: 'project_create_milestone', label: '创建 Git 里程碑', description: '创建命名里程碑提交，不改写历史。', participant: false },
    { name: 'project_restore_commit', label: '恢复为新提交', description: '把旧版本恢复成新提交，不执行 reset。', participant: false },
] as const;

const HUMAN_ONLY_PROJECT_OPERATIONS = [
    '分享或撤销项目访问',
    '指定或更换项目 Leader',
    '绑定、查看或轮换 MCP 凭证',
    '批准高风险或外部写操作',
    '归档、删除项目或改写 Git 历史',
] as const;

const stringList = (value: unknown): string[] => Array.isArray(value) ? value.map(entry => String(entry)).filter(Boolean) : [];

function projectToolResolution(tool: ProjectToolDefinition, member: RecordValue, policies: RecordValue | null) {
    const role = bool(member, 'is_leader') ? 'leader' : 'participant';
    const lifecycleBlocked = member.is_enabled === false;
    const roleCeiling = role === 'leader' || tool.participant;
    const projectPolicy = obj(obj(policies?.policies).project_tools || obj(policies).project_tools);
    const policyDisabled = new Set([
        ...stringList(projectPolicy.disabled),
        ...stringList(projectPolicy[`${role}_disabled`]),
    ]);
    const roleAllowed = Array.isArray(projectPolicy[`${role}_allowed`]) ? new Set(stringList(projectPolicy[`${role}_allowed`])) : null;
    const config = obj(member.config_snapshot);
    const memberDisabled = new Set(stringList(config.disabled_project_tools));
    const memberAllowed = Array.isArray(config.enabled_project_tools) ? new Set(stringList(config.enabled_project_tools)) : null;
    const policyBlocked = policyDisabled.has(tool.name) || Boolean(roleAllowed && !roleAllowed.has(tool.name));
    const snapshotBlocked = Boolean(memberAllowed && !memberAllowed.has(tool.name));
    return {
        role,
        roleCeiling,
        policyBlocked,
        snapshotBlocked,
        lifecycleBlocked,
        memberDisabled: memberDisabled.has(tool.name),
        effective: !lifecycleBlocked && roleCeiling && !policyBlocked && !snapshotBlocked && !memberDisabled.has(tool.name),
    };
}
const errorMessage = (error: unknown): string => error instanceof Error ? error.message : '请求失败，请稍后重试';

function parseRecord(value: unknown): RecordValue | null {
    if (value && typeof value === 'object' && !Array.isArray(value)) return value as RecordValue;
    if (typeof value !== 'string') return null;
    const source = value.trim();
    const start = source.indexOf('{');
    if (start < 0) return null;
    try {
        const parsed = JSON.parse(source.slice(start));
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as RecordValue : null;
    } catch {
        return null;
    }
}

function traceRecords(source: RecordValue): RecordValue[] {
    const records: RecordValue[] = [];
    const seen = new Set<RecordValue>();
    const visit = (value: unknown, depth: number) => {
        if (depth > 3) return;
        const record = parseRecord(value);
        if (!record || seen.has(record)) return;
        seen.add(record);
        records.push(record);
        ['event_metadata', 'metadata', 'input', 'output', 'runtime', 'result', 'delivery_result', 'message_meta', 'session', 'subagent_run'].forEach((key) => visit(record[key], depth + 1));
        if (Array.isArray(record.subagent_runs)) record.subagent_runs.forEach((item) => visit(item, depth + 1));
    };
    visit(source, 0);
    return records;
}

function traceValue(records: RecordValue[], ...keys: string[]): string {
    for (const key of keys) {
        for (const record of records) {
            const value = record[key];
            if (typeof value === 'string' || typeof value === 'number') return String(value);
        }
    }
    return '';
}

type SessionRoute = { sessionId: string; kind: 'group' | 'session' };

function sessionRouteOf(source: RecordValue): SessionRoute | null {
    const records = traceRecords(source);
    const eventType = traceValue(records, 'event_type', 'type').toLowerCase();
    const explicitMode = traceValue(records, 'session_mode', 'mode', 'kind').toLowerCase();
    const isGroup = eventType.startsWith('group.') || explicitMode === 'group';
    const sessionId = isGroup
        ? traceValue(records, 'group_session_id', 'session_id', 'conversation_id')
        : traceValue(records, 'subagent_session_id', 'session_id', 'conversation_id', 'child_session_id');
    return sessionId ? { sessionId, kind: isGroup ? 'group' : 'session' } : null;
}

function sessionIdOf(source: RecordValue): string {
    return sessionRouteOf(source)?.sessionId || '';
}

function SessionButton({ source, onOpen, label = '查看会话' }: { source: RecordValue; onOpen: OpenSession; label?: string }) {
    if (!sessionIdOf(source)) return null;
    return <Button type="button" variant="ghost" className="project-workspace__session-link" onClick={() => onOpen(source)}><IconMessageCircle size={14} />{label}</Button>;
}

function pickCollection(payload: RecordValue, ...keys: string[]): RecordValue[] {
    for (const key of keys) {
        const value = payload[key];
        const items = arr(value);
        if (items.length || Array.isArray(value) || Array.isArray(obj(value).items)) return items;
    }
    return [];
}

function StatusPill({ status }: { status: string }) {
    const tone = ['running', 'success', 'completed', 'done'].includes(status)
        ? 'success'
        : ['failed', 'blocked'].includes(status)
            ? 'error'
            : ['paused', 'waiting', 'review'].includes(status)
                ? 'warning'
                : 'neutral';
    return <ProjectStatusBadge tone={tone} className="project-workspace__status">{statusLabel(status)}</ProjectStatusBadge>;
}

const EmptyState = ProjectEmptyState;

function SectionHeading({ eyebrow, title, description, actions }: { eyebrow: string; title: string; description: string; actions?: ReactNode }) {
    return (
        <header className="project-workspace__section-heading">
            <div><span>{eyebrow}</span><h2>{title}</h2><p>{description}</p></div>
            {actions && <div className="project-workspace__heading-actions">{actions}</div>}
        </header>
    );
}

export default function ProjectWorkspacePage() {
    const routeParams = useParams<{ projectId?: string; id?: string }>();
    const projectId = routeParams.projectId || routeParams.id || '';
    const [tab, setTab] = useState<WorkspaceTab>('cockpit');
    const [data, setData] = useState<WorkspaceData | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [resourceWarnings, setResourceWarnings] = useState<string[]>([]);
    const toast = useToast();
    const [busyAction, setBusyAction] = useState('');
    const [selectedWorkItemId, setSelectedWorkItemId] = useState('');
    const [selectedMemberId, setSelectedMemberId] = useState('');
    const [selectedCommit, setSelectedCommit] = useState<RecordValue | null>(null);
    const [gitDialog, setGitDialog] = useState<'restore' | 'branch' | null>(null);
    const [sessionTarget, setSessionTarget] = useState<ProjectSessionTarget | null>(null);
    const refreshingRef = useRef(0);

    const load = useCallback(async ({ silent = false }: { silent?: boolean } = {}) => {
        if (!projectId) {
            setError('缺少项目 ID，无法加载项目工作台。');
            setLoading(false);
            return;
        }
        if (refreshingRef.current > 0 && silent) return;
        refreshingRef.current += 1;
        if (!silent) {
            setLoading(true);
            setError('');
            setResourceWarnings([]);
        }
        try {
            if (silent) {
                const dashboard = obj(await projectsApi.dashboard(projectId));
                const dashboardProject = obj(dashboard.project);
                const dashboardGit = obj(dashboard.git);
                const dashboardFiles = Array.isArray(dashboard.files) ? dashboard.files : [];
                setData((current) => current ? {
                    ...current,
                    project: { ...current.project, ...dashboardProject } as ProjectSummary,
                    workItems: pickCollection(dashboard, 'work_items', 'workItems'),
                    runs: pickCollection(dashboard, 'runs'),
                    events: pickCollection(dashboard, 'events', 'audit_events'),
                    commits: pickCollection(dashboardGit, 'commits').length ? pickCollection(dashboardGit, 'commits') : pickCollection(dashboard, 'commits'),
                    gitRepository: { ...current.gitRepository, ...dashboardGit },
                    files: dashboardFiles.map((entry) => typeof entry === 'string' ? { id: entry, path: entry, name: entry.split('/').pop() || entry } : obj(entry)),
                } : current);
                setResourceWarnings((current) => current.filter((warning) => warning !== '后台刷新暂时失败'));
                return;
            }
            const [project, dashboard] = await Promise.all([
                projectsApi.get(projectId),
                projectsApi.dashboard(projectId),
            ]);
            const resources = await Promise.allSettled([
                projectsApi.listMembers(projectId),
                projectsApi.listCapabilities(projectId),
                projectsApi.getGit(projectId),
                projectsApi.getSettings(projectId),
                projectsApi.listFiles(projectId),
                projectsApi.getGroupSession(projectId),
            ]);
            const [membersResult, capabilitiesResult, gitResult, settingsResult, filesResult, groupSessionResult] = resources;
            const warningLabels = ['成员快照', '能力绑定', 'Git 仓库', '项目策略', '项目文件', '项目群聊'];
            setResourceWarnings(resources.flatMap((result, index) => result.status === 'rejected' ? [`${warningLabels[index]}：${errorMessage(result.reason)}`] : []));
            const members = membersResult.status === 'fulfilled' ? membersResult.value : [];
            const capabilities = capabilitiesResult.status === 'fulfilled' ? capabilitiesResult.value : [];
            const git = gitResult.status === 'fulfilled' ? gitResult.value : {};
            const settings = settingsResult.status === 'fulfilled' ? settingsResult.value : null;
            const serverFiles = filesResult.status === 'fulfilled' ? filesResult.value : [];
            const payload = obj(dashboard);
            const gitPayload = obj(git);
            const rawFiles = Array.isArray(gitPayload.files) ? gitPayload.files : [];
            const projectSettings = obj(obj(payload.project).settings);
            const next: WorkspaceData = {
                project,
                members: arr(members),
                capabilities: arr(capabilities),
                workItems: pickCollection(payload, 'work_items', 'workItems'),
                runs: pickCollection(payload, 'runs'),
                events: pickCollection(payload, 'events', 'audit_events'),
                commits: pickCollection(gitPayload, 'commits'),
                gitRepository: { ...obj(obj(settings || {}).git), ...gitPayload },
                files: arr(serverFiles).length ? arr(serverFiles) : rawFiles.map((entry) => typeof entry === 'string' ? { id: entry, path: entry, name: entry.split('/').pop() || entry } : obj(entry)),
                policies: settings ? obj(settings) : payload.policies ? obj(payload.policies) : Object.keys(projectSettings).length ? projectSettings : null,
                groupSession: groupSessionResult.status === 'fulfilled' ? obj(groupSessionResult.value) : null,
            };
            setData(next);
            setSelectedWorkItemId((current) => current || text(next.workItems[0] || {}, 'id', 'work_item_id'));
            setSelectedMemberId((current) => current || text(next.members[0] || {}, 'id', 'member_id', 'agent_id'));
            setSelectedCommit((current) => current && next.commits.some(commit => text(commit, 'commit', 'hash', 'commit_hash', 'id') === text(current, 'commit', 'hash', 'commit_hash', 'id')) ? current : next.commits[0] || null);
        } catch (loadError) {
            if (silent) {
                setResourceWarnings((current) => current.includes('后台刷新暂时失败') ? current : [...current, '后台刷新暂时失败']);
            } else {
                setError(errorMessage(loadError));
                setData(null);
            }
        } finally {
            refreshingRef.current = Math.max(0, refreshingRef.current - 1);
            if (!silent) setLoading(false);
        }
    }, [projectId]);

    useEffect(() => { void load(); }, [load]);
    const fastRefresh = Boolean(data && (
        ['initializing', 'running'].includes(data.project.status)
        || data.runs.some((run) => ['queued', 'running'].includes(text(run, 'status')))
    ));
    useEffect(() => {
        const refresh = () => {
            if (document.visibilityState === 'visible') void load({ silent: true });
        };
        const intervalId = window.setInterval(refresh, fastRefresh ? 3000 : 12000);
        const onVisibilityChange = () => {
            if (document.visibilityState === 'visible') refresh();
        };
        document.addEventListener('visibilitychange', onVisibilityChange);
        return () => {
            window.clearInterval(intervalId);
            document.removeEventListener('visibilitychange', onVisibilityChange);
        };
    }, [fastRefresh, load]);
    const runAction = useCallback(async (key: string, action: () => Promise<unknown>, success: string) => {
        setBusyAction(key);
        try {
            await action();
            toast.success(success);
            await load();
            return true;
        } catch (actionError) {
            toast.error(errorMessage(actionError));
            return false;
        } finally {
            setBusyAction('');
        }
    }, [load, toast]);
    const openSession = useCallback<OpenSession>((source, title) => {
        if (!data) return;
        const sourceRecords = traceRecords(source);
        const sourceRunId = traceValue(sourceRecords, 'run_id');
        const sourceCommit = traceValue(sourceRecords, 'commit_hash', 'commit', 'hash');
        const linkedRun = data.runs.find((run) => text(run, 'id', 'run_id') === sourceRunId);
        const linkedEvents = data.events.filter((event) => {
            const records = traceRecords(event);
            return (sourceRunId && traceValue(records, 'run_id') === sourceRunId)
                || (sourceCommit && traceValue(records, 'commit_hash', 'commit', 'hash') === sourceCommit);
        });
        const sessionSources = [source, ...(linkedRun ? [linkedRun] : []), ...linkedEvents];
        const routedSource = sessionSources
            .map((entry) => ({ source: entry, route: sessionRouteOf(entry) }))
            .find((entry) => entry.route);
        if (!routedSource?.route) {
            toast.warning('该记录还没有可查看的会话锚点');
            return;
        }
        const { sessionId, kind } = routedSource.route;
        const records = sessionSources.flatMap(traceRecords);
        const group = kind === 'group' ? data.groupSession : null;
        const agentId = group
            ? text(group, 'access_agent_id', 'session_agent_id', 'agent_id')
            : traceValue(traceRecords(routedSource.source), 'session_agent_id', 'access_agent_id', 'execution_agent_id', 'subagent_agent_id', 'agent_id', 'to_agent_id', 'assignee_agent_id', 'actor_agent_id', 'from_agent_id')
                || traceValue(records, 'session_agent_id', 'access_agent_id', 'execution_agent_id', 'subagent_agent_id', 'agent_id', 'to_agent_id', 'assignee_agent_id', 'actor_agent_id', 'from_agent_id');
        if (!agentId) {
            toast.warning('会话缺少归属 Agent，暂时无法打开');
            return;
        }
        const member = data.members.find((entry) => text(entry, 'agent_id') === agentId);
        setSessionTarget({
            sessionId,
            agentId,
            agentName: kind === 'group' ? '项目群聊' : text(member || {}, 'name_snapshot', 'agent_name', 'name') || traceValue(records, 'execution_agent_name', 'agent_name', 'actor_name', 'to_agent_name', 'from_agent_name') || 'Agent',
            title: title || (kind === 'group' ? text(group || {}, 'title', 'group_name') : '') || traceValue(records, 'session_title', 'title', 'task', 'objective', 'summary') || `项目会话 ${sessionId.slice(0, 8)}`,
            status: traceValue(records, 'session_status', 'status'),
            mode: kind === 'group' ? 'group' : traceValue(records, 'session_mode', 'mode'),
            kind,
        });
    }, [data, toast]);
    const groupMembers = useMemo(() => {
        const sources = [...(data?.members || []), ...arr(data?.groupSession ? obj(data.groupSession).members : [])];
        const normalized = sources.map((member) => {
            const snapshot = obj(member.snapshot || member.member_snapshot || member.member);
            const agent = obj(member.agent || snapshot.agent);
            return {
                agentId: text(member, 'agent_id') || text(snapshot, 'agent_id') || text(agent, 'id', 'agent_id'),
                name: text(member, 'name_snapshot', 'agent_name', 'name')
                    || text(snapshot, 'name_snapshot', 'agent_name', 'name')
                    || text(agent, 'name', 'agent_name')
                    || 'Agent',
                isLeader: member.is_leader === true || snapshot.is_leader === true,
                isEnabled: member.is_enabled !== false && snapshot.is_enabled !== false,
            };
        }).filter((member) => member.agentId);
        return Array.from(new Map(normalized.map((member) => [member.agentId, member])).values());
    }, [data?.groupSession, data?.members]);
    const groupConfig = useMemo(() => {
        const group = obj(data?.groupSession);
        const configuredLimit = Number(group.max_mentions || group.mention_limit || obj(data?.policies).group_max_mentions || 4);
        const nonLeaderCount = groupMembers.filter((member) => member.isEnabled !== false && !member.isLeader).length;
        return {
            members: groupMembers,
            maxMentions: Math.min(Number.isFinite(configuredLimit) ? Math.max(0, configuredLimit) : 4, nonLeaderCount),
            loadMessages: (sessionId: string) => projectsApi.listGroupMessages(projectId, sessionId),
            sendMessage: (sessionId: string, payload: { content: string; llm_content?: string; mentions: string[]; attachments: Array<Record<string, unknown>> }) => projectsApi.sendGroupMessage(projectId, sessionId, payload),
        };
    }, [data?.groupSession, data?.policies, groupMembers, projectId]);

    if (loading) {
        return <main className="project-workspace project-workspace__state" aria-live="polite"><IconLoader2 className="project-workspace__spinner" size={28} /><strong>正在建立项目运行视图</strong><p>读取成员、能力、运行与追溯信息…</p></main>;
    }
    if (error || !data) {
        return <main className="project-workspace project-workspace__state" role="alert"><IconAlertTriangle size={28} /><strong>项目工作台未能加载</strong><p>{error || '没有可显示的项目数据。'}</p><Button variant="secondary" onClick={() => void load()}><IconRefresh size={16} />重新加载</Button></main>;
    }

    const renderContent = () => {
        switch (tab) {
            case 'cockpit': return <Cockpit data={data} onNavigate={setTab} runAction={runAction} busyAction={busyAction} />;
            case 'work': return <WorkBoard projectId={projectId} items={data.workItems} members={data.members} onSelect={(id) => { setSelectedWorkItemId(id); setTab('detail'); }} runAction={runAction} busyAction={busyAction} />;
            case 'group': return <GroupChatPanel projectId={projectId} project={data.project} members={data.members} groupSession={data.groupSession} groupConfig={groupConfig} />;
            case 'mesh': return <MeshPanel members={data.members} events={data.events} runs={data.runs} onOpenSession={openSession} />;
            case 'detail': return <WorkItemDetail projectId={projectId} items={data.workItems} members={data.members} runs={data.runs} events={data.events} files={data.files} commits={data.commits} selectedId={selectedWorkItemId} onSelect={setSelectedWorkItemId} onNavigate={setTab} onOpenSession={openSession} runAction={runAction} busyAction={busyAction} />;
            case 'files': return <FilesPanel projectId={projectId} files={data.files} commits={data.commits} runAction={runAction} busyAction={busyAction} />;
            case 'runs': return <RunsPanel projectId={projectId} runs={data.runs} onOpenSession={openSession} runAction={runAction} busyAction={busyAction} />;
            case 'members': return <MembersPanel projectId={projectId} members={data.members} runs={data.runs} selectedId={selectedMemberId} onSelect={setSelectedMemberId} onOpenSession={openSession} runAction={runAction} busyAction={busyAction} />;
            case 'capabilities': return <CapabilitiesPanel projectId={projectId} members={data.members} capabilities={data.capabilities} policies={data.policies} runAction={runAction} busyAction={busyAction} />;
            case 'matrix': return <CapabilityMatrix members={data.members} capabilities={data.capabilities} policies={data.policies} />;
            case 'policies': return <PoliciesPanel projectId={projectId} project={data.project} policies={data.policies} onReload={load} runAction={runAction} busyAction={busyAction} />;
            case 'git': return <GitPanel projectId={projectId} project={data.project} repository={data.gitRepository} commits={data.commits} events={data.events} selected={selectedCommit} onSelect={setSelectedCommit} onDialog={setGitDialog} onOpenSession={openSession} onReload={load} />;
            case 'audit': return <AuditPanel events={data.events} members={data.members} onRefresh={load} onOpenSession={openSession} />;
        }
    };

    return (
        <main className="project-workspace">
            <header className="project-workspace__header">
                <div className="project-workspace__project-mark">{data.project.name.slice(0, 1).toUpperCase()}</div>
                <div className="project-workspace__project-copy">
                    <div><h1>{data.project.name}</h1><StatusPill status={data.project.status} /><span className="project-workspace__visibility"><IconLock size={12} />{data.project.visibility === 'shared' ? '已分享' : '仅自己可见'}</span></div>
                    <p>{data.project.objective || data.project.description || '尚未设置项目目标'}</p>
                </div>
                <Button variant="secondary" onClick={() => setTab('policies')}><IconSettings size={16} />项目设置</Button>
            </header>

            <div className="project-workspace__body">
                <aside className="project-workspace__nav" aria-label="项目工作台导航">
                    {NAV_GROUPS.map((group) => <section key={group.label}><h2>{group.label}</h2>{group.items.map((item) => {
                        const Icon = item.icon;
                        return <Button type="button" variant="ghost" key={item.id} className={tab === item.id ? 'is-active' : ''} aria-current={tab === item.id ? 'page' : undefined} onClick={() => setTab(item.id)}><Icon size={17} /><span>{item.label}</span>{item.id === 'mesh' && data.events.length > 0 && <ProjectCountBadge>{data.events.length}</ProjectCountBadge>}</Button>;
                    })}</section>)}
                    <div className="project-workspace__snapshot-note"><IconBox size={16} /><span><strong>项目隔离已开启</strong><small>成员与能力改动只在本项目生效</small></span></div>
                </aside>
                <div className={`project-workspace__content${tab === 'group' ? ' project-workspace__content--chat' : ''}`} key={tab}>{data.project.status === 'planning' && <section className="project-workspace__planning-banner" role="status"><span><IconSparkles size={18} /></span><div><strong>方案仍处于规划阶段</strong><p>继续与 Leader 讨论目标与方案；确认无误后，再由你启动 Leader 自驱执行。</p></div><Button variant="primary" disabled={busyAction === 'kickoff'} onClick={() => void runAction('kickoff', () => projectsApi.confirmKickoff(projectId), '方案已确认，Leader 开始推进项目')}>{busyAction === 'kickoff' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconPlayerPlay size={16} />}确认方案并启动 Leader</Button></section>}{resourceWarnings.length > 0 && <div className="project-workspace__resource-warning" role="status"><IconAlertTriangle size={17} /><div><strong>部分项目资源暂不可用</strong><p>{resourceWarnings.join('；')}</p></div><Button variant="ghost" onClick={() => void load()}><IconRefresh size={15} />重试</Button></div>}{renderContent()}</div>
            </div>

            {gitDialog && selectedCommit && <GitActionDialog projectId={projectId} mode={gitDialog} commit={selectedCommit} busy={busyAction} onClose={() => setGitDialog(null)} runAction={runAction} />}
            <SessionViewerDrawer agentId={sessionTarget?.agentId || ''} agentName={sessionTarget?.agentName || 'Agent'} target={sessionTarget} interactive groupConfig={sessionTarget?.kind === 'group' ? groupConfig : undefined} onClose={() => setSessionTarget(null)} />
        </main>
    );
}

function Cockpit({ data, onNavigate, runAction, busyAction }: { data: WorkspaceData; onNavigate: (tab: WorkspaceTab) => void; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const liveRun = data.runs.find((run) => ['running', 'queued', 'waiting'].includes(text(run, 'status')));
    const displayedRun = liveRun || data.runs[0];
    const runningItems = data.workItems.filter((item) => ['doing', 'running', 'in_progress'].includes(text(item, 'status', 'state')));
    const focusItems = data.workItems.filter((item) => ['doing', 'running', 'in_progress', 'review', 'waiting_approval'].includes(text(item, 'status', 'state')));
    const blockedItems = data.workItems.filter((item) => ['blocked', 'failed'].includes(text(item, 'status', 'state')));
    const displayedRunStatus = text(displayedRun || {}, 'status');
    const progress = displayedRunStatus === 'succeeded' ? 100 : displayedRun ? num(displayedRun, 'progress', 'progress_percent') : data.project.progress;
    const pauseOrResume = text(liveRun || {}, 'status') === 'waiting' ? 'running' : 'waiting';
    const displayedRunId = text(displayedRun || {}, 'id', 'run_id');
    const liveRunId = text(liveRun || {}, 'id', 'run_id');
    const memberNameByAgentId = useMemo(
        () => new Map(data.members.map((member) => [
            text(member, 'agent_id'),
            text(member, 'name_snapshot', 'agent_name', 'name') || '未命名 Agent',
        ])),
        [data.members],
    );
    const assigneeName = (item: RecordValue) => text(item, 'assignee_name', 'owner_name', 'agent_name')
        || memberNameByAgentId.get(text(item, 'assignee_agent_id'))
        || '未委派';
    return <>
        <SectionHeading eyebrow="PROJECT COCKPIT" title="项目驾驶舱" description="Leader 聚焦目标、阻塞与收敛，成员通过 A2A 直接推进工作。" actions={<Button variant="primary" onClick={() => onNavigate('mesh')}><IconMessageCircle size={16} />进入协作中心</Button>} />
        {displayedRun ? <section className="project-workspace__run-hero">
            <div className="project-workspace__pulse"><IconBolt size={22} /></div>
            <div className="project-workspace__run-copy"><div><StatusPill status={displayedRunStatus} /><code>{displayedRunId || 'RUN'}</code></div><h3>{text(obj(displayedRun.input), 'objective', 'task') || text(displayedRun, 'name', 'objective', 'title') || data.project.objective}</h3><p>开始于 {dateLabel(displayedRun.started_at || displayedRun.created_at)} · 分支 <code>{text(displayedRun, 'branch', 'git_branch') || '—'}</code></p><ProjectProgressBar value={progress} label="当前 Run 进度" className="project-workspace__progress" /></div>
            <div className="project-workspace__run-actions">{liveRunId ? <Button variant="secondary" disabled={busyAction === 'run-state'} onClick={() => void runAction('run-state', () => projectsApi.patchRun(data.project.id, liveRunId, { status: pauseOrResume }), pauseOrResume === 'waiting' ? '运行已转为等待' : '运行已恢复')}>{busyAction === 'run-state' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : pauseOrResume === 'waiting' ? <IconPlayerPause size={16} /> : <IconPlayerPlay size={16} />}{pauseOrResume === 'waiting' ? '暂停 Run' : '恢复 Run'}</Button> : <Button variant="secondary" onClick={() => onNavigate('runs')}>查看运行历史 <IconArrowRight size={14} /></Button>}</div>
        </section> : <EmptyState icon={<IconBolt size={22} />} title="还没有运行" description="创建 Run 后，这里会展示目标进度、预算与执行健康度。" action={<Button variant="primary" onClick={() => onNavigate('runs')}>创建第一个 Run</Button>} />}
        <div className="project-workspace__metric-strip"><article><span>工作项</span><strong>{data.workItems.length}</strong><small>{runningItems.length} 项正在执行</small></article><article><span>阻塞</span><strong className={blockedItems.length ? 'is-danger' : ''}>{blockedItems.length}</strong><small>{blockedItems.length ? '需要介入' : '当前无阻塞'}</small></article><article><span>协作事件</span><strong>{data.events.length}</strong><small>A2A 与系统事件</small></article><article><span>项目能力</span><strong>{data.capabilities.length}</strong><small>Skill 与 MCP</small></article></div>
        <div className="project-workspace__two-column"><section className="project-workspace__card"><header><div><span>执行态势</span><h3>当前工作</h3></div><Button variant="ghost" onClick={() => onNavigate('work')}>查看全部 <IconArrowRight size={14} /></Button></header>{focusItems.length ? <div className="project-workspace__compact-list">{focusItems.slice(0, 5).map((item) => <article key={text(item, 'id', 'work_item_id')}><StatusPill status={text(item, 'status', 'state')} /><div><strong>{text(item, 'title', 'name')}</strong><small>{assigneeName(item)} · {dateLabel(item.updated_at)}</small></div><IconChevronRight size={15} /></article>)}</div> : <EmptyState icon={<IconArchive size={22} />} title="没有执行中或待评审的工作项" description="待工作项被 Agent 接受后，这里会实时更新。" />}</section><section className="project-workspace__card"><header><div><span>需要关注</span><h3>风险与阻塞</h3></div><ProjectCountBadge className="project-workspace__count">{blockedItems.length}</ProjectCountBadge></header>{blockedItems.length ? <div className="project-workspace__risk-list">{blockedItems.map((item) => <article key={text(item, 'id', 'work_item_id')}><IconAlertTriangle size={18} /><div><strong>{text(item, 'title', 'name')}</strong><p>{text(item, 'blocked_reason', 'error', 'description') || '工作项已停止推进，请查看运行记录。'}</p></div></article>)}</div> : <EmptyState icon={<IconCircleCheck size={22} />} title="没有已知阻塞" description="系统发现风险或 Agent 主动上报后会出现在这里。" />}</section></div>
    </>;
}

function WorkBoard({ projectId, items, members, onSelect, runAction, busyAction }: { projectId: string; items: RecordValue[]; members: RecordValue[]; onSelect: (id: string) => void; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const [showCreate, setShowCreate] = useState(false);
    const [view, setView] = useState<'graph' | 'board'>('graph');
    const [title, setTitle] = useState('');
    const [description, setDescription] = useState('');
    const [assignee, setAssignee] = useState('');
    const [priority, setPriority] = useState('medium');
    const [acceptance, setAcceptance] = useState('');
    const columns = [{ key: 'todo', label: '待处理' }, { key: 'doing', label: '进行中' }, { key: 'review', label: '待评审' }, { key: 'done', label: '已完成' }] as const;
    const groupFor = (item: RecordValue) => {
        const status = text(item, 'status', 'state');
        if (['completed', 'success', 'done'].includes(status)) return 'done';
        if (['running', 'doing', 'in_progress'].includes(status)) return 'doing';
        if (['review', 'waiting_approval'].includes(status)) return 'review';
        return 'todo';
    };
    const submit = (event: FormEvent) => {
        event.preventDefault();
        void runAction('create-work', () => projectsApi.createWorkItem(projectId, { title: title.trim(), description: description.trim(), assignee_agent_id: assignee || null, priority, status: 'todo', acceptance_criteria: acceptance.split('\n').map((line) => line.trim()).filter(Boolean) }), '工作项已创建并持久化').then((ok) => { if (ok) { setShowCreate(false); setTitle(''); setDescription(''); setAcceptance(''); } });
    };
    const memberOptions = members
        .filter((member) => member.is_enabled !== false)
        .map((member) => ({ value: text(member, 'agent_id'), label: text(member, 'name_snapshot', 'agent_name') }));
    const priorityOptions = [
        { value: 'low', label: '低' },
        { value: 'medium', label: '中' },
        { value: 'high', label: '高' },
        { value: 'urgent', label: '紧急' },
    ];
    const priorityLabels = new Map(priorityOptions.map((option) => [option.value, option.label]));
    const memberNameByAgentId = new Map(members.map((member) => [text(member, 'agent_id'), text(member, 'name_snapshot', 'agent_name', 'name')]));
    const itemAssignee = (item: RecordValue) => text(item, 'assignee_name', 'owner_name', 'agent_name')
        || memberNameByAgentId.get(text(item, 'assignee_agent_id'))
        || '未委派';
    const graphItems = items.map((item) => ({ ...item, assignee_name: itemAssignee(item) }));
    return <><SectionHeading eyebrow="OBJECTIVES / WORK GRAPH" title="目标与任务" description="从项目结果拆解到可验收工作项，依赖、委派与证据保持关联。" actions={<><ProjectSegmentedControl value={view} options={[{ value: 'graph', label: '依赖图' }, { value: 'board', label: '看板' }]} onChange={setView} ariaLabel="工作项视图" /><Button variant="primary" onClick={() => setShowCreate((value) => !value)}><IconPlus size={16} />创建工作项</Button></>} />
        {showCreate && <form className="project-workspace__action-panel" onSubmit={submit}><header><div><span>NEW WORK ITEM</span><h3>创建并指派工作项</h3></div><ProjectIconButton aria-label="关闭" onClick={() => setShowCreate(false)}><IconX size={17} /></ProjectIconButton></header><div className="project-workspace__form-grid"><ProjectField className="is-wide" label="标题" labelFor="project-work-title" required><TextInput id="project-work-title" value={title} onChange={(event) => setTitle(event.target.value)} required autoFocus /></ProjectField><ProjectField className="is-wide" label="说明" labelFor="project-work-description"><ProjectTextarea id="project-work-description" value={description} onChange={(event) => setDescription(event.target.value)} rows={3} /></ProjectField><ProjectField label="指派 Agent"><ProjectSelect value={assignee} options={memberOptions} onChange={setAssignee} ariaLabel="指派 Agent" placeholder="暂不指派" /></ProjectField><ProjectField label="优先级"><ProjectSelect value={priority} options={priorityOptions} onChange={setPriority} ariaLabel="工作项优先级" /></ProjectField><ProjectField className="is-wide" label="验收条件（每行一项）" labelFor="project-work-acceptance"><ProjectTextarea id="project-work-acceptance" value={acceptance} onChange={(event) => setAcceptance(event.target.value)} rows={3} /></ProjectField></div><footer><Button type="button" variant="secondary" onClick={() => setShowCreate(false)}>取消</Button><Button type="submit" variant="primary" disabled={!title.trim() || busyAction === 'create-work'}>{busyAction === 'create-work' && <IconLoader2 className="project-workspace__spinner" size={16} />}创建工作项</Button></footer></form>}
        {items.length ? view === 'graph' ? <div className="project-workspace__graph-panel"><WorkDependencyGraph items={graphItems} selectedWorkItemId={undefined} onWorkItemSelect={(id) => onSelect(id)} /><ProjectGraphLegend /></div> : <div className="project-workspace__kanban">{columns.map((column) => {
            const list = items.filter((item) => groupFor(item) === column.key);
            return <section key={column.key} data-column={column.key}><header><span><i />{column.label}</span><ProjectCountBadge>{list.length}</ProjectCountBadge></header><div>{list.map((item) => {
                const id = text(item, 'id', 'work_item_id');
                const itemPriority = text(item, 'priority') || 'medium';
                return <Button variant="ghost" className="project-workspace__kanban-card" key={id} onClick={() => onSelect(id)}>
                    <div className="project-workspace__kanban-card-meta"><code title={id}>{compactId(id)}</code><em data-priority={itemPriority}>{priorityLabels.get(itemPriority) || itemPriority}</em></div>
                    <h3>{text(item, 'title', 'name') || '未命名工作项'}</h3>
                    <p>{listText(item, 'acceptance_criteria') || text(item, 'description') || '尚未设置验收条件'}</p>
                    <footer><span title={itemAssignee(item)}>{itemAssignee(item)}</span><time>{dateLabel(item.updated_at)}</time></footer>
                </Button>;
            })}{!list.length && <div className="project-workspace__column-empty">暂无工作项</div>}</div></section>;
        })}</div> : <EmptyState icon={<IconChecklist size={22} />} title="目标还没有拆成工作项" description="创建后会进入项目 API，并出现在 Agent 的执行队列。" action={<Button variant="primary" onClick={() => setShowCreate(true)}>创建第一个工作项</Button>} />}</>;
}

function GroupChatPanel({ project, groupSession, groupConfig }: { projectId: string; project: ProjectSummary; members: RecordValue[]; groupSession: RecordValue | null; groupConfig: SessionViewerGroupConfig }) {
    const sessionId = text(groupSession || {}, 'id', 'group_session_id', 'session_id');
    const agentId = text(groupSession || {}, 'access_agent_id', 'session_agent_id', 'agent_id');
    const target: SessionViewerTarget | null = sessionId && agentId ? {
        sessionId,
        agentId,
        title: text(groupSession || {}, 'title', 'group_name') || `${project.name} · 项目群聊`,
        mode: 'group',
    } : null;
    return target ? (
            <SessionViewerDrawer
                embedded
                agentId={agentId}
                agentName="项目群聊"
                target={target}
                interactive
                groupConfig={groupConfig}
                onClose={() => undefined}
            />
        ) : <EmptyState icon={<IconMessageCircle size={22} />} title="项目群聊暂不可用" description="项目群会话初始化完成后，会在这里显示共享时间线与输入区。" />;
}

function MeshPanel({ members, events, runs, onOpenSession }: { members: RecordValue[]; events: RecordValue[]; runs: RecordValue[]; onOpenSession: OpenSession }) {
    const sessionForAgent = (agentId: string) => [...runs, ...events]
        .filter((entry) => {
            if (sessionRouteOf(entry)?.kind !== 'session') return false;
            const records = traceRecords(entry);
            return [
                traceValue(records, 'agent_id', 'execution_agent_id', 'subagent_agent_id'),
                traceValue(records, 'from_agent_id', 'source_agent_id'),
                traceValue(records, 'to_agent_id', 'target_agent_id'),
            ].includes(agentId);
        })
        .sort((left, right) => new Date(String(right.updated_at || right.created_at || 0)).getTime() - new Date(String(left.updated_at || left.created_at || 0)).getTime())[0];
    return <><SectionHeading eyebrow="A2A DIRECT MESH" title="Agent 协作网络" description="成员可直接唤醒、咨询、委派或请求评审；Leader 不再是消息中转站。" />
        <section className="project-workspace__mesh-graph">{members.length ? <><A2AMeshGraph members={members} events={events} onAgentSelect={(agentId, member) => {
            const source = sessionForAgent(agentId);
            onOpenSession(source ? { ...source, agent_id: agentId } : { ...member, agent_id: agentId }, `${text(member, 'name_snapshot', 'agent_name', 'name') || 'Agent'} · 工作会话`);
        }} /><ProjectGraphLegend /></> : <EmptyState icon={<IconUsers size={22} />} title="项目还没有 Agent 成员" description="添加成员后，A2A 直连拓扑会显示在这里。" />}</section>
        <section className="project-workspace__card project-workspace__timeline"><header><div><span>实时事件</span><h3>A2A 协作流</h3></div></header>{events.length ? events.slice(0, 30).map((event) => <article key={text(event, 'id', 'event_id') || `${text(event, 'created_at')}-${text(event, 'type')}`}><i /><span>{(text(event, 'actor_name', 'from_agent_name') || '系统').slice(0, 1)}</span><div><strong>{text(event, 'actor_name', 'from_agent_name') || '系统'} <em>{text(event, 'type', 'event_type')}</em> {text(event, 'target_name', 'to_agent_name')}</strong><p>{text(event, 'message', 'summary', 'detail') || '事件没有附加说明'}</p><small>{dateLabel(event.created_at)}</small><SessionButton source={event} onOpen={onOpenSession} /></div></article>) : <EmptyState title="还没有协作事件" description="成员直接唤醒或委派后，事件会按因果顺序出现在这里。" />}</section>
    </>;
}

type WorkItemDetailTab = 'context' | 'execution' | 'conversation' | 'changes' | 'review';

function WorkItemDetail({ projectId, items, members, runs, events, files, commits, selectedId, onSelect, onNavigate, onOpenSession, runAction, busyAction }: { projectId: string; items: RecordValue[]; members: RecordValue[]; runs: RecordValue[]; events: RecordValue[]; files: RecordValue[]; commits: RecordValue[]; selectedId: string; onSelect: (id: string) => void; onNavigate: (tab: WorkspaceTab) => void; onOpenSession: OpenSession; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const item = items.find((entry) => text(entry, 'id', 'work_item_id') === selectedId) || items[0];
    const [detailTab, setDetailTab] = useState<WorkItemDetailTab>('context');
    const [assignee, setAssignee] = useState('');
    const [status, setStatus] = useState('todo');
    const [priority, setPriority] = useState('medium');
    const [selectedEvidencePath, setSelectedEvidencePath] = useState('');
    useEffect(() => {
        setAssignee(text(item || {}, 'assignee_agent_id'));
        setStatus(text(item || {}, 'status') || 'todo');
        setPriority(text(item || {}, 'priority') || 'medium');
        setSelectedEvidencePath('');
    }, [item]);
    const itemId = text(item || {}, 'id', 'work_item_id');
    const save = () => void runAction('save-work', () => projectsApi.patchWorkItem(projectId, itemId, { assignee_agent_id: assignee || null, status, priority }), '工作项状态与指派已保存');
    const startRun = () => void runAction('run-work', () => projectsApi.createRun(projectId, { work_item_id: itemId, agent_id: assignee || undefined, input: { objective: text(item || {}, 'title'), task: text(item || {}, 'description') || text(item || {}, 'title') } }), '工作项运行已创建');
    const workItemOptions = items.map((entry) => {
        const id = text(entry, 'id', 'work_item_id');
        return { value: id, label: `${compactId(id)} · ${text(entry, 'title', 'name')}` };
    });
    const currentAssigneeId = text(item || {}, 'assignee_agent_id');
    const memberOptions = members
        .filter((member) => member.is_enabled !== false || text(member, 'agent_id') === currentAssigneeId)
        .map((member) => ({
            value: text(member, 'agent_id'),
            label: `${text(member, 'name_snapshot', 'agent_name')}${member.is_enabled === false ? ' · 已退出（历史指派）' : ''}`,
            disabled: member.is_enabled === false,
        }));
    const statusOptions = [{ value: 'backlog', label: '待规划' }, { value: 'todo', label: '待处理' }, { value: 'in_progress', label: '进行中' }, { value: 'review', label: '待评审' }, { value: 'blocked', label: '阻塞' }, { value: 'done', label: '已完成' }];
    const priorityOptions = [{ value: 'low', label: '低' }, { value: 'medium', label: '中' }, { value: 'high', label: '高' }, { value: 'urgent', label: '紧急' }];
    const memberNameByAgentId = new Map(members.map((member) => [text(member, 'agent_id'), text(member, 'name_snapshot', 'agent_name', 'name') || '未命名 Agent']));
    const matchesWorkItem = (entry: RecordValue) => {
        if (!itemId) return false;
        const records = traceRecords(entry);
        return traceValue(records, 'work_item_id', 'project_work_item_id', 'task_id') === itemId;
    };
    const relatedRuns = runs.filter(matchesWorkItem);
    const relatedEvents = events.filter(matchesWorkItem);
    const relatedEventCommitIds = new Set(relatedEvents.map((event) => traceValue(traceRecords(event), 'commit_hash', 'commit', 'hash')).filter(Boolean));
    const relatedEventPaths = new Set(relatedEvents.map((event) => traceValue(traceRecords(event), 'path', 'file_path')).filter(Boolean));
    const relatedCommits = commits.filter((commit) => {
        if (matchesWorkItem(commit)) return true;
        const commitId = text(commit, 'commit', 'hash', 'commit_hash', 'id');
        return [...relatedEventCommitIds].some((eventCommitId) => commitId === eventCommitId || commitId.startsWith(eventCommitId) || eventCommitId.startsWith(commitId));
    });
    const relatedCommitIds = new Set(relatedCommits.map((commit) => text(commit, 'commit', 'hash', 'commit_hash', 'id')).filter(Boolean));
    const relatedFiles = files.filter((file) => matchesWorkItem(file) || relatedEventPaths.has(text(file, 'path', 'id', 'name')) || relatedCommitIds.has(text(file, 'commit', 'commit_hash', 'hash')));
    const sessionSources = [...relatedRuns, ...relatedEvents].filter((entry, index, source) => {
        const route = sessionRouteOf(entry);
        return Boolean(route) && source.findIndex((candidate) => sessionIdOf(candidate) === route?.sessionId) === index;
    });
    const acceptanceCriteria = Array.isArray(item?.acceptance_criteria)
        ? (item.acceptance_criteria as unknown[]).map(String).filter(Boolean)
        : text(item || {}, 'acceptance', 'acceptance_criteria').split('\n').map((entry) => entry.trim()).filter(Boolean);
    const dependencyIds = Array.isArray(item?.dependency_ids) ? (item.dependency_ids as unknown[]).map(String) : [];
    const latestRun = relatedRuns[0];
    const latestCommit = relatedCommits[0];
    const selectedFile = relatedFiles.find((entry) => text(entry, 'path', 'id') === selectedEvidencePath) || relatedFiles[0];
    const activeAssigneeName = memberNameByAgentId.get(text(item || {}, 'assignee_agent_id')) || '未指派';
    const detailTabs = [
        { value: 'context' as const, label: '上下文与验收' },
        { value: 'execution' as const, label: '执行记录', count: relatedRuns.length + relatedEvents.length },
        { value: 'conversation' as const, label: '关联会话', count: sessionSources.length },
        { value: 'changes' as const, label: '代码与文件变化', count: relatedCommits.length + relatedFiles.length },
        { value: 'review' as const, label: '评审与审批' },
    ];

    let detailContent: ReactNode = null;
    if (item && detailTab === 'context') detailContent = <div className="project-workspace__item-context-grid">
        <section className="project-workspace__item-copy"><span className="project-workspace__item-kicker">任务说明</span><h3>交付内容</h3><p>{text(item, 'description', 'context') || '未提供工作项背景。'}</p><div className="project-workspace__item-trace-note"><strong>输入与依赖</strong>{dependencyIds.length ? <ul>{dependencyIds.map((id) => {
            const dependency = items.find((entry) => text(entry, 'id', 'work_item_id') === id);
            return <li key={id}><code>{compactId(id)}</code><span>{text(dependency || {}, 'title', 'name') || '关联工作项'}</span></li>;
        })}</ul> : <p>无前置依赖，可直接开始。</p>}</div></section>
        <section className="project-workspace__item-criteria"><span className="project-workspace__item-kicker">ACCEPTANCE CRITERIA</span><h3>验收条件</h3>{acceptanceCriteria.length ? <ol>{acceptanceCriteria.map((criterion, index) => <li key={`${criterion}-${index}`}><IconCircleCheck size={16} /><span><strong>{criterion}</strong><small>{text(item, 'status') === 'done' ? '工作项已完成，请核对关联证据' : '等待执行结果或评审证据'}</small></span></li>)}</ol> : <ProjectEmptyState title="尚未定义验收条件" description="请先完善验收边界，再启动执行。" />}</section>
        <aside className="project-workspace__item-control"><span className="project-workspace__item-kicker">执行控制</span><h3>指派与状态推进</h3><ProjectField label="负责人"><ProjectSelect value={assignee} options={memberOptions} onChange={setAssignee} ariaLabel="工作项负责人" placeholder="未指派" /></ProjectField><ProjectField label="状态"><ProjectSelect value={status} options={statusOptions} onChange={setStatus} ariaLabel="工作项状态" /></ProjectField><ProjectField label="优先级"><ProjectSelect value={priority} options={priorityOptions} onChange={setPriority} ariaLabel="工作项优先级" /></ProjectField><footer><Button variant="secondary" onClick={startRun} disabled={busyAction === 'run-work'}>{busyAction === 'run-work' && <IconLoader2 className="project-workspace__spinner" size={16} />}{assignee ? '创建 Run' : '由 Leader 执行'}</Button><Button variant="primary" onClick={save} disabled={busyAction === 'save-work'}>{busyAction === 'save-work' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconDeviceFloppy size={16} />}保存</Button></footer></aside>
    </div>;
    if (item && detailTab === 'execution') detailContent = relatedRuns.length || relatedEvents.length ? <div className="project-workspace__item-execution">
        {relatedRuns.map((run) => <article key={text(run, 'id', 'run_id')}><span className="project-workspace__item-event-icon"><IconBolt size={17} /></span><div><header><strong>{text(obj(run.input), 'objective') || '执行工作项'}</strong><StatusPill status={text(run, 'status')} /></header><p>{text(obj(run.output), 'summary', 'result', 'message') || text(run, 'error') || '已冻结本次执行的成员、能力与输入快照。'}</p><footer><code>{compactId(text(run, 'id', 'run_id'))}</code><time>{dateLabel(run.started_at || run.created_at)}</time><SessionButton source={run} onOpen={onOpenSession} /></footer></div></article>)}
        {relatedEvents.slice(0, 20).map((event) => <article key={text(event, 'id', 'event_id')}><span className="project-workspace__item-event-icon"><IconActivityHeartbeat size={17} /></span><div><header><strong>{statusLabel(text(event, 'event_type', 'type'))}</strong><code>{text(event, 'event_type', 'type') || '项目事件'}</code></header><p>{text(event, 'detail', 'summary', 'message') || '该事件没有附加说明。'}</p><footer><time>{dateLabel(event.created_at)}</time><SessionButton source={event} onOpen={onOpenSession} /></footer></div></article>)}
    </div> : <ProjectEmptyState icon={<IconHistory size={22} />} title="还没有执行记录" description="当 Agent 接受、运行或更新该工作项时，这里会按时间展示。" />;
    if (item && detailTab === 'conversation') detailContent = sessionSources.length ? <div className="project-workspace__item-sessions">{sessionSources.map((source) => {
        const route = sessionRouteOf(source);
        const records = traceRecords(source);
        const agentId = traceValue(records, 'agent_id', 'execution_agent_id', 'subagent_agent_id', 'actor_agent_id');
        return <article key={route?.sessionId}><span><IconMessageCircle size={18} /></span><div><strong>{route?.kind === 'group' ? '项目群聊' : `${memberNameByAgentId.get(agentId) || 'Agent'} 工作会话`}</strong><p>{traceValue(records, 'objective', 'summary', 'message', 'task') || '会话已作为工作项的执行证据保留。'}</p><code>{route?.sessionId}</code></div><Button variant="secondary" onClick={() => onOpenSession(source, `${text(item, 'title')} · 关联会话`)}>打开会话</Button></article>;
    })}</div> : <ProjectEmptyState icon={<IconMessageCircle size={22} />} title="还没有关联会话" description="该工作项触发 Agent 执行或 A2A 协作后，会话锚点会出现在这里。" />;
    if (item && detailTab === 'changes') detailContent = relatedFiles.length || relatedCommits.length ? <div className="project-workspace__item-changes"><aside><header><strong>{relatedFiles.length} 个关联文件</strong><span>{relatedCommits.length} 个 Commit</span></header>{relatedFiles.map((file) => {
        const path = text(file, 'path', 'id', 'name');
        return <Button variant="ghost" className={text(selectedFile || {}, 'path', 'id', 'name') === path ? 'is-active' : ''} key={path} onClick={() => setSelectedEvidencePath(path)}><IconFile size={15} /><span>{path}</span><code>{compactId(text(file, 'commit_hash', 'commit')) || '已追溯'}</code></Button>;
    })}{relatedCommits.map((commit) => <Button variant="ghost" key={text(commit, 'commit', 'hash', 'commit_hash', 'id')} onClick={() => onNavigate('git')}><IconBrandGit size={15} /><span>{text(commit, 'message', 'title') || '项目提交'}</span><code>{compactId(text(commit, 'commit', 'hash', 'commit_hash', 'id'))}</code></Button>)}</aside><section><header><code>{text(selectedFile || latestCommit || {}, 'path', 'message', 'title') || '关联变更'}</code><Button variant="ghost" onClick={() => onNavigate(selectedFile ? 'files' : 'git')}>{selectedFile ? '在项目文件中打开' : '查看 Git 历史'} <IconArrowRight size={14} /></Button></header>{selectedFile ? <pre>{text(selectedFile, 'preview', 'content_preview', 'content') || '该文件没有可在线预览的文本内容。'}</pre> : <div className="project-workspace__item-change-summary"><IconBrandGit size={24} /><strong>{text(latestCommit || {}, 'message', 'title') || '关联提交'}</strong><code>{text(latestCommit || {}, 'commit', 'hash', 'commit_hash', 'id')}</code></div>}</section></div> : <ProjectEmptyState icon={<IconCodeDots size={22} />} title="还没有关联的代码或文件变化" description="项目提交包含 Work-Item 追溯信息后，变更会自动归集。" action={<Button variant="secondary" onClick={() => onNavigate('files')}>打开项目文件</Button>} />;
    if (item && detailTab === 'review') detailContent = <div className="project-workspace__item-review"><section><div className="project-workspace__item-review-summary"><span><IconShieldCheck size={22} /></span><div><span className="project-workspace__item-kicker">VERIFIABLE RESULT</span><h3>验收覆盖</h3><p>{acceptanceCriteria.length} 条验收条件 · {relatedRuns.length} 次执行 · {relatedCommits.length + relatedFiles.length} 项交付证据</p></div><StatusPill status={text(item, 'status')} /></div><div className="project-workspace__item-review-list">{acceptanceCriteria.length ? acceptanceCriteria.map((criterion, index) => <article key={`${criterion}-${index}`}><IconCircleCheck size={17} /><div><strong>{criterion}</strong><p>{relatedRuns.length || relatedCommits.length ? '已有关联运行或版本证据，请人工核对结果。' : '还没有可核对的运行或版本证据。'}</p></div></article>) : <ProjectEmptyState title="无验收条件" description="请先回到上下文完善可验收结果。" />}</div></section><aside><span className="project-workspace__item-kicker">HUMAN GATE</span><h3>人工审批</h3><p>审批只更新该工作项状态，不改写 Git 历史或覆盖运行证据。</p><dl><div><dt>负责人</dt><dd>{activeAssigneeName}</dd></div><div><dt>最近运行</dt><dd>{latestRun ? compactId(text(latestRun, 'id', 'run_id')) : '—'}</dd></div><div><dt>关联 Commit</dt><dd><code>{latestCommit ? compactId(text(latestCommit, 'commit', 'hash', 'commit_hash', 'id')) : '—'}</code></dd></div><div><dt>最后更新</dt><dd>{dateLabel(item.updated_at)}</dd></div></dl><footer><Button variant="secondary" disabled={busyAction === 'review-return'} onClick={() => void runAction('review-return', () => projectsApi.patchWorkItem(projectId, itemId, { status: 'blocked' }), '工作项已退回修改')}>{busyAction === 'review-return' && <IconLoader2 className="project-workspace__spinner" size={16} />}退回修改</Button><Button variant="primary" disabled={busyAction === 'review-approve'} onClick={() => void runAction('review-approve', () => projectsApi.patchWorkItem(projectId, itemId, { status: 'done' }), '工作项已验收通过')}>{busyAction === 'review-approve' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconCircleCheck size={16} />}验收通过</Button></footer></aside></div>;

    return <><SectionHeading eyebrow="WORK ITEM / EVIDENCE" title="工作项详情" description="把目标上下文、执行过程、对话与交付证据收敛到一个可追溯对象。" actions={items.length ? <ProjectSelect ariaLabel="选择工作项" value={itemId} options={workItemOptions} onChange={onSelect} /> : undefined} />{item ? <section className="project-workspace__item-shell"><header className="project-workspace__item-meta"><StatusPill status={text(item, 'status', 'state')} /><div><small>负责人</small><strong>{activeAssigneeName}</strong></div><div><small>优先级</small><strong>{priorityOptions.find((option) => option.value === text(item, 'priority'))?.label || text(item, 'priority') || '中'}</strong></div><div><small>依赖</small><strong>{dependencyIds.length ? `${dependencyIds.length} 项` : '无'}</strong></div><div><small>最近执行</small><code>{latestRun ? compactId(text(latestRun, 'id', 'run_id')) : '—'}</code></div><div><small>更新时间</small><strong>{dateLabel(item.updated_at)}</strong></div></header><div className="project-workspace__item-title"><code>{compactId(itemId)}</code><h3>{text(item, 'title', 'name')}</h3></div><ProjectSegmentedControl className="project-workspace__item-tabs" value={detailTab} options={detailTabs} onChange={setDetailTab} ariaLabel="工作项详情分区" /><div className="project-workspace__item-content">{detailContent}</div></section> : <EmptyState icon={<IconChecklist size={22} />} title="没有可查看的工作项" description="项目创建工作项后，可在这里核对上下文、对话、Diff 与验收证据。" />}</>;
}

function FilesPanel({ projectId, files, commits, runAction, busyAction }: { projectId: string; files: RecordValue[]; commits: RecordValue[]; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    return <ProjectFileWorkspace projectId={projectId} files={files} commits={commits} runAction={runAction} busyAction={busyAction} />;
}

function RunsPanel({ projectId, runs, onOpenSession, runAction, busyAction }: { projectId: string; runs: RecordValue[]; onOpenSession: OpenSession; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const [objective, setObjective] = useState('');
    const create = (event: FormEvent) => { event.preventDefault(); void runAction('new-run', () => projectsApi.createRun(projectId, { input: { objective: objective.trim() } }), '新运行已创建').then((ok) => { if (ok) setObjective(''); }); };
    return <><SectionHeading eyebrow="RUN CONTROL" title="运行控制" description="每次运行冻结目标、成员与能力快照；重试会创建新 Run，不覆盖历史。" />
        <form className="project-workspace__inline-create" onSubmit={create}><ProjectField label="本次运行输入" labelFor="project-run-objective" required><TextInput id="project-run-objective" value={objective} onChange={(event) => setObjective(event.target.value)} placeholder="说明本次 Run 要推进的目标" required /></ProjectField><Button variant="primary" type="submit" disabled={!objective.trim() || busyAction === 'new-run'}>{busyAction === 'new-run' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconPlayerPlay size={16} />}创建 Run</Button></form>
        {runs.length ? <div className="project-workspace__run-list">{runs.map((run) => { const runId = text(run, 'id', 'run_id'); const runStatus = text(run, 'status'); const input = obj(run.input); const nextStatus = runStatus === 'running' ? 'waiting' : runStatus === 'waiting' || runStatus === 'queued' ? 'running' : ''; return <article key={runId}><div className="project-workspace__run-icon"><IconBolt size={18} /></div><div><div><StatusPill status={runStatus} /><code>{runId}</code></div><h3>{text(input, 'objective') || text(run, 'name', 'objective') || '项目运行'}</h3><p>{dateLabel(run.started_at || run.created_at)} · {text(run, 'trigger_type') || 'manual'}</p></div><div className="project-workspace__run-controls"><SessionButton source={run} onOpen={onOpenSession} />{nextStatus && <Button variant="secondary" disabled={busyAction === `run-${runId}`} onClick={() => void runAction(`run-${runId}`, () => projectsApi.patchRun(projectId, runId, { status: nextStatus }), nextStatus === 'waiting' ? 'Run 已转为等待' : 'Run 已恢复')}>{busyAction === `run-${runId}` ? <IconLoader2 className="project-workspace__spinner" size={15} /> : nextStatus === 'waiting' ? <IconPlayerPause size={15} /> : <IconPlayerPlay size={15} />}{nextStatus === 'waiting' ? '暂停' : '继续'}</Button>}{!['succeeded', 'failed', 'cancelled'].includes(runStatus) && <Button variant="ghost" disabled={busyAction === `finish-${runId}`} onClick={() => void runAction(`finish-${runId}`, () => projectsApi.patchRun(projectId, runId, { status: 'succeeded' }), 'Run 已标记完成')}><IconCircleCheck size={15} />完成</Button>}</div></article>; })}</div> : <EmptyState title="还没有运行记录" description="输入目标并创建首个 Run；创建后会立即持久化并冻结快照。" />}</>;
}

function MembersPanel({ projectId, members, runs, selectedId, onSelect, onOpenSession, runAction, busyAction }: { projectId: string; members: RecordValue[]; runs: RecordValue[]; selectedId: string; onSelect: (id: string) => void; onOpenSession: OpenSession; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const activeMembers = useMemo(() => members.filter((entry) => entry.is_enabled !== false), [members]);
    const departedMembers = members.filter((entry) => entry.is_enabled === false);
    const member = members.find((entry) => text(entry, 'id', 'member_id', 'agent_id') === selectedId) || activeMembers[0] || departedMembers[0];
    const [config, setConfig] = useState('{}');
    const [configError, setConfigError] = useState('');
    const [availableAgents, setAvailableAgents] = useState<RecordValue[]>([]);
    const [agentsLoading, setAgentsLoading] = useState(true);
    const [agentsError, setAgentsError] = useState('');
    const [addDialogOpen, setAddDialogOpen] = useState(false);
    const [removeDialogOpen, setRemoveDialogOpen] = useState(false);
    const [candidateAgentId, setCandidateAgentId] = useState('');

    useEffect(() => {
        setConfig(JSON.stringify(obj(member?.config_snapshot), null, 2));
        setConfigError('');
    }, [member]);

    useEffect(() => {
        let mounted = true;
        setAgentsLoading(true);
        setAgentsError('');
        void projectsApi.bootstrapOptions().then((options) => {
            if (mounted) setAvailableAgents(options.agents.map((agent) => ({ ...agent })));
        }).catch((error) => {
            if (mounted) setAgentsError(errorMessage(error));
        }).finally(() => {
            if (mounted) setAgentsLoading(false);
        });
        return () => { mounted = false; };
    }, []);

    const memberId = text(member || {}, 'id', 'member_id');
    const agentId = text(member || {}, 'agent_id');
    const memberName = text(member || {}, 'name_snapshot', 'agent_name', 'name') || '项目成员';
    const departed = member?.is_enabled === false;
    const memberAgentIds = new Set(members.map((entry) => text(entry, 'agent_id')).filter(Boolean));
    const candidates = availableAgents.filter((agent) => !memberAgentIds.has(text(agent, 'id', 'agent_id')));
    const candidateOptions = candidates.map((agent) => ({
        value: text(agent, 'id', 'agent_id'),
        label: `${text(agent, 'name', 'agent_name') || '未命名 Agent'} · ${text(agent, 'role_description', 'role') || '项目成员'}`,
    }));
    const selectedCandidate = candidates.find((agent) => text(agent, 'id', 'agent_id') === candidateAgentId) || candidates[0];
    const selectedCandidateId = text(selectedCandidate || {}, 'id', 'agent_id');
    const memberRuns = runs.filter((run) => {
        const records = traceRecords(run);
        return [
            traceValue(records, 'agent_id', 'execution_agent_id', 'subagent_agent_id'),
            traceValue(records, 'assignee_agent_id', 'actor_agent_id'),
        ].includes(agentId);
    });

    const saveSnapshot = () => {
        if (departed) return;
        let parsed: RecordValue;
        try {
            parsed = JSON.parse(config) as RecordValue;
            setConfigError('');
        } catch {
            setConfigError('配置必须是有效 JSON');
            return;
        }
        void runAction('save-member', () => projectsApi.patchMember(projectId, memberId, { config_snapshot: parsed }), '项目成员快照已更新；源 Agent 未改变');
    };
    const addMember = () => {
        if (!selectedCandidateId) return;
        void runAction('add-member', () => projectsApi.addMember(projectId, {
            agent_id: selectedCandidateId,
            is_leader: false,
            is_enabled: true,
        }), 'Agent 已加入项目，并生成项目快照').then((succeeded) => {
            if (succeeded) {
                setAddDialogOpen(false);
                setCandidateAgentId('');
            }
        });
    };
    const removeMember = () => {
        if (!memberId || bool(member || {}, 'is_leader')) return;
        void runAction('remove-member', () => projectsApi.removeMember(projectId, memberId, 'human_removed_from_project'), '成员已退出项目；历史记录保持可追溯').then((succeeded) => {
            if (succeeded) setRemoveDialogOpen(false);
        });
    };
    const restoreMember = () => {
        if (!memberId || !departed) return;
        void runAction('restore-member', () => projectsApi.restoreMember(projectId, memberId, 'human_restored_to_project'), '成员已恢复，可继续参与项目');
    };

    const renderMemberGroup = (label: string, entries: RecordValue[], historical = false) => entries.length ? <section className="project-workspace__member-group">
        <header><span>{label}</span><ProjectCountBadge>{entries.length}</ProjectCountBadge></header>
        {entries.map((entry) => {
            const id = text(entry, 'id', 'member_id', 'agent_id');
            const name = text(entry, 'agent_name', 'name_snapshot', 'name') || 'Agent';
            return <Button variant="ghost" key={id} className={`${text(member || {}, 'id', 'member_id', 'agent_id') === id ? 'is-active' : ''}${historical ? ' is-departed' : ''}`} onClick={() => onSelect(id)}>
                <span>{name.slice(0, 1)}</span>
                <div><strong>{name}</strong><small>{bool(entry, 'is_leader') ? 'Leader · ' : ''}{text(entry, 'role_snapshot', 'role') || '项目成员'}</small></div>
                {historical ? <ProjectStatusBadge tone="neutral">已退出</ProjectStatusBadge> : bool(entry, 'is_leader') ? <ProjectStatusBadge tone="info">Leader</ProjectStatusBadge> : <ProjectStatusBadge tone="success">在岗</ProjectStatusBadge>}
                <IconChevronRight size={15} />
            </Button>;
        })}
    </section> : null;

    return <>
        <SectionHeading
            eyebrow="MEMBERS / SNAPSHOTS"
            title="成员与三层快照"
            description="成员可以随项目阶段增减；退出只冻结后续权限，快照、Run 与会话历史永久保留。"
            actions={<Button variant="primary" onClick={() => setAddDialogOpen(true)}><IconPlus size={16} />添加 Agent</Button>}
        />
        {members.length ? <>
            <div className="project-workspace__member-summary" aria-label="项目成员概览">
                <article><span>在岗成员</span><strong>{activeMembers.length}</strong><small>可被 @、指派与调用项目能力</small></article>
                <article><span>历史成员</span><strong>{departedMembers.length}</strong><small>保留快照与执行证据，只读可见</small></article>
                <article><span>冻结 Run</span><strong>{runs.length}</strong><small>每个 Run 保留当时的成员执行视图</small></article>
            </div>
            <div className="project-workspace__member-layout">
                <div className="project-workspace__member-list">
                    {renderMemberGroup('在岗', activeMembers)}
                    {renderMemberGroup('已退出 · 历史记录', departedMembers, true)}
                </div>
                <div className="project-workspace__snapshot-graph">
                    <div className="project-workspace__snapshot-heading">
                        <div><strong>{memberName}</strong><small>{departed ? '历史快照 · 不再接收新消息或任务' : '当前项目快照 · 仅在本项目生效'}</small></div>
                        <ProjectStatusBadge tone={departed ? 'neutral' : 'success'}>{departed ? '已退出' : '在岗'}</ProjectStatusBadge>
                    </div>
                    <SnapshotLineageGraph member={member || null} runs={memberRuns} />
                    <ProjectGraphLegend />
                </div>
            </div>
            <section className={`project-workspace__action-panel project-workspace__member-editor${departed ? ' is-readonly' : ''}`}>
                <header>
                    <div><span>{departed ? 'HISTORICAL PROJECT SNAPSHOT' : 'PROJECT SNAPSHOT EDITOR'}</span><h3>{departed ? `查看 ${memberName} 的历史快照` : `编辑 ${memberName} 的项目快照`}</h3></div>
                    <div className="project-workspace__member-actions">
                        {departed ? <Button variant="secondary" disabled={busyAction === 'restore-member'} onClick={restoreMember}>{busyAction === 'restore-member' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconRestore size={15} />}恢复成员</Button> : !bool(member || {}, 'is_leader') ? <>
                            <Button variant="secondary" disabled={busyAction === 'leader'} onClick={() => void runAction('leader', () => projectsApi.setLeader(projectId, agentId), '项目 Leader 已切换')}><IconFlag size={15} />设为 Leader</Button>
                            <Button variant="danger" onClick={() => setRemoveDialogOpen(true)}><IconTrash size={15} />移出项目</Button>
                        </> : <ProjectStatusBadge tone="info">当前 Leader</ProjectStatusBadge>}
                    </div>
                </header>
                {departed && <div className="project-workspace__member-history-note"><IconArchive size={17} /><div><strong>历史只读</strong><p>该 Agent 不再接收 @、新指派或项目工具调用。恢复前只能查看其快照、Run 与关联会话。</p></div></div>}
                <ProjectField label="项目内配置 JSON" labelFor="project-member-config" error={configError || undefined} hint={departed ? '退出时的项目快照保持原样。' : '修改只写入项目快照，不影响源 Agent。'}>
                    <ProjectTextarea id="project-member-config" value={config} onChange={(event) => setConfig(event.target.value)} rows={7} spellCheck={false} disabled={departed} />
                </ProjectField>
                <section className="project-workspace__member-run-history">
                    <header><div><span>RUN / SESSION HISTORY</span><h4>关联执行记录</h4></div><ProjectCountBadge>{memberRuns.length}</ProjectCountBadge></header>
                    {memberRuns.length ? memberRuns.slice(0, 6).map((run) => {
                        const runId = text(run, 'id', 'run_id');
                        const hasSession = Boolean(sessionIdOf(run));
                        return <article key={runId}>
                            <StatusPill status={text(run, 'status')} />
                            <div><strong>{text(obj(run.input), 'objective', 'task') || '项目执行'}</strong><small><code>{compactId(runId)}</code> · {dateLabel(run.started_at || run.created_at)}</small></div>
                            {hasSession && <Button variant="ghost" onClick={() => onOpenSession(departed ? { ...run, status: 'disabled', member_enabled: false } : run, `${memberName} · ${departed ? '历史只读会话' : '工作会话'}`)}><IconMessageCircle size={14} />{departed ? '历史只读' : '查看会话'}</Button>}
                        </article>;
                    }) : <p>该成员尚未产生项目 Run；退出后仍会保留此处的历史记录。</p>}
                </section>
                {!departed && <footer><span className="project-workspace__switch-copy">源 Agent 保持稳定，修改只在当前项目生效</span><Button variant="primary" onClick={saveSnapshot} disabled={busyAction === 'save-member'}>{busyAction === 'save-member' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconDeviceFloppy size={16} />}保存快照</Button></footer>}
            </section>
        </> : <EmptyState icon={<IconUsers size={22} />} title="项目还没有成员" description="先添加一个 Agent，再从在岗成员中指定 Leader。" action={<Button variant="primary" onClick={() => setAddDialogOpen(true)}><IconPlus size={16} />添加 Agent</Button>} />}

        <ProjectDialog open={addDialogOpen} onClose={() => { if (busyAction !== 'add-member') setAddDialogOpen(false); }} ariaLabel="添加项目 Agent" className="project-workspace__member-dialog">
            <div className="project-workspace__modal">
                <header><div><span>ADD PROJECT MEMBER</span><h2>添加项目 Agent</h2></div><ProjectIconButton aria-label="关闭" disabled={busyAction === 'add-member'} onClick={() => setAddDialogOpen(false)}><IconX size={18} /></ProjectIconButton></header>
                <p>从平台现有 Agent 中选择。加入后会生成独立的项目快照，源 Agent 配置保持不变。</p>
                {agentsError ? <div className="project-workspace__repository-error" role="alert"><IconAlertTriangle size={16} /><span>{agentsError}</span></div> : candidateOptions.length ? <>
                    <ProjectField label="Agent"><ProjectSelect value={selectedCandidateId} options={candidateOptions} onChange={setCandidateAgentId} ariaLabel="选择要加入项目的 Agent" disabled={agentsLoading || busyAction === 'add-member'} /></ProjectField>
                    {selectedCandidate && <div className="project-workspace__member-candidate"><span>{(text(selectedCandidate, 'name') || 'A').slice(0, 1)}</span><div><strong>{text(selectedCandidate, 'name') || '未命名 Agent'}</strong><p>{text(selectedCandidate, 'role_description') || '项目成员'}</p><small>{text(selectedCandidate, 'status') || 'idle'} · {num(selectedCandidate, 'skill_count')} Skill · {num(selectedCandidate, 'mcp_count')} MCP</small></div></div>}
                </> : <ProjectEmptyState icon={agentsLoading ? <IconLoader2 className="project-workspace__spinner" size={20} /> : <IconUsers size={20} />} title={agentsLoading ? '正在读取 Agent' : '没有可添加的 Agent'} description={agentsLoading ? '请稍候…' : '平台中的 Agent 都已加入过该项目；已退出成员请从历史记录中恢复。'} />}
                <footer><Button variant="secondary" onClick={() => setAddDialogOpen(false)} disabled={busyAction === 'add-member'}>取消</Button><Button variant="primary" onClick={addMember} disabled={!selectedCandidateId || agentsLoading || busyAction === 'add-member'}>{busyAction === 'add-member' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconPlus size={16} />}添加到项目</Button></footer>
            </div>
        </ProjectDialog>

        <ProjectDialog open={removeDialogOpen} onClose={() => { if (busyAction !== 'remove-member') setRemoveDialogOpen(false); }} ariaLabel="确认移出项目成员" className="project-workspace__member-dialog">
            <div className="project-workspace__modal">
                <header><div><span>MEMBER LIFECYCLE</span><h2>将 {memberName} 移出项目？</h2></div><ProjectIconButton aria-label="关闭" disabled={busyAction === 'remove-member'} onClick={() => setRemoveDialogOpen(false)}><IconX size={18} /></ProjectIconButton></header>
                <p>移出后该 Agent 将立即停止接收 @、新任务与项目工具调用。历史快照、Run、会话和审计记录仍然可见，也可以稍后恢复。</p>
                <div className="project-workspace__safe-note"><IconLock size={16} /><span>这是可恢复的成员状态变更，不会删除历史数据。</span></div>
                <footer><Button variant="secondary" onClick={() => setRemoveDialogOpen(false)} disabled={busyAction === 'remove-member'}>保留成员</Button><Button variant="danger" onClick={removeMember} disabled={busyAction === 'remove-member'}>{busyAction === 'remove-member' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconTrash size={16} />}移出项目</Button></footer>
            </div>
        </ProjectDialog>
    </>;
}

function ProjectToolsControl({ projectId, members, policies, runAction, busyAction }: { projectId: string; members: RecordValue[]; policies: RecordValue | null; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const activeMembers = useMemo(() => members.filter((entry) => entry.is_enabled !== false), [members]);
    const [selectedMemberId, setSelectedMemberId] = useState(() => text(activeMembers[0] || {}, 'id', 'member_id'));
    useEffect(() => {
        if (!activeMembers.some(member => text(member, 'id', 'member_id') === selectedMemberId)) {
            setSelectedMemberId(text(activeMembers[0] || {}, 'id', 'member_id'));
        }
    }, [activeMembers, selectedMemberId]);
    const member = activeMembers.find(entry => text(entry, 'id', 'member_id') === selectedMemberId) || activeMembers[0];
    const memberId = text(member || {}, 'id', 'member_id');
    const memberName = text(member || {}, 'name_snapshot', 'agent_name', 'name') || '项目成员';
    const effectiveCount = member ? PROJECT_TOOL_REGISTRY.filter(tool => projectToolResolution(tool, member, policies).effective).length : 0;
    const toggleTool = (tool: ProjectToolDefinition, checked: boolean) => {
        if (!member || !memberId) return;
        const resolution = projectToolResolution(tool, member, policies);
        if (!resolution.roleCeiling || resolution.policyBlocked || resolution.snapshotBlocked) return;
        const config = obj(member.config_snapshot);
        const existingDisabled = stringList(config.disabled_project_tools);
        const disabled = new Set(existingDisabled);
        if (checked) disabled.delete(tool.name); else disabled.add(tool.name);
        const knownNames = new Set(PROJECT_TOOL_REGISTRY.map(entry => entry.name));
        const disabledProjectTools = [
            ...existingDisabled.filter(name => !knownNames.has(name)),
            ...PROJECT_TOOL_REGISTRY.map(entry => entry.name).filter(name => disabled.has(name)),
        ];
        void runAction(
            `project-tool-${memberId}-${tool.name}`,
            () => projectsApi.patchMember(projectId, memberId, { config_snapshot: { ...config, disabled_project_tools: disabledProjectTools } }),
            `${memberName} 的项目管理工具已更新`,
        );
    };
    return <section className="project-workspace__project-tools">
        <header className="project-workspace__subsection-heading"><div><span>PROJECT MANAGEMENT TOOLS</span><h3>项目管理工具</h3><p>运行时工具来自同一原子 registry，并按角色基线、项目策略和成员快照求交集；已退出成员的工具权限保持冻结。</p></div>{member && <div className="project-workspace__project-tool-member"><ProjectSelect value={memberId} options={activeMembers.map(entry => ({ value: text(entry, 'id', 'member_id'), label: `${text(entry, 'name_snapshot', 'agent_name', 'name')}${bool(entry, 'is_leader') ? ' · Leader' : ''}` }))} onChange={setSelectedMemberId} ariaLabel="选择项目成员" /><ProjectCountBadge>{effectiveCount} / {PROJECT_TOOL_REGISTRY.length}</ProjectCountBadge></div>}</header>
        <div className="project-workspace__tool-baselines"><article><strong>Leader 基线</strong><span>{PROJECT_TOOL_REGISTRY.length} 个 Agent-safe 工具</span><small>包含计划、成员、能力和安全 Git 操作。</small></article><article><strong>参与者基线</strong><span>{PROJECT_TOOL_REGISTRY.filter(tool => tool.participant).length} 个执行工具</span><small>读上下文、工作项、文件，更新本人工作，写文件和定向消息。</small></article></div>
        {member ? <div className="project-workspace__project-tool-grid">{PROJECT_TOOL_REGISTRY.map(tool => {
            const resolution = projectToolResolution(tool, member, policies);
            const blockedLabel = !resolution.roleCeiling ? 'Leader 专用' : resolution.policyBlocked ? '项目策略禁用' : resolution.snapshotBlocked ? '快照白名单限制' : resolution.memberDisabled ? '成员已关闭' : '当前有效';
            const tone = resolution.effective ? 'success' : resolution.memberDisabled ? 'neutral' : 'warning';
            const actionKey = `project-tool-${memberId}-${tool.name}`;
            return <article key={tool.name} className={resolution.effective ? 'is-effective' : ''}><header><span><IconTool size={16} /></span><div><strong>{tool.label}</strong><code>{tool.name}</code></div><ToggleSwitch checked={resolution.effective} onChange={checked => toggleTool(tool, checked)} ariaLabel={`${resolution.effective ? '关闭' : '启用'} ${tool.label}`} disabled={!resolution.roleCeiling || resolution.policyBlocked || resolution.snapshotBlocked || busyAction === actionKey} /></header><p>{tool.description}</p><footer><ProjectStatusBadge tone={tone}>{blockedLabel}</ProjectStatusBadge><small>{tool.participant ? '参与者基线' : 'Leader 基线'}</small></footer></article>;
        })}</div> : <ProjectEmptyState icon={<IconUsers size={22} />} title="没有在岗成员" description="恢复历史成员或添加新成员后，才能配置项目管理工具。" />}
        <aside className="project-workspace__human-only"><IconLock size={18} /><div><strong>Human-only 操作不进入 Agent 工具集</strong><p>{HUMAN_ONLY_PROJECT_OPERATIONS.join(' · ')}</p></div></aside>
    </section>;
}

function CapabilitiesPanel({ projectId, members, capabilities, policies, runAction, busyAction }: { projectId: string; members: RecordValue[]; capabilities: RecordValue[]; policies: RecordValue | null; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const [filter, setFilter] = useState('all');
    const departedAgentIds = new Set(members.filter((member) => member.is_enabled === false).map((member) => text(member, 'agent_id')).filter(Boolean));
    const visible = capabilities.filter((cap) => {
        if (filter === 'all') return true;
        if (['skill', 'mcp'].includes(filter)) return text(cap, 'capability_type', 'kind', 'type') === filter;
        const inherited = Boolean(text(cap, 'inherited_from_agent_id')) || ['agent', 'inherited'].includes(text(cap, 'source'));
        return filter === 'agent' ? inherited : !inherited;
    });
    return <><SectionHeading eyebrow="CAPABILITY CONTROL" title="项目能力中心" description="项目共享 Skill/MCP 对成员可见；Agent 带入能力可逐项关闭，均不影响源 Agent。" />
        <ProjectSegmentedControl className="project-workspace__filters" value={filter} options={[{ value: 'all', label: '全部' }, { value: 'skill', label: 'Skill' }, { value: 'mcp', label: 'MCP' }, { value: 'project', label: '项目共享' }, { value: 'agent', label: 'Agent 带入' }]} onChange={setFilter} ariaLabel="能力筛选" />
        {visible.length ? <div className="project-workspace__cap-grid">{visible.map((cap) => { const id = text(cap, 'id', 'binding_id', 'capability_id'); const capabilityType = text(cap, 'capability_type', 'kind', 'type'); const enabled = cap.is_enabled !== false; const scopeCount = Object.keys(obj(cap.scope)).length; const inheritedFromAgentId = text(cap, 'inherited_from_agent_id'); const departedOwner = Boolean(inheritedFromAgentId && departedAgentIds.has(inheritedFromAgentId)); return <article key={id} className={departedOwner ? 'is-readonly' : ''}><header><span className={`is-${capabilityType || 'skill'}`}>{capabilityType === 'mcp' ? <IconCodeDots size={17} /> : <IconTool size={17} />}</span><div><strong>{text(cap, 'name', 'capability_name')}</strong><small>{text(cap, 'version') || capabilityType.toUpperCase() || '能力'} · {text(cap, 'source') === 'agent' ? `由 ${inheritedFromAgentId || 'Agent'} 带入${departedOwner ? ' · 成员已退出' : ''}` : '项目共享'}</small></div><ToggleSwitch checked={departedOwner ? false : enabled} ariaLabel={`${enabled ? '关闭' : '启用'} ${text(cap, 'name', 'capability_name')}`} disabled={departedOwner || busyAction === `cap-${id}`} onChange={(checked) => void runAction(`cap-${id}`, () => projectsApi.patchCapability(projectId, id, { is_enabled: checked }), checked ? '能力已在项目内启用' : '能力已在项目内关闭；源 Agent 不受影响')} /></header><p>{departedOwner ? '成员已退出，带入能力保持冻结；恢复成员后可重新配置。' : text(cap, 'description') || '没有能力说明。'}</p><footer><span>{scopeCount ? `${scopeCount} 项权限范围` : '未配置权限范围'}</span>{departedOwner ? <ProjectStatusBadge tone="neutral">历史只读</ProjectStatusBadge> : text(cap, 'risk_level') && <em>{text(cap, 'risk_level')} risk</em>}</footer></article>; })}</div> : <EmptyState icon={<IconTool size={22} />} title="没有符合条件的能力" description="添加项目共享 Skill/MCP，或启用成员带入的能力后会出现在这里。" />}
        <ProjectToolsControl projectId={projectId} members={members} policies={policies} runAction={runAction} busyAction={busyAction} /></>;
}

function CapabilityBindingsMatrix({ members, capabilities }: { members: RecordValue[]; capabilities: RecordValue[] }) {
    return <><SectionHeading eyebrow="EFFECTIVE CAPABILITIES" title="能力矩阵" description="最终能力 = 在岗成员 ∩ 项目分配 ∩ 成员启用 ∩ 角色策略 ∩ 安全边界；每次解析均写入审计。" />{members.length && capabilities.length ? <div className="project-workspace__matrix-wrap"><ProjectDataTable><ProjectDataTableHead><ProjectDataTableRow><ProjectDataTableHeader>能力</ProjectDataTableHeader><ProjectDataTableHeader>来源</ProjectDataTableHeader>{members.map((member) => <ProjectDataTableHeader key={text(member, 'agent_id', 'id', 'member_id')}>{text(member, 'agent_name', 'name_snapshot', 'name')}<small>{member.is_enabled === false ? '已退出 · 历史' : bool(member, 'is_leader') ? 'Leader' : '在岗'}</small></ProjectDataTableHeader>)}</ProjectDataTableRow></ProjectDataTableHead><ProjectDataTableBody>{capabilities.map((cap) => { const assignments = obj(cap.assignments); const inheritedAgentId = text(cap, 'inherited_from_agent_id'); const inherited = Boolean(inheritedAgentId) || ['agent', 'inherited'].includes(text(cap, 'source')); return <ProjectDataTableRow key={text(cap, 'id', 'binding_id', 'capability_id')}><ProjectDataTableCell><strong>{text(cap, 'name', 'capability_name')}</strong><small>{text(cap, 'capability_type', 'kind', 'type')}</small></ProjectDataTableCell><ProjectDataTableCell>{inherited ? 'Agent 带入' : '项目共享'}</ProjectDataTableCell>{members.map((member) => { const id = text(member, 'agent_id', 'id', 'member_id'); const explicitlyAssigned = Object.prototype.hasOwnProperty.call(assignments, id); const enabled = cap.is_enabled !== false; const resolved: 'yes' | 'no' | 'unknown' = member.is_enabled === false || !enabled ? 'no' : inheritedAgentId ? (inheritedAgentId === id ? 'yes' : 'no') : explicitlyAssigned ? (assignments[id] === false ? 'no' : 'yes') : 'unknown'; return <ProjectDataTableCell key={id}><span className={`project-workspace__matrix-${resolved}`}>{resolved === 'yes' ? <IconCircleCheck size={17} /> : resolved === 'no' ? <IconX size={16} /> : <IconClock size={16} />}<small>{member.is_enabled === false ? '历史只读' : resolved === 'yes' ? '可用' : resolved === 'no' ? '不可用' : '按策略解析'}</small></span></ProjectDataTableCell>; })}</ProjectDataTableRow>; })}</ProjectDataTableBody></ProjectDataTable></div> : <EmptyState icon={<IconCodeDots size={22} />} title="能力矩阵尚不可计算" description="至少需要一个项目成员和一个能力绑定。" />}</>;
}

function ProjectToolsMatrix({ members, policies }: { members: RecordValue[]; policies: RecordValue | null }) {
    return <section className="project-workspace__project-tool-matrix"><header className="project-workspace__subsection-heading"><div><span>PROJECT TOOL EFFECTIVE MATRIX</span><h3>项目管理工具有效矩阵</h3><p>同一 registry 依次应用成员生命周期、角色 ceiling、项目策略和成员关闭项；Human-only 操作不出现在矩阵中。</p></div><ProjectCountBadge>{PROJECT_TOOL_REGISTRY.length} 个原子工具</ProjectCountBadge></header>{members.length ? <div className="project-workspace__matrix-wrap"><ProjectDataTable><ProjectDataTableHead><ProjectDataTableRow><ProjectDataTableHeader>原子工具</ProjectDataTableHeader><ProjectDataTableHeader>角色基线</ProjectDataTableHeader>{members.map(member => <ProjectDataTableHeader key={text(member, 'id', 'member_id', 'agent_id')}>{text(member, 'name_snapshot', 'agent_name', 'name')}<small>{member.is_enabled === false ? '已退出 · 历史' : bool(member, 'is_leader') ? 'Leader' : '参与者'}</small></ProjectDataTableHeader>)}</ProjectDataTableRow></ProjectDataTableHead><ProjectDataTableBody>{PROJECT_TOOL_REGISTRY.map(tool => <ProjectDataTableRow key={tool.name}><ProjectDataTableCell><strong>{tool.label}</strong><small>{tool.name}</small></ProjectDataTableCell><ProjectDataTableCell>{tool.participant ? 'Leader / 参与者' : '仅 Leader'}</ProjectDataTableCell>{members.map(member => { const resolution = projectToolResolution(tool, member, policies); const label = resolution.lifecycleBlocked ? '成员已退出' : resolution.effective ? '有效' : !resolution.roleCeiling ? '超出角色' : resolution.memberDisabled ? '成员关闭' : resolution.policyBlocked ? '策略禁用' : '快照限制'; return <ProjectDataTableCell key={text(member, 'id', 'member_id', 'agent_id')}><span className={resolution.effective ? 'project-workspace__matrix-yes' : resolution.lifecycleBlocked || !resolution.roleCeiling ? 'project-workspace__matrix-no' : 'project-workspace__matrix-unknown'}>{resolution.effective ? <IconCircleCheck size={17} /> : resolution.lifecycleBlocked || !resolution.roleCeiling ? <IconX size={16} /> : <IconClock size={16} />}<small>{label}</small></span></ProjectDataTableCell>; })}</ProjectDataTableRow>)}</ProjectDataTableBody></ProjectDataTable></div> : <ProjectEmptyState icon={<IconUsers size={22} />} title="项目管理工具矩阵尚不可计算" description="至少需要一个项目成员。" />}</section>;
}

function CapabilityMatrix({ members, capabilities, policies }: { members: RecordValue[]; capabilities: RecordValue[]; policies: RecordValue | null }) {
    return <><CapabilityBindingsMatrix members={members} capabilities={capabilities} /><ProjectToolsMatrix members={members} policies={policies} /></>;
}

function ProjectVisibilitySettings({ projectId, project, onReload }: { projectId: string; project: ProjectSummary; onReload: () => Promise<void> }) {
    const currentUser = useAuthStore(state => state.user);
    const toast = useToast();
    const [visibility, setVisibility] = useState<'private' | 'shared'>(project.visibility);
    const [sharedUserIds, setSharedUserIds] = useState<string[]>(project.shared_with_user_ids || []);
    const [shareTargets, setShareTargets] = useState<Array<{ id: string; name: string; email?: string | null }>>([]);
    const [targetsLoading, setTargetsLoading] = useState(true);
    const [targetsError, setTargetsError] = useState('');
    const [saveError, setSaveError] = useState('');
    const [saving, setSaving] = useState(false);
    const [permissionDenied, setPermissionDenied] = useState(project.editable === false);

    useEffect(() => {
        setVisibility(project.visibility);
        setSharedUserIds(project.shared_with_user_ids || []);
        setPermissionDenied(project.editable === false);
        setSaveError('');
    }, [project.editable, project.shared_with_user_ids, project.updated_at, project.visibility]);

    useEffect(() => {
        let active = true;
        setTargetsLoading(true);
        setTargetsError('');
        void projectsApi.bootstrapOptions().then(options => {
            if (active) setShareTargets(options.users);
        }).catch(error => {
            if (active) setTargetsError(errorMessage(error));
        }).finally(() => {
            if (active) setTargetsLoading(false);
        });
        return () => { active = false; };
    }, []);

    const isOwner = Boolean(currentUser?.id && project.owner_id === currentUser.id);
    const knownNonOwner = Boolean(currentUser?.id && project.owner_id && !isOwner);
    const permissionUnknown = project.editable == null && (!currentUser?.id || !project.owner_id);
    const readOnly = permissionDenied || project.editable === false || knownNonOwner;
    const sharedWithoutMembers = visibility === 'shared' && sharedUserIds.length === 0;
    const save = async () => {
        if (readOnly) return;
        if (sharedWithoutMembers) {
            setSaveError('共享项目至少需要选择一名组织成员。');
            return;
        }
        setSaving(true);
        setSaveError('');
        try {
            await projectsApi.update(projectId, {
                visibility,
                shared_with_user_ids: visibility === 'shared' ? sharedUserIds : [],
            });
            toast.success(visibility === 'shared' ? '项目共享范围已保存' : '项目已设为仅自己可见');
            await onReload();
        } catch (error) {
            const status = Number(obj(error).status);
            const message = errorMessage(error);
            if (status === 403 || status === 404) setPermissionDenied(true);
            setSaveError(status === 403 || status === 404 ? '你没有管理此项目可见范围的权限，草稿已保留。' : message);
            toast.error(message);
        } finally {
            setSaving(false);
        }
    };
    const shareOptions = shareTargets.filter(user => user.id !== project.owner_id).map(user => ({
        value: user.id,
        label: user.email ? `${user.name} · ${user.email}` : user.name,
    }));

    return <section className="project-workspace__visibility-settings">
        <header><div><span>PROJECT ACCESS</span><h3>可见范围</h3><p>私有项目仅自己可见；共享项目只对选中的同组织成员开放。</p></div><ProjectStatusBadge tone={visibility === 'shared' ? 'info' : 'neutral'}>{visibility === 'shared' ? '已共享' : '仅自己可见'}</ProjectStatusBadge></header>
        <div className="project-workspace__visibility-body">
            <ProjectField label="访问模式" hint="切换为私有后，现有共享授权会全部撤销。"><ProjectSegmentedControl value={visibility} options={[{ value: 'private', label: '仅自己可见' }, { value: 'shared', label: '指定成员共享' }]} onChange={setVisibility} ariaLabel="项目可见范围" disabled={readOnly} /></ProjectField>
            <ProjectField label="共享成员" hint={visibility === 'shared' ? '成员必须来自当前组织；项目所有者无需重复选择。' : '切换到指定成员共享后可选择组织成员。'} error={sharedWithoutMembers ? '至少选择一名成员' : undefined}>
                <MultiSelectDropdown options={shareOptions} values={sharedUserIds} onChange={setSharedUserIds} emptyLabel={targetsLoading ? '正在读取组织成员…' : '选择共享成员'} selectedLabel={count => `已选择 ${count} 名成员`} searchPlaceholder="搜索组织成员" noOptionsLabel={targetsError ? '组织成员暂不可用' : '暂无可共享成员'} noMatchesLabel="没有匹配的组织成员" ariaLabel="选择项目共享成员" disabled={readOnly || visibility !== 'shared' || targetsLoading} />
            </ProjectField>
        </div>
        <footer><div>{targetsError && <small className="is-warning">组织成员读取失败：{targetsError}</small>}{permissionUnknown && !permissionDenied && <small>项目未返回可编辑标识，保存时将由服务端校验管理权限。</small>}{readOnly && <small className="is-warning">当前账号不能管理此项目的可见范围。</small>}{saveError && <small className="is-error" role="alert">{saveError}</small>}</div><Button variant="secondary" onClick={() => void save()} disabled={readOnly || saving || sharedWithoutMembers}>{saving ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconLock size={16} />}保存可见范围</Button></footer>
    </section>;
}

function PoliciesPanel({ projectId, project, policies, onReload, runAction, busyAction }: { projectId: string; project: ProjectSummary; policies: RecordValue | null; onReload: () => Promise<void>; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const runtime = obj(policies?.runtime);
    const governance = obj(policies?.policies);
    const [model, setModel] = useState('default');
    const [budget, setBudget] = useState('0');
    const [approval, setApproval] = useState('risk');
    const [parallel, setParallel] = useState('4');
    const [a2aLimit, setA2aLimit] = useState('12');
    const [loopGuard, setLoopGuard] = useState(true);
    useEffect(() => { const nextRuntime = obj(policies?.runtime); const nextGovernance = obj(policies?.policies); setModel(text(nextRuntime, 'model', 'default_model') || 'default'); setBudget(text(nextRuntime, 'monthly_budget', 'budget_limit') || '0'); setApproval(text(nextGovernance, 'approval_policy') || 'risk'); setParallel(text(nextRuntime, 'max_parallel_runs') || '4'); setA2aLimit(text(nextGovernance, 'max_a2a_wakes') || '12'); setLoopGuard(nextGovernance.loop_guard !== false); }, [policies]);
    const save = () => void runAction('save-policies', () => projectsApi.updateSettings(projectId, { runtime: { ...runtime, model, monthly_budget: Number(budget), max_parallel_runs: Number(parallel) }, policies: { ...governance, approval_policy: approval, max_a2a_wakes: Number(a2aLimit), loop_guard: loopGuard } }), '项目策略已保存，新运行将使用最新版本');
    const approvalOptions = [{ value: 'risk', label: '仅高风险操作' }, { value: 'all_writes', label: '所有写操作' }, { value: 'manual', label: '手动审批' }];
    return <><SectionHeading eyebrow="POLICIES / SAFETY" title="运行与安全策略" description="模型、预算、凭证和审批规则在项目范围生效；保存后由服务端持久化。" actions={<Button variant="primary" onClick={save} disabled={busyAction === 'save-policies'}>{busyAction === 'save-policies' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconDeviceFloppy size={16} />}保存策略</Button>} /><ProjectVisibilitySettings projectId={projectId} project={project} onReload={onReload} /><div className="project-workspace__settings-grid"><ProjectField label="默认模型" labelFor="project-policy-model"><TextInput id="project-policy-model" value={model} onChange={(event) => setModel(event.target.value)} placeholder="default" /></ProjectField><ProjectField label="月度预算" labelFor="project-policy-budget"><TextInput id="project-policy-budget" type="number" min="0" step="1" value={budget} onChange={(event) => setBudget(event.target.value)} /></ProjectField><ProjectField label="审批策略"><ProjectSelect value={approval} options={approvalOptions} onChange={setApproval} ariaLabel="审批策略" /></ProjectField><ProjectField label="最大并行 Run" labelFor="project-policy-parallel"><TextInput id="project-policy-parallel" type="number" min="1" max="32" value={parallel} onChange={(event) => setParallel(event.target.value)} /></ProjectField><ProjectField label="单次 Run 最大 A2A 唤醒" labelFor="project-policy-a2a-limit"><TextInput id="project-policy-a2a-limit" type="number" min="1" max="100" value={a2aLimit} onChange={(event) => setA2aLimit(event.target.value)} /></ProjectField><div className="project-workspace__switch-setting"><span><strong>循环保护</strong><small>阻止重复唤醒与无界委派</small></span><ToggleSwitch checked={loopGuard} onChange={setLoopGuard} ariaLabel="循环保护" /></div></div><div className="project-workspace__policy-grid">{[
        ['模型策略', model, '成员新运行默认使用的模型策略。', <IconSparkles size={18} />],
        ['预算边界', budget ? `每月 ${budget}` : '未设置', '到达阈值时阻止创建新 Run。', <IconActivityHeartbeat size={18} />],
        ['审批规则', approval, '高风险写入和外部动作按此审批。', <IconShieldCheck size={18} />],
        ['循环保护', loopGuard ? '已启用' : '已关闭', `最大 A2A 唤醒 ${a2aLimit} 次。`, <IconRefresh size={18} />],
    ].map(([title, value, desc, icon]) => <article key={String(title)}><span>{icon}</span><div><strong>{title}</strong><p>{desc}</p><code>{String(value)}</code></div></article>)}</div></>;
}

function GitRepositoryControls({ projectId, project, repository, commits, onReload }: { projectId: string; project: ProjectSummary; repository: RecordValue; commits: RecordValue[]; onReload: () => Promise<void> }) {
    const currentUser = useAuthStore(state => state.user);
    const toast = useToast();
    const isOwner = Boolean(currentUser?.id && project.owner_id === currentUser.id);
    const [remotes, setRemotes] = useState<Array<{ name: string; url: string }>>([]);
    const [remotesLoading, setRemotesLoading] = useState(false);
    const [remoteError, setRemoteError] = useState('');
    const [remoteName, setRemoteName] = useState('');
    const [remoteUrl, setRemoteUrl] = useState('');
    const [editingRemote, setEditingRemote] = useState('');
    const [cloneUrl, setCloneUrl] = useState('');
    const [cloneBranch, setCloneBranch] = useState('');
    const [cloneConfirmOpen, setCloneConfirmOpen] = useState(false);
    const [busy, setBusy] = useState('');

    const loadRemotes = useCallback(async () => {
        if (!isOwner) {
            setRemotes([]);
            setRemoteError('');
            return;
        }
        setRemotesLoading(true);
        setRemoteError('');
        try {
            setRemotes(await projectsApi.listGitRemotes(projectId));
        } catch (error) {
            setRemoteError(errorMessage(error));
        } finally {
            setRemotesLoading(false);
        }
    }, [isOwner, projectId]);
    useEffect(() => { void loadRemotes(); }, [loadRemotes]);

    const repositoryFiles = Array.isArray(repository.files) ? repository.files.map(String).sort() : [];
    const source = text(repository, 'source') === 'cloned' ? 'cloned' : 'managed';
    const initializationOnly = source !== 'cloned'
        && commits.length === 1
        && text(commits[0], 'message', 'subject', 'title') === 'Initialize AI-native project'
        && repositoryFiles.length === 2
        && repositoryFiles[0] === 'PROJECT.json'
        && repositoryFiles[1] === 'README.md';
    const canClone = isOwner && project.status === 'planning' && initializationOnly;
    const cloneUnavailableReason = !isOwner
        ? '只有项目所有者可以替换仓库来源。'
        : project.status !== 'planning'
            ? '项目已离开规划阶段，不能再替换仓库。'
            : !initializationOnly
                ? '仓库已包含项目产出，不能再执行初始化克隆。'
                : '';

    const resetRemoteDraft = () => {
        setEditingRemote('');
        setRemoteName('');
        setRemoteUrl('');
    };
    const saveRemote = async (event: FormEvent) => {
        event.preventDefault();
        if (!isOwner || !remoteName.trim() || !remoteUrl.trim()) return;
        setBusy('remote-save');
        setRemoteError('');
        try {
            await projectsApi.putGitRemote(projectId, remoteName.trim(), remoteUrl.trim());
            toast.success(editingRemote ? `远端 ${remoteName.trim()} 已更新` : `远端 ${remoteName.trim()} 已添加`);
            resetRemoteDraft();
            await loadRemotes();
            await onReload();
        } catch (error) {
            const message = errorMessage(error);
            setRemoteError(message);
            toast.error(message);
        } finally {
            setBusy('');
        }
    };
    const deleteRemote = async (name: string) => {
        if (!isOwner) return;
        setBusy(`remote-delete-${name}`);
        setRemoteError('');
        try {
            await projectsApi.deleteGitRemote(projectId, name);
            toast.success(`远端 ${name} 已删除`);
            if (editingRemote === name) resetRemoteDraft();
            await loadRemotes();
            await onReload();
        } catch (error) {
            const message = errorMessage(error);
            setRemoteError(message);
            toast.error(message);
        } finally {
            setBusy('');
        }
    };
    const cloneRepository = async (event: FormEvent) => {
        event.preventDefault();
        if (!canClone || !cloneUrl.trim()) return;
        setBusy('clone');
        try {
            await projectsApi.cloneGitRepository(projectId, { url: cloneUrl.trim(), branch: cloneBranch.trim() || undefined });
            toast.success('远程仓库已原子克隆，项目 Git 基线已更新');
            setCloneConfirmOpen(false);
            setCloneUrl('');
            setCloneBranch('');
            await loadRemotes();
            await onReload();
        } catch (error) {
            toast.error(errorMessage(error));
        } finally {
            setBusy('');
        }
    };

    return <section className="project-workspace__repository-control">
        <header><div><span>HUMAN-ONLY REPOSITORY</span><h3>仓库来源与远端</h3><p>Git 仓库来源和远端只由人类所有者管理，不进入 Agent 工具集。</p></div><ProjectStatusBadge tone="warning">Human-only</ProjectStatusBadge></header>
        <div className="project-workspace__repository-summary"><article><span>仓库来源</span><strong>{source === 'cloned' ? '远程仓库克隆' : '平台初始化仓库'}</strong><small>{source === 'cloned' ? '当前内容来自一次原子 clone' : '当前为平台创建的 managed Git 基线'}</small></article><article><span>默认分支</span><strong>{text(repository, 'default_branch') || text(repository, 'branch') || '未记录'}</strong><small>HEAD <code>{text(repository, 'head').slice(0, 12) || '—'}</code></small></article><article><span>远端数量</span><strong>{isOwner ? remotes.length : '仅所有者可见'}</strong><small>支持任意标准 Git 服务商</small></article></div>
        <div className="project-workspace__repository-grid">
            <section><header><div><span>REMOTES</span><h4>远端列表</h4></div><ProjectCountBadge>{isOwner ? remotes.length : 0}</ProjectCountBadge></header>{isOwner ? remotes.length ? <ProjectDataTable className="project-workspace__remote-table"><ProjectDataTableHead><ProjectDataTableRow><ProjectDataTableHeader>名称</ProjectDataTableHeader><ProjectDataTableHeader>地址</ProjectDataTableHeader><ProjectDataTableHeader>操作</ProjectDataTableHeader></ProjectDataTableRow></ProjectDataTableHead><ProjectDataTableBody>{remotes.map(remote => <ProjectDataTableRow key={remote.name}><ProjectDataTableCell><code>{remote.name}</code></ProjectDataTableCell><ProjectDataTableCell><code title={remote.url}>{remote.url}</code></ProjectDataTableCell><ProjectDataTableCell><div className="project-workspace__remote-actions"><Button variant="ghost" disabled={Boolean(busy)} onClick={() => { setEditingRemote(remote.name); setRemoteName(remote.name); setRemoteUrl(remote.url); }}>修改</Button><ProjectIconButton aria-label={`删除远端 ${remote.name}`} disabled={Boolean(busy)} onClick={() => void deleteRemote(remote.name)}>{busy === `remote-delete-${remote.name}` ? <IconLoader2 className="project-workspace__spinner" size={15} /> : <IconTrash size={15} />}</ProjectIconButton></div></ProjectDataTableCell></ProjectDataTableRow>)}</ProjectDataTableBody></ProjectDataTable> : <ProjectEmptyState title={remotesLoading ? '正在读取远端' : '还没有配置远端'} description="添加后可记录上游地址；配置远端不会改变当前提交历史。" /> : <ProjectEmptyState icon={<IconLock size={20} />} title="远端仅所有者可管理" description="共享成员仍可查看项目提交历史，但不能读取或修改仓库远端。" />}
                {isOwner && <form className="project-workspace__remote-form" onSubmit={saveRemote}><ProjectField label="远端名称" labelFor="project-remote-name" required><TextInput id="project-remote-name" value={remoteName} onChange={event => setRemoteName(event.target.value)} placeholder="origin" pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}" disabled={Boolean(editingRemote)} required /></ProjectField><ProjectField label="远端 URL" labelFor="project-remote-url" required><TextInput id="project-remote-url" value={remoteUrl} onChange={event => setRemoteUrl(event.target.value)} placeholder="https://git.example.com/team/project.git" required /></ProjectField><footer>{editingRemote && <Button type="button" variant="ghost" onClick={resetRemoteDraft}>取消修改</Button>}<Button type="submit" variant="secondary" disabled={!remoteName.trim() || !remoteUrl.trim() || Boolean(busy)}>{busy === 'remote-save' ? <IconLoader2 className="project-workspace__spinner" size={15} /> : editingRemote ? <IconDeviceFloppy size={15} /> : <IconPlus size={15} />}{editingRemote ? '保存远端' : '添加远端'}</Button></footer></form>}
            </section>
            <section><header><div><span>INITIAL SOURCE</span><h4>从远程初始化</h4></div><ProjectStatusBadge tone={canClone ? 'success' : 'neutral'}>{canClone ? '可克隆' : '不可克隆'}</ProjectStatusBadge></header><div className="project-workspace__clone-form"><ProjectField label="仓库 URL" labelFor="project-clone-url" required><TextInput id="project-clone-url" value={cloneUrl} onChange={event => setCloneUrl(event.target.value)} placeholder="https://… 或 ssh://…" disabled={!canClone || Boolean(busy)} /></ProjectField><ProjectField label="分支（可选）" labelFor="project-clone-branch"><TextInput id="project-clone-branch" value={cloneBranch} onChange={event => setCloneBranch(event.target.value)} placeholder="留空使用远端默认分支" disabled={!canClone || Boolean(busy)} /></ProjectField><div className="project-workspace__clone-note"><IconShieldCheck size={17} /><p>支持 HTTPS 与 SSH。凭证不得写入 URL；私有 SSH 仓库使用服务端已配置的密钥，并以非交互模式连接。</p></div>{cloneUnavailableReason && <small>{cloneUnavailableReason}</small>}<Button variant="danger" disabled={!canClone || !cloneUrl.trim() || Boolean(busy)} onClick={() => setCloneConfirmOpen(true)}><IconBrandGit size={16} />确认远程来源</Button></div></section>
        </div>
        {remoteError && <div className="project-workspace__repository-error" role="alert"><IconAlertTriangle size={16} /><span>{remoteError}</span></div>}
        <ProjectDialog open={cloneConfirmOpen} onClose={() => { if (busy !== 'clone') setCloneConfirmOpen(false); }} ariaLabel="确认从远程仓库初始化" className="project-workspace__git-dialog"><form className="project-workspace__modal" onSubmit={cloneRepository}><header><div><span>ATOMIC REPOSITORY REPLACEMENT</span><h2>确认替换初始化仓库</h2></div><ProjectIconButton aria-label="关闭" disabled={busy === 'clone'} onClick={() => setCloneConfirmOpen(false)}><IconX size={18} /></ProjectIconButton></header><p>系统将先在隔离目录完成 clone 与 Git 校验，再原子替换当前仅含初始化文件的仓库。该操作只允许执行一次；已有项目产出时服务端会拒绝。</p><dl className="project-workspace__definition-list"><div><dt>远端</dt><dd><code>{cloneUrl}</code></dd></div><div><dt>分支</dt><dd><code>{cloneBranch || '远端默认分支'}</code></dd></div></dl><div className="project-workspace__safe-note"><IconLock size={16} /><span>不要在 URL 中携带用户名、密码、Token 或私钥。</span></div><footer><Button type="button" variant="secondary" disabled={busy === 'clone'} onClick={() => setCloneConfirmOpen(false)}>取消</Button><Button type="submit" variant="danger" disabled={busy === 'clone'}>{busy === 'clone' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconBrandGit size={16} />}原子克隆并替换</Button></footer></form></ProjectDialog>
    </section>;
}

function GitPanel({ projectId, project, repository, commits, events, selected, onSelect, onDialog, onOpenSession, onReload }: { projectId: string; project: ProjectSummary; repository: RecordValue; commits: RecordValue[]; events: RecordValue[]; selected: RecordValue | null; onSelect: (commit: RecordValue) => void; onDialog: (mode: 'restore' | 'branch') => void; onOpenSession: OpenSession; onReload: () => Promise<void> }) {
    const selectedId = text(selected || {}, 'commit', 'hash', 'commit_hash', 'id');
    const eventsForCommit = (commit: RecordValue) => {
        const commitId = text(commit, 'commit', 'hash', 'commit_hash', 'id');
        return events.filter((event) => traceValue(traceRecords(event), 'commit_hash', 'commit', 'hash', 'from_commit', 'source_commit') === commitId);
    };
    const refsForCommit = (commit: RecordValue, index: number) => {
        const refs = new Set<string>();
        if (index === 0) refs.add('HEAD');
        [commit.branch, commit.branches, commit.refs].forEach((value) => {
            if (Array.isArray(value)) value.forEach((entry) => { if (entry) refs.add(String(entry)); });
            else if (typeof value === 'string' && value.trim()) value.split(',').forEach((entry) => refs.add(entry.trim()));
        });
        eventsForCommit(commit).forEach((event) => {
            if (text(event, 'event_type', 'type') !== 'git.branch.created') return;
            const branch = traceValue(traceRecords(event), 'branch', 'branch_name');
            if (branch) refs.add(branch);
        });
        return [...refs];
    };
    const isMilestone = (commit: RecordValue) => bool(commit, 'milestone') || eventsForCommit(commit).some((event) => text(event, 'event_type', 'type') === 'git.milestone.created' || traceRecords(event).some((record) => record.milestone === true));
    const selectedIndex = Math.max(0, commits.findIndex((commit) => text(commit, 'commit', 'hash', 'commit_hash', 'id') === selectedId));
    const selectedRefs = selected ? refsForCommit(selected, selectedIndex) : [];
    const selectedEvents = selected ? eventsForCommit(selected) : [];
    const linkedEvent = selectedEvents.find((event) => sessionIdOf(event)) || selectedEvents[0];
    const sessionSource = sessionIdOf(selected || {}) ? selected || {} : linkedEvent || selected || {};
    return <><SectionHeading eyebrow="GIT / TRACEABILITY" title="Git 历史与恢复" description="使用标准 Git 日志追溯项目产物；恢复会创建新提交，禁止 reset --hard 与 force push。" /><GitRepositoryControls projectId={projectId} project={project} repository={repository} commits={commits} onReload={onReload} />{commits.length ? <div className="project-workspace__git-layout"><section className="project-workspace__card project-workspace__git-log" aria-label="Git 提交历史"><header><div><span>COMMIT LOG</span><h3>提交历史</h3></div><ProjectCountBadge>{commits.length}</ProjectCountBadge></header><div className="project-workspace__git-log-head" aria-hidden="true"><span>Commit</span><span>说明</span><span>作者</span><span>时间</span><span>引用</span></div><ol>{commits.map((commit, index) => { const hash = text(commit, 'commit', 'hash', 'commit_hash', 'id'); const refs = refsForCommit(commit, index); const milestone = isMilestone(commit); return <li key={hash}><Button type="button" variant="ghost" className={hash === selectedId ? 'is-active' : ''} aria-pressed={hash === selectedId} onClick={() => onSelect(commit)}><code title={hash}>{text(commit, 'short_commit') || hash.slice(0, 12)}</code><span className="project-workspace__git-log-message"><strong>{text(commit, 'message', 'subject', 'title') || '未命名提交'}</strong><small>{hash}</small></span><span>{text(commit, 'author', 'author_name', 'actor_name') || 'Clawith'}</span><time dateTime={text(commit, 'created_at', 'timestamp')}>{dateLabel(commit.created_at || commit.timestamp)}</time><span className="project-workspace__git-refs">{refs.map((ref) => <ProjectStatusBadge key={ref} tone={ref === 'HEAD' ? 'success' : 'info'}>{ref}</ProjectStatusBadge>)}{milestone && <ProjectStatusBadge tone="warning">里程碑</ProjectStatusBadge>}{!refs.length && !milestone && <small>—</small>}</span></Button></li>; })}</ol></section><aside className="project-workspace__card project-workspace__commit-detail"><header><div><span>提交详情</span><h3>{text(selected || {}, 'message', 'subject', 'title') || '未命名提交'}</h3></div></header><div className="project-workspace__repro"><IconCircleCheck size={18} /><span><strong>历史保留</strong><small>恢复操作不会删除现有提交</small></span></div><dl className="project-workspace__definition-list"><div><dt>Commit</dt><dd><code>{selectedId || '—'}</code></dd></div><div><dt>作者</dt><dd>{text(selected || {}, 'author', 'author_name', 'actor_name') || 'Clawith'}</dd></div><div><dt>时间</dt><dd>{dateLabel(selected?.created_at || selected?.timestamp)}</dd></div><div><dt>引用</dt><dd className="project-workspace__git-detail-refs">{selectedRefs.map((ref) => <ProjectStatusBadge key={ref} tone={ref === 'HEAD' ? 'success' : 'info'}>{ref}</ProjectStatusBadge>)}{selected && isMilestone(selected) && <ProjectStatusBadge tone="warning">里程碑</ProjectStatusBadge>}{!selectedRefs.length && !(selected && isMilestone(selected)) && '—'}</dd></div><div><dt>关联 Run</dt><dd>{traceValue(traceRecords(linkedEvent || selected || {}), 'run_id') || '—'}</dd></div><div><dt>工作项</dt><dd>{traceValue(traceRecords(linkedEvent || selected || {}), 'work_item_id') || '—'}</dd></div><div><dt>会话</dt><dd>{sessionIdOf(sessionSource) || '—'}<SessionButton source={sessionSource} onOpen={onOpenSession} /></dd></div><div><dt>变更文件</dt><dd>{text(selected || {}, 'file_count', 'files_changed') || '—'}</dd></div></dl><div className="project-workspace__git-actions"><Button variant="secondary" onClick={() => onDialog('branch')}><IconGitBranch size={16} />从此创建分支</Button><Button variant="danger" onClick={() => onDialog('restore')}><IconRestore size={16} />还原为新提交</Button></div><p><IconLock size={14} /> 保护规则：禁止 force push 与 reset --hard</p></aside></div> : <EmptyState icon={<IconBrandGit size={22} />} title="还没有提交历史" description="项目产物提交后，可在这里查看 Commit、作者、时间、分支、里程碑与关联会话。" />}</>;
}

function AuditPanel({ events, members, onRefresh, onOpenSession }: { events: RecordValue[]; members: RecordValue[]; onRefresh: () => Promise<void>; onOpenSession: OpenSession }) {
    const { t } = useTranslation();
    const [query, setQuery] = useState('');
    const [actor, setActor] = useState('');
    const [kind, setKind] = useState('');
    const memberNames = useMemo(() => new Map(members.map((member) => [text(member, 'agent_id'), text(member, 'name_snapshot', 'agent_name', 'name') || t('projectAudit.projectAgent')])), [members, t]);
    const actorLabel = useCallback((event: RecordValue) => {
        const agentId = text(event, 'actor_agent_id');
        if (agentId) return memberNames.get(agentId) || t('projectAudit.projectAgent');
        if (text(event, 'actor_user_id')) return t('projectAudit.projectUser');
        return t('projectAudit.projectSystem');
    }, [memberNames, t]);
    const eventLabel = useCallback((event: RecordValue) => { const code = text(event, 'type', 'event_type'); return t(`projectAudit.events.${code}`, { defaultValue: code }); }, [t]);
    const actors = useMemo(() => Array.from(new Set(events.map(actorLabel))), [actorLabel, events]);
    const kinds = useMemo(() => Array.from(new Set(events.map((event) => text(event, 'type', 'event_type')).filter(Boolean))), [events]);
    const visible = events.filter((event) => (!query || `${JSON.stringify(event)} ${eventLabel(event)} ${actorLabel(event)}`.toLowerCase().includes(query.toLowerCase())) && (!actor || actorLabel(event) === actor) && (!kind || text(event, 'type', 'event_type') === kind));
    return <><SectionHeading eyebrow="EVENT AUDIT" title="项目事件审计" description="按因果链记录人、Agent 与系统动作，并关联会话、工作项、运行和 Git 提交。" actions={<Button variant="secondary" onClick={() => void onRefresh()}><IconRefresh size={16} />刷新审计</Button>} /><div className="project-workspace__audit-filters"><SearchInput value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索事件、会话或 Commit" aria-label="搜索审计事件" /><ProjectSelect value={actor} options={actors.map((value) => ({ value, label: value }))} onChange={setActor} ariaLabel={t('projectAudit.filterActor')} placeholder={t('projectAudit.allActors')} /><ProjectSelect value={kind} options={kinds.map((value) => ({ value, label: eventLabel({ event_type: value }) }))} onChange={setKind} ariaLabel="筛选事件类型" placeholder="全部事件" /><Button variant="ghost" onClick={() => { setQuery(''); setActor(''); setKind(''); }}><IconFilter size={15} />清除筛选</Button></div>{visible.length ? <ProjectDataTable className="project-workspace__audit-table"><ProjectDataTableHead><ProjectDataTableRow><ProjectDataTableHeader>时间</ProjectDataTableHeader><ProjectDataTableHeader>{t('projectAudit.actorColumn')}</ProjectDataTableHeader><ProjectDataTableHeader>事件</ProjectDataTableHeader><ProjectDataTableHeader>详情</ProjectDataTableHeader><ProjectDataTableHeader>关联</ProjectDataTableHeader></ProjectDataTableRow></ProjectDataTableHead><ProjectDataTableBody>{visible.map((event) => { const eventCode = text(event, 'type', 'event_type'); return <ProjectDataTableRow key={text(event, 'id', 'event_id') || `${text(event, 'created_at')}-${eventCode}`}><ProjectDataTableCell>{dateLabel(event.created_at)}<small>{text(event, 'id', 'event_id')}</small></ProjectDataTableCell><ProjectDataTableCell>{actorLabel(event)}</ProjectDataTableCell><ProjectDataTableCell><span>{eventLabel(event)}</span><small><code>{eventCode}</code></small></ProjectDataTableCell><ProjectDataTableCell>{text(event, 'message', 'summary', 'detail') || '—'}</ProjectDataTableCell><ProjectDataTableCell>{text(event, 'run_id') && <em>{text(event, 'run_id')}</em>}{text(event, 'commit_hash') && <code>{text(event, 'commit_hash')}</code>}<SessionButton source={event} onOpen={onOpenSession} /></ProjectDataTableCell></ProjectDataTableRow>; })}</ProjectDataTableBody></ProjectDataTable> : <EmptyState icon={<IconHistory size={22} />} title={events.length ? '没有符合条件的事件' : '还没有审计事件'} description={events.length ? '调整或清除筛选条件后重试。' : '项目操作发生后，审计事件会按时间和因果链显示。'} />}</>;
}

function GitActionDialog({ projectId, mode, commit, busy, onClose, runAction }: { projectId: string; mode: 'restore' | 'branch'; commit: RecordValue; busy: string; onClose: () => void; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean> }) {
    const hash = text(commit, 'commit', 'hash', 'commit_hash', 'id');
    const [branchName, setBranchName] = useState(`restore/${new Date().toISOString().slice(0, 10)}`);
    const submit = (event: FormEvent) => {
        event.preventDefault();
        const promise = mode === 'restore'
            ? () => projectsApi.restoreCommit(projectId, { commit: hash })
            : () => projectsApi.createBranch(projectId, { from_commit: hash, name: branchName });
        void runAction(`git-${mode}`, promise, mode === 'restore' ? 'Restore 提交已创建；原历史保持不变' : '新分支已创建').then((succeeded) => { if (succeeded) onClose(); });
    };
    return <ProjectDialog open onClose={onClose} ariaLabel={mode === 'restore' ? '还原为新提交' : '从旧提交创建分支'} className="project-workspace__git-dialog"><form className="project-workspace__modal" onSubmit={submit}><header><div><span>GIT SAFE OPERATION</span><h2 id="project-git-dialog-title">{mode === 'restore' ? '还原为新提交' : '从旧提交创建分支'}</h2></div><ProjectIconButton aria-label="关闭" onClick={onClose}><IconX size={18} /></ProjectIconButton></header><p>{mode === 'restore' ? <>系统会以当前分支为基础，把 <code>{hash}</code> 的内容还原为一个新的 commit。现有提交、会话和审计事件不会删除。</> : <>从 <code>{hash}</code> 创建独立分支，当前分支与全部历史保持不变。</>}</p>{mode === 'branch' && <ProjectField label="分支名称" labelFor="project-git-branch" required><TextInput id="project-git-branch" value={branchName} onChange={(e) => setBranchName(e.target.value)} required pattern="[A-Za-z0-9._/-]+" /></ProjectField>}<div className="project-workspace__safe-note"><IconLock size={16} /><span>系统不会执行 force push 或 reset --hard。</span></div><footer><Button type="button" variant="secondary" onClick={onClose}>取消</Button><Button type="submit" variant={mode === 'restore' ? 'danger' : 'primary'} disabled={busy === `git-${mode}`}>{busy === `git-${mode}` && <IconLoader2 className="project-workspace__spinner" size={16} />}{mode === 'restore' ? '创建 Restore 提交' : '创建分支'}</Button></footer></form></ProjectDialog>;
}
