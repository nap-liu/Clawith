import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react';
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
    IconSend,
    IconSettings,
    IconShieldCheck,
    IconSparkles,
    IconTargetArrow,
    IconTool,
    IconUsers,
    IconX,
} from '@tabler/icons-react';

import { useToast } from '../../components/Toast/ToastProvider';
import SessionViewerDrawer, { type SessionViewerTarget } from '../../components/SessionViewerDrawer';
import { projectsApi } from '../../services/projects';
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
import './projectWorkspace.css';

type RecordValue = Record<string, unknown>;
type ProjectSessionTarget = SessionViewerTarget & { agentName: string; kind?: 'group' | 'session' };
type OpenSession = (source: RecordValue, title?: string) => void;
type WorkspaceTab =
    | 'cockpit'
    | 'work'
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
const statusLabel = (status: string): string => ({
    running: '运行中', queued: '排队中', waiting: '等待中', paused: '已暂停', completed: '已完成', success: '已完成', succeeded: '已完成', cancelled: '已取消', failed: '失败', blocked: '阻塞', review: '待评审', backlog: '待规划', todo: '待处理', doing: '进行中', in_progress: '进行中', done: '已完成',
}[status] || status || '未知');
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

    const load = useCallback(async () => {
        if (!projectId) {
            setError('缺少项目 ID，无法加载项目工作台。');
            setLoading(false);
            return;
        }
        setLoading(true);
        setError('');
        setResourceWarnings([]);
        try {
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
                files: arr(serverFiles).length ? arr(serverFiles) : rawFiles.map((entry) => typeof entry === 'string' ? { id: entry, path: entry, name: entry.split('/').pop() || entry } : obj(entry)),
                policies: settings ? obj(settings) : payload.policies ? obj(payload.policies) : Object.keys(projectSettings).length ? projectSettings : null,
                groupSession: groupSessionResult.status === 'fulfilled' ? obj(groupSessionResult.value) : null,
            };
            setData(next);
            setSelectedWorkItemId((current) => current || text(next.workItems[0] || {}, 'id', 'work_item_id'));
            setSelectedMemberId((current) => current || text(next.members[0] || {}, 'id', 'member_id', 'agent_id'));
            setSelectedCommit((current) => current || next.commits[0] || null);
        } catch (loadError) {
            setError(errorMessage(loadError));
            setData(null);
        } finally {
            setLoading(false);
        }
    }, [projectId]);

    useEffect(() => { void load(); }, [load]);
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
    const openGroupSession = useCallback(() => {
        if (!data?.groupSession) {
            toast.warning('项目群聊暂不可用');
            return;
        }
        const group = data.groupSession;
        const sessionId = text(group, 'id', 'group_session_id', 'session_id');
        const agentId = text(group, 'access_agent_id', 'session_agent_id', 'agent_id');
        if (!sessionId || !agentId) {
            toast.warning('项目群聊缺少会话路由');
            return;
        }
        setSessionTarget({ sessionId, agentId, agentName: '项目群聊', title: text(group, 'title', 'group_name') || `${data.project.name} · 项目群聊`, mode: 'group', kind: 'group' });
    }, [data, toast]);
    const groupConfig = useMemo(() => ({
        members: (data?.members || []).map((member) => ({
            agentId: text(member, 'agent_id'),
            name: text(member, 'name_snapshot', 'agent_name', 'name') || 'Agent',
        })).filter((member) => member.agentId),
        maxMentions: 4,
        loadMessages: (sessionId: string) => projectsApi.listGroupMessages(projectId, sessionId),
        sendMessage: (sessionId: string, payload: { content: string; llm_content?: string; mentions: string[]; attachments: Array<Record<string, unknown>> }) => projectsApi.sendGroupMessage(projectId, sessionId, payload),
    }), [data?.members, projectId]);

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
            case 'mesh': return <MeshPanel projectId={projectId} members={data.members} events={data.events} groupSession={data.groupSession} onOpenGroup={openGroupSession} onOpenSession={openSession} runAction={runAction} busyAction={busyAction} />;
            case 'detail': return <WorkItemDetail projectId={projectId} items={data.workItems} members={data.members} selectedId={selectedWorkItemId} onSelect={setSelectedWorkItemId} onOpenSession={openSession} runAction={runAction} busyAction={busyAction} />;
            case 'files': return <FilesPanel projectId={projectId} files={data.files} commits={data.commits} runAction={runAction} busyAction={busyAction} />;
            case 'runs': return <RunsPanel projectId={projectId} runs={data.runs} onOpenSession={openSession} runAction={runAction} busyAction={busyAction} />;
            case 'members': return <MembersPanel projectId={projectId} members={data.members} runs={data.runs} selectedId={selectedMemberId} onSelect={setSelectedMemberId} runAction={runAction} busyAction={busyAction} />;
            case 'capabilities': return <CapabilitiesPanel projectId={projectId} capabilities={data.capabilities} runAction={runAction} busyAction={busyAction} />;
            case 'matrix': return <CapabilityMatrix members={data.members} capabilities={data.capabilities} />;
            case 'policies': return <PoliciesPanel projectId={projectId} policies={data.policies} runAction={runAction} busyAction={busyAction} />;
            case 'git': return <GitPanel commits={data.commits} events={data.events} selected={selectedCommit} onSelect={setSelectedCommit} onDialog={setGitDialog} onOpenSession={openSession} />;
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
                <div className="project-workspace__content" key={tab}>{resourceWarnings.length > 0 && <div className="project-workspace__resource-warning" role="status"><IconAlertTriangle size={17} /><div><strong>部分项目资源暂不可用</strong><p>{resourceWarnings.join('；')}</p></div><Button variant="ghost" onClick={() => void load()}><IconRefresh size={15} />重试</Button></div>}{renderContent()}</div>
            </div>

            {gitDialog && selectedCommit && <GitActionDialog projectId={projectId} mode={gitDialog} commit={selectedCommit} busy={busyAction} onClose={() => setGitDialog(null)} runAction={runAction} />}
            <SessionViewerDrawer agentId={sessionTarget?.agentId || ''} agentName={sessionTarget?.agentName || 'Agent'} target={sessionTarget} interactive groupConfig={sessionTarget?.kind === 'group' ? groupConfig : undefined} onClose={() => setSessionTarget(null)} />
        </main>
    );
}

function Cockpit({ data, onNavigate, runAction, busyAction }: { data: WorkspaceData; onNavigate: (tab: WorkspaceTab) => void; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const activeRun = data.runs.find((run) => ['running', 'queued', 'waiting'].includes(text(run, 'status'))) || data.runs[0];
    const runningItems = data.workItems.filter((item) => ['doing', 'running', 'in_progress', 'review'].includes(text(item, 'status', 'state')));
    const blockedItems = data.workItems.filter((item) => ['blocked', 'failed'].includes(text(item, 'status', 'state')));
    const progress = activeRun ? num(activeRun, 'progress', 'progress_percent') : data.project.progress;
    const pauseOrResume = text(activeRun || {}, 'status') === 'waiting' ? 'running' : 'waiting';
    const activeRunId = text(activeRun || {}, 'id', 'run_id');
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
        {activeRun ? <section className="project-workspace__run-hero">
            <div className="project-workspace__pulse"><IconBolt size={22} /></div>
            <div className="project-workspace__run-copy"><div><StatusPill status={text(activeRun, 'status')} /><code>{activeRunId || 'RUN'}</code></div><h3>{text(activeRun, 'name', 'objective', 'title') || data.project.objective}</h3><p>开始于 {dateLabel(activeRun.started_at || activeRun.created_at)} · 分支 <code>{text(activeRun, 'branch', 'git_branch') || '—'}</code></p><ProjectProgressBar value={progress} label="当前 Run 进度" className="project-workspace__progress" /></div>
            <div className="project-workspace__run-actions"><Button variant="secondary" disabled={!activeRunId || busyAction === 'run-state'} onClick={() => void runAction('run-state', () => projectsApi.patchRun(data.project.id, activeRunId, { status: pauseOrResume }), pauseOrResume === 'waiting' ? '运行已转为等待' : '运行已恢复')}>{busyAction === 'run-state' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : pauseOrResume === 'waiting' ? <IconPlayerPause size={16} /> : <IconPlayerPlay size={16} />}{pauseOrResume === 'waiting' ? '暂停 Run' : '恢复 Run'}</Button></div>
        </section> : <EmptyState icon={<IconBolt size={22} />} title="还没有运行" description="创建 Run 后，这里会展示目标进度、预算与执行健康度。" action={<Button variant="primary" onClick={() => onNavigate('runs')}>创建第一个 Run</Button>} />}
        <div className="project-workspace__metric-strip"><article><span>工作项</span><strong>{data.workItems.length}</strong><small>{runningItems.length} 项正在执行</small></article><article><span>阻塞</span><strong className={blockedItems.length ? 'is-danger' : ''}>{blockedItems.length}</strong><small>{blockedItems.length ? '需要介入' : '当前无阻塞'}</small></article><article><span>协作事件</span><strong>{data.events.length}</strong><small>A2A 与系统事件</small></article><article><span>项目能力</span><strong>{data.capabilities.length}</strong><small>Skill 与 MCP</small></article></div>
        <div className="project-workspace__two-column"><section className="project-workspace__card"><header><div><span>执行态势</span><h3>正在推进的工作</h3></div><Button variant="ghost" onClick={() => onNavigate('work')}>查看全部 <IconArrowRight size={14} /></Button></header>{runningItems.length ? <div className="project-workspace__compact-list">{runningItems.slice(0, 5).map((item) => <article key={text(item, 'id', 'work_item_id')}><StatusPill status={text(item, 'status', 'state')} /><div><strong>{text(item, 'title', 'name')}</strong><small>{assigneeName(item)} · {dateLabel(item.updated_at)}</small></div><IconChevronRight size={15} /></article>)}</div> : <EmptyState icon={<IconArchive size={22} />} title="没有执行中的工作项" description="待工作项被 Agent 接受后，这里会实时更新。" />}</section><section className="project-workspace__card"><header><div><span>需要关注</span><h3>风险与阻塞</h3></div><ProjectCountBadge className="project-workspace__count">{blockedItems.length}</ProjectCountBadge></header>{blockedItems.length ? <div className="project-workspace__risk-list">{blockedItems.map((item) => <article key={text(item, 'id', 'work_item_id')}><IconAlertTriangle size={18} /><div><strong>{text(item, 'title', 'name')}</strong><p>{text(item, 'blocked_reason', 'error', 'description') || '工作项已停止推进，请查看运行记录。'}</p></div></article>)}</div> : <EmptyState icon={<IconCircleCheck size={22} />} title="没有已知阻塞" description="系统发现风险或 Agent 主动上报后会出现在这里。" />}</section></div>
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
    const columns = [{ key: 'todo', label: '待处理' }, { key: 'doing', label: '进行中' }, { key: 'review', label: '待评审' }, { key: 'done', label: '已完成' }];
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
    const memberOptions = members.map((member) => ({ value: text(member, 'agent_id'), label: text(member, 'name_snapshot', 'agent_name') }));
    const priorityOptions = [
        { value: 'low', label: '低' },
        { value: 'medium', label: '中' },
        { value: 'high', label: '高' },
        { value: 'urgent', label: '紧急' },
    ];
    const memberNameByAgentId = new Map(members.map((member) => [text(member, 'agent_id'), text(member, 'name_snapshot', 'agent_name', 'name')]));
    const itemAssignee = (item: RecordValue) => text(item, 'assignee_name', 'owner_name', 'agent_name')
        || memberNameByAgentId.get(text(item, 'assignee_agent_id'))
        || '未委派';
    const graphItems = items.map((item) => ({ ...item, assignee_name: itemAssignee(item) }));
    return <><SectionHeading eyebrow="OBJECTIVES / WORK GRAPH" title="目标与任务" description="从项目结果拆解到可验收工作项，依赖、委派与证据保持关联。" actions={<><ProjectSegmentedControl value={view} options={[{ value: 'graph', label: '依赖图' }, { value: 'board', label: '看板' }]} onChange={setView} ariaLabel="工作项视图" /><Button variant="primary" onClick={() => setShowCreate((value) => !value)}><IconPlus size={16} />创建工作项</Button></>} />
        {showCreate && <form className="project-workspace__action-panel" onSubmit={submit}><header><div><span>NEW WORK ITEM</span><h3>创建并指派工作项</h3></div><ProjectIconButton aria-label="关闭" onClick={() => setShowCreate(false)}><IconX size={17} /></ProjectIconButton></header><div className="project-workspace__form-grid"><ProjectField className="is-wide" label="标题" labelFor="project-work-title" required><TextInput id="project-work-title" value={title} onChange={(event) => setTitle(event.target.value)} required autoFocus /></ProjectField><ProjectField className="is-wide" label="说明" labelFor="project-work-description"><ProjectTextarea id="project-work-description" value={description} onChange={(event) => setDescription(event.target.value)} rows={3} /></ProjectField><ProjectField label="指派 Agent"><ProjectSelect value={assignee} options={memberOptions} onChange={setAssignee} ariaLabel="指派 Agent" placeholder="暂不指派" /></ProjectField><ProjectField label="优先级"><ProjectSelect value={priority} options={priorityOptions} onChange={setPriority} ariaLabel="工作项优先级" /></ProjectField><ProjectField className="is-wide" label="验收条件（每行一项）" labelFor="project-work-acceptance"><ProjectTextarea id="project-work-acceptance" value={acceptance} onChange={(event) => setAcceptance(event.target.value)} rows={3} /></ProjectField></div><footer><Button type="button" variant="secondary" onClick={() => setShowCreate(false)}>取消</Button><Button type="submit" variant="primary" disabled={!title.trim() || busyAction === 'create-work'}>{busyAction === 'create-work' && <IconLoader2 className="project-workspace__spinner" size={16} />}创建工作项</Button></footer></form>}
        {items.length ? view === 'graph' ? <div className="project-workspace__graph-panel"><WorkDependencyGraph items={graphItems} selectedWorkItemId={undefined} onWorkItemSelect={(id) => onSelect(id)} /><ProjectGraphLegend /></div> : <div className="project-workspace__kanban">{columns.map((column) => { const list = items.filter((item) => groupFor(item) === column.key); return <section key={column.key}><header><span><i />{column.label}</span><ProjectCountBadge>{list.length}</ProjectCountBadge></header><div>{list.map((item) => { const id = text(item, 'id', 'work_item_id'); return <Button variant="ghost" key={id} onClick={() => onSelect(id)}><div><code>{id}</code>{text(item, 'priority') && <em>{text(item, 'priority')}</em>}</div><h3>{text(item, 'title', 'name') || '未命名工作项'}</h3><p>{listText(item, 'acceptance_criteria') || text(item, 'description') || '尚未设置验收条件'}</p><footer><span>{itemAssignee(item)}</span><time>{dateLabel(item.updated_at)}</time></footer></Button>; })}{!list.length && <div className="project-workspace__column-empty">暂无工作项</div>}</div></section>; })}</div> : <EmptyState icon={<IconChecklist size={22} />} title="目标还没有拆成工作项" description="创建后会进入项目 API，并出现在 Agent 的执行队列。" action={<Button variant="primary" onClick={() => setShowCreate(true)}>创建第一个工作项</Button>} />}</>;
}

function MeshPanel({ projectId, members, events, groupSession, onOpenGroup, onOpenSession, runAction, busyAction }: { projectId: string; members: RecordValue[]; events: RecordValue[]; groupSession: RecordValue | null; onOpenGroup: () => void; onOpenSession: OpenSession; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const [source, setSource] = useState('');
    const [target, setTarget] = useState('');
    const [kind, setKind] = useState('wake');
    const [message, setMessage] = useState('');
    const submit = (event: FormEvent) => {
        event.preventDefault();
        if (!source || !target || source === target || !message.trim()) return;
        void runAction('a2a', () => projectsApi.sendA2A(projectId, { from_agent_id: source, to_agent_id: target, mode: kind, message: message.trim() }), 'A2A 消息已进入目标 Agent 的唤醒队列').then((succeeded) => { if (succeeded) setMessage(''); });
    };
    const memberOptions = members.map((member) => ({
        value: text(member, 'agent_id', 'id', 'member_id'),
        label: text(member, 'agent_name', 'name_snapshot', 'name'),
    }));
    const actionOptions = [
        { value: 'wake', label: '唤醒' },
        { value: 'consult', label: '咨询' },
        { value: 'delegate', label: '委派' },
        { value: 'review', label: '请求评审' },
    ];
    return <><SectionHeading eyebrow="A2A DIRECT MESH" title="Agent 协作网络" description="成员可直接唤醒、咨询、委派或请求评审；Leader 不再是消息中转站。" actions={<Button variant="secondary" onClick={onOpenGroup} disabled={!groupSession}><IconMessageCircle size={16} />进入项目群聊</Button>} />
        <div className="project-workspace__mesh-layout"><section className="project-workspace__mesh-graph">{members.length ? <><A2AMeshGraph members={members} events={events} selectedAgentId={target} onAgentSelect={(id) => setTarget(id)} /><ProjectGraphLegend /></> : <EmptyState icon={<IconUsers size={22} />} title="项目还没有 Agent 成员" description="添加成员后，A2A 直连拓扑会显示在这里。" />}</section>
            <section className="project-workspace__mesh-composer"><header><IconBolt size={18} /><div><strong>直接发起 A2A</strong><small>发送成功仅代表事件已排队，实际执行结果会写入事件流。</small></div></header><form onSubmit={submit}><div className="project-workspace__field-row"><ProjectField label="发起方"><ProjectSelect value={source} options={memberOptions} onChange={setSource} ariaLabel="A2A 发起方" placeholder="选择 Agent" /></ProjectField><ProjectField label="接收方"><ProjectSelect value={target} options={memberOptions.filter((option) => option.value !== source)} onChange={setTarget} ariaLabel="A2A 接收方" placeholder="选择 Agent" /></ProjectField></div><ProjectField label="动作"><ProjectSelect value={kind} options={actionOptions} onChange={setKind} ariaLabel="A2A 动作" /></ProjectField><ProjectField label="消息与期望产出" labelFor="project-a2a-message" required><ProjectTextarea id="project-a2a-message" value={message} onChange={(e) => setMessage(e.target.value)} placeholder="说明上下文、希望对方完成什么，以及返回哪些证据…" rows={5} required /></ProjectField><Button variant="primary" type="submit" disabled={!source || !target || source === target || !message.trim() || busyAction === 'a2a'}>{busyAction === 'a2a' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconSend size={16} />}发送并唤醒</Button></form></section></div>
        <section className="project-workspace__card project-workspace__timeline"><header><div><span>实时事件</span><h3>A2A 协作流</h3></div></header>{events.length ? events.slice(0, 30).map((event) => <article key={text(event, 'id', 'event_id') || `${text(event, 'created_at')}-${text(event, 'type')}`}><i /><span>{(text(event, 'actor_name', 'from_agent_name') || '系统').slice(0, 1)}</span><div><strong>{text(event, 'actor_name', 'from_agent_name') || '系统'} <em>{text(event, 'type', 'event_type')}</em> {text(event, 'target_name', 'to_agent_name')}</strong><p>{text(event, 'message', 'summary', 'detail') || '事件没有附加说明'}</p><small>{dateLabel(event.created_at)}</small><SessionButton source={event} onOpen={onOpenSession} /></div></article>) : <EmptyState title="还没有协作事件" description="成员直接唤醒或委派后，事件会按因果顺序出现在这里。" />}</section>
    </>;
}

function WorkItemDetail({ projectId, items, members, selectedId, onSelect, onOpenSession, runAction, busyAction }: { projectId: string; items: RecordValue[]; members: RecordValue[]; selectedId: string; onSelect: (id: string) => void; onOpenSession: OpenSession; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const item = items.find((entry) => text(entry, 'id', 'work_item_id') === selectedId) || items[0];
    const [assignee, setAssignee] = useState('');
    const [status, setStatus] = useState('todo');
    const [priority, setPriority] = useState('medium');
    useEffect(() => { setAssignee(text(item || {}, 'assignee_agent_id')); setStatus(text(item || {}, 'status') || 'todo'); setPriority(text(item || {}, 'priority') || 'medium'); }, [item]);
    const itemId = text(item || {}, 'id', 'work_item_id');
    const save = () => void runAction('save-work', () => projectsApi.patchWorkItem(projectId, itemId, { assignee_agent_id: assignee || null, status, priority }), '工作项状态与指派已保存');
    const startRun = () => void runAction('run-work', () => projectsApi.createRun(projectId, { work_item_id: itemId, agent_id: assignee || undefined, input: { objective: text(item || {}, 'title') } }), '工作项运行已创建');
    const workItemOptions = items.map((entry) => ({ value: text(entry, 'id', 'work_item_id'), label: `${text(entry, 'id', 'work_item_id')} · ${text(entry, 'title', 'name')}` }));
    const memberOptions = members.map((member) => ({ value: text(member, 'agent_id'), label: text(member, 'name_snapshot', 'agent_name') }));
    const statusOptions = [{ value: 'backlog', label: '待规划' }, { value: 'todo', label: '待处理' }, { value: 'in_progress', label: '进行中' }, { value: 'review', label: '待评审' }, { value: 'blocked', label: '阻塞' }, { value: 'done', label: '已完成' }];
    const priorityOptions = [{ value: 'low', label: '低' }, { value: 'medium', label: '中' }, { value: 'high', label: '高' }, { value: 'urgent', label: '紧急' }];
    return <><SectionHeading eyebrow="WORK ITEM / EVIDENCE" title="工作项详情" description="把目标上下文、执行过程、对话与交付证据收敛到一个可追溯对象。" actions={items.length ? <ProjectSelect ariaLabel="选择工作项" value={itemId} options={workItemOptions} onChange={onSelect} /> : undefined} />{item ? <div className="project-workspace__detail-grid"><section className="project-workspace__card project-workspace__detail-main"><header><div><span>{itemId}</span><h3>{text(item, 'title', 'name')}</h3></div><StatusPill status={text(item, 'status', 'state')} /></header><div className="project-workspace__prose"><h4>为什么要做</h4><p>{text(item, 'description', 'context') || '未提供工作项背景。'}</p><h4>验收条件</h4><p>{listText(item, 'acceptance_criteria') || text(item, 'acceptance') || '尚未设置验收条件。'}</p><h4>执行结果</h4><p>{text(item, 'result', 'output_summary') || 'Agent 尚未提交结果。'}</p></div></section><aside><section className="project-workspace__card project-workspace__control-card"><header><div><span>执行控制</span><h3>指派与状态推进</h3></div></header><ProjectField label="负责人"><ProjectSelect value={assignee} options={memberOptions} onChange={setAssignee} ariaLabel="工作项负责人" placeholder="未指派" /></ProjectField><ProjectField label="状态"><ProjectSelect value={status} options={statusOptions} onChange={setStatus} ariaLabel="工作项状态" /></ProjectField><ProjectField label="优先级"><ProjectSelect value={priority} options={priorityOptions} onChange={setPriority} ariaLabel="工作项优先级" /></ProjectField><footer><Button variant="secondary" onClick={startRun} disabled={!assignee || busyAction === 'run-work'}>{busyAction === 'run-work' && <IconLoader2 className="project-workspace__spinner" size={16} />}创建 Run</Button><Button variant="primary" onClick={save} disabled={busyAction === 'save-work'}>{busyAction === 'save-work' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconDeviceFloppy size={16} />}保存</Button></footer></section><section className="project-workspace__card"><header><div><span>追溯锚点</span><h3>关联证据</h3></div></header><dl className="project-workspace__definition-list"><div><dt>依赖</dt><dd>{listText(item, 'dependency_ids') || '无'}</dd></div><div><dt>会话</dt><dd>{sessionIdOf(item) || '—'}<SessionButton source={item} onOpen={onOpenSession} /></dd></div><div><dt>Commit</dt><dd><code>{text(item, 'commit_hash') || '—'}</code></dd></div><div><dt>更新时间</dt><dd>{dateLabel(item.updated_at)}</dd></div></dl></section></aside></div> : <EmptyState icon={<IconChecklist size={22} />} title="没有可查看的工作项" description="项目创建工作项后，可在这里核对上下文、对话、Diff 与验收证据。" />}</>;
}

function FilesPanel({ projectId, files, commits, runAction, busyAction }: { projectId: string; files: RecordValue[]; commits: RecordValue[]; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const [selected, setSelected] = useState(() => text(files[0] || {}, 'path', 'id'));
    const [path, setPath] = useState('');
    const [content, setContent] = useState('');
    const [milestoneMessage, setMilestoneMessage] = useState('');
    const file = selected ? files.find((entry) => text(entry, 'path', 'id') === selected) : undefined;
    const contentIsTruncated = Boolean(file) && num(file || {}, 'size') > text(file || {}, 'preview', 'content_preview').length;
    useEffect(() => { if (file) { setPath(text(file, 'path', 'id')); setContent(text(file, 'content', 'preview', 'content_preview')); } }, [file]);
    const save = (event: FormEvent) => { event.preventDefault(); void runAction('save-file', () => projectsApi.writeFile(projectId, { path: path.trim(), content }), '文件已原子保存并创建 Git 提交'); };
    const milestone = (event: FormEvent) => { event.preventDefault(); void runAction('milestone', () => projectsApi.commitFiles(projectId, { message: milestoneMessage.trim(), milestone: true }), '项目级里程碑已创建并写入审计').then((ok) => { if (ok) setMilestoneMessage(''); }); };
    return <><SectionHeading eyebrow="FILES / DELIVERABLES" title="项目文件与交付" description="保存文件会原子写入并创建普通 Git commit；里程碑单独创建可恢复锚点。" actions={<Button variant="secondary" onClick={() => { setSelected(''); setPath(''); setContent(''); }}><IconPlus size={16} />新建文件</Button>} /><div className="project-workspace__files"><aside><header><IconGitBranch size={15} /><span>{text(commits[0] || {}, 'branch') || '项目工作树'}</span></header>{files.map((entry) => { const id = text(entry, 'path', 'id'); return <Button variant="ghost" className={text(file || {}, 'path', 'id') === id && selected !== '' ? 'is-active' : ''} key={id} onClick={() => setSelected(id)}><IconFile size={15} /><span>{text(entry, 'name') || id}</span></Button>; })}{!files.length && <div className="project-workspace__column-empty">暂无文件</div>}</aside><section className="project-workspace__card project-workspace__file-editor"><header><div><span>{selected ? 'EDIT FILE' : 'NEW FILE'}</span><h3>{path || '创建项目文件'}</h3></div><code>{text(file || {}, 'commit_hash', 'commit') || '保存时提交'}</code></header><form onSubmit={save}><ProjectField label="项目内路径" labelFor="project-file-path" required><TextInput id="project-file-path" value={path} onChange={(event) => setPath(event.target.value)} placeholder="docs/deliverable.md" required /></ProjectField><ProjectField label="文件内容" labelFor="project-file-content" error={contentIsTruncated ? '文件超过在线预览上限，为避免截断覆盖，请由 Agent 工作区修改后再提交。' : undefined} required><ProjectTextarea id="project-file-content" value={content} onChange={(event) => setContent(event.target.value)} rows={18} spellCheck={false} required disabled={contentIsTruncated} /></ProjectField><footer><Button type="submit" variant="primary" disabled={!path.trim() || contentIsTruncated || busyAction === 'save-file'}>{busyAction === 'save-file' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconDeviceFloppy size={16} />}保存并提交</Button></footer></form></section></div><form className="project-workspace__inline-create project-workspace__milestone" onSubmit={milestone}><ProjectField label="里程碑说明" labelFor="project-milestone-message" required><TextInput id="project-milestone-message" value={milestoneMessage} onChange={(event) => setMilestoneMessage(event.target.value)} placeholder="例如：M1 · 需求与技术方案冻结" required /></ProjectField><Button variant="secondary" type="submit" disabled={!milestoneMessage.trim() || busyAction === 'milestone'}>{busyAction === 'milestone' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconFlag size={16} />}创建里程碑</Button></form></>;
}

function RunsPanel({ projectId, runs, onOpenSession, runAction, busyAction }: { projectId: string; runs: RecordValue[]; onOpenSession: OpenSession; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const [objective, setObjective] = useState('');
    const create = (event: FormEvent) => { event.preventDefault(); void runAction('new-run', () => projectsApi.createRun(projectId, { input: { objective: objective.trim() } }), '新运行已创建').then((ok) => { if (ok) setObjective(''); }); };
    return <><SectionHeading eyebrow="RUN CONTROL" title="运行控制" description="每次运行冻结目标、成员与能力快照；重试会创建新 Run，不覆盖历史。" />
        <form className="project-workspace__inline-create" onSubmit={create}><ProjectField label="本次运行输入" labelFor="project-run-objective" required><TextInput id="project-run-objective" value={objective} onChange={(event) => setObjective(event.target.value)} placeholder="说明本次 Run 要推进的目标" required /></ProjectField><Button variant="primary" type="submit" disabled={!objective.trim() || busyAction === 'new-run'}>{busyAction === 'new-run' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconPlayerPlay size={16} />}创建 Run</Button></form>
        {runs.length ? <div className="project-workspace__run-list">{runs.map((run) => { const runId = text(run, 'id', 'run_id'); const runStatus = text(run, 'status'); const input = obj(run.input); const nextStatus = runStatus === 'running' ? 'waiting' : runStatus === 'waiting' || runStatus === 'queued' ? 'running' : ''; return <article key={runId}><div className="project-workspace__run-icon"><IconBolt size={18} /></div><div><div><StatusPill status={runStatus} /><code>{runId}</code></div><h3>{text(input, 'objective') || text(run, 'name', 'objective') || '项目运行'}</h3><p>{dateLabel(run.started_at || run.created_at)} · {text(run, 'trigger_type') || 'manual'}</p></div><div className="project-workspace__run-controls"><SessionButton source={run} onOpen={onOpenSession} />{nextStatus && <Button variant="secondary" disabled={busyAction === `run-${runId}`} onClick={() => void runAction(`run-${runId}`, () => projectsApi.patchRun(projectId, runId, { status: nextStatus }), nextStatus === 'waiting' ? 'Run 已转为等待' : 'Run 已恢复')}>{busyAction === `run-${runId}` ? <IconLoader2 className="project-workspace__spinner" size={15} /> : nextStatus === 'waiting' ? <IconPlayerPause size={15} /> : <IconPlayerPlay size={15} />}{nextStatus === 'waiting' ? '暂停' : '继续'}</Button>}{!['succeeded', 'failed', 'cancelled'].includes(runStatus) && <Button variant="ghost" disabled={busyAction === `finish-${runId}`} onClick={() => void runAction(`finish-${runId}`, () => projectsApi.patchRun(projectId, runId, { status: 'succeeded' }), 'Run 已标记完成')}><IconCircleCheck size={15} />完成</Button>}</div></article>; })}</div> : <EmptyState title="还没有运行记录" description="输入目标并创建首个 Run；创建后会立即持久化并冻结快照。" />}</>;
}

function MembersPanel({ projectId, members, runs, selectedId, onSelect, runAction, busyAction }: { projectId: string; members: RecordValue[]; runs: RecordValue[]; selectedId: string; onSelect: (id: string) => void; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const member = members.find((entry) => text(entry, 'id', 'member_id', 'agent_id') === selectedId) || members[0];
    const [config, setConfig] = useState('{}');
    const [configError, setConfigError] = useState('');
    useEffect(() => { setConfig(JSON.stringify(obj(member?.config_snapshot), null, 2)); setConfigError(''); }, [member]);
    const memberId = text(member || {}, 'id', 'member_id');
    const agentId = text(member || {}, 'agent_id');
    const saveSnapshot = () => {
        let parsed: RecordValue;
        try { parsed = JSON.parse(config) as RecordValue; setConfigError(''); } catch { setConfigError('配置必须是有效 JSON'); return; }
        void runAction('save-member', () => projectsApi.patchMember(projectId, memberId, { config_snapshot: parsed }), '项目成员快照已更新；源 Agent 未改变');
    };
    return <><SectionHeading eyebrow="MEMBERS / SNAPSHOTS" title="成员与三层快照" description="源 Agent 保持稳定；项目快照承接项目内配置，每个 Run 再冻结一次执行视图。" />{members.length ? <><div className="project-workspace__member-layout"><div className="project-workspace__member-list">{members.map((entry) => { const id = text(entry, 'id', 'member_id', 'agent_id'); return <Button variant="ghost" key={id} className={text(member || {}, 'id', 'member_id', 'agent_id') === id ? 'is-active' : ''} onClick={() => onSelect(id)}><span>{(text(entry, 'agent_name', 'name_snapshot', 'name') || 'A').slice(0, 1)}</span><div><strong>{text(entry, 'agent_name', 'name_snapshot', 'name')}</strong><small>{bool(entry, 'is_leader') ? 'Leader · ' : ''}{text(entry, 'role_snapshot', 'role') || '项目成员'}</small></div><IconChevronRight size={15} /></Button>; })}</div><div className="project-workspace__snapshot-graph"><SnapshotLineageGraph member={member || null} runs={runs} /><ProjectGraphLegend /></div></div><section className="project-workspace__action-panel project-workspace__member-editor"><header><div><span>PROJECT SNAPSHOT EDITOR</span><h3>编辑 {text(member || {}, 'name_snapshot')} 的项目快照</h3></div>{!bool(member || {}, 'is_leader') && <Button variant="secondary" disabled={busyAction === 'leader'} onClick={() => void runAction('leader', () => projectsApi.setLeader(projectId, agentId), '项目 Leader 已切换')}><IconFlag size={15} />设为 Leader</Button>}</header><ProjectField label="项目内配置 JSON" labelFor="project-member-config" error={configError || undefined}><ProjectTextarea id="project-member-config" value={config} onChange={(event) => setConfig(event.target.value)} rows={7} spellCheck={false} /></ProjectField><footer><ToggleSwitch checked={member?.is_enabled !== false} ariaLabel="启用或停用项目成员" disabled={busyAction === 'toggle-member'} onChange={(checked) => void runAction('toggle-member', () => projectsApi.patchMember(projectId, memberId, { is_enabled: checked }), checked ? '成员已在项目内启用' : '成员已在项目内停用')} /><span className="project-workspace__switch-copy">仅影响当前项目</span><Button variant="primary" onClick={saveSnapshot} disabled={busyAction === 'save-member'}>{busyAction === 'save-member' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconDeviceFloppy size={16} />}保存快照</Button></footer></section></> : <EmptyState icon={<IconUsers size={22} />} title="项目还没有成员" description="添加 Agent 并指定 Leader 后，系统会创建项目隔离快照。" />}</>;
}

function CapabilitiesPanel({ projectId, capabilities, runAction, busyAction }: { projectId: string; capabilities: RecordValue[]; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
    const [filter, setFilter] = useState('all');
    const visible = capabilities.filter((cap) => {
        if (filter === 'all') return true;
        if (['skill', 'mcp'].includes(filter)) return text(cap, 'capability_type', 'kind', 'type') === filter;
        const inherited = Boolean(text(cap, 'inherited_from_agent_id')) || ['agent', 'inherited'].includes(text(cap, 'source'));
        return filter === 'agent' ? inherited : !inherited;
    });
    return <><SectionHeading eyebrow="CAPABILITY CONTROL" title="项目能力中心" description="项目共享 Skill/MCP 对成员可见；Agent 带入能力可逐项关闭，均不影响源 Agent。" />
        <ProjectSegmentedControl className="project-workspace__filters" value={filter} options={[{ value: 'all', label: '全部' }, { value: 'skill', label: 'Skill' }, { value: 'mcp', label: 'MCP' }, { value: 'project', label: '项目共享' }, { value: 'agent', label: 'Agent 带入' }]} onChange={setFilter} ariaLabel="能力筛选" />
        {visible.length ? <div className="project-workspace__cap-grid">{visible.map((cap) => { const id = text(cap, 'id', 'binding_id', 'capability_id'); const capabilityType = text(cap, 'capability_type', 'kind', 'type'); const enabled = cap.is_enabled !== false; return <article key={id}><header><span className={`is-${capabilityType || 'skill'}`}>{capabilityType === 'mcp' ? <IconCodeDots size={17} /> : <IconTool size={17} />}</span><div><strong>{text(cap, 'name', 'capability_name')}</strong><small>{text(cap, 'version') || capabilityType.toUpperCase() || '能力'} · {text(cap, 'source') === 'agent' ? `由 ${text(cap, 'inherited_from_agent_id') || 'Agent'} 带入` : '项目共享'}</small></div><ToggleSwitch checked={enabled} ariaLabel={`${enabled ? '关闭' : '启用'} ${text(cap, 'name', 'capability_name')}`} disabled={busyAction === `cap-${id}`} onChange={(checked) => void runAction(`cap-${id}`, () => projectsApi.patchCapability(projectId, id, { is_enabled: checked }), checked ? '能力已在项目内启用' : '能力已在项目内关闭；源 Agent 不受影响')} /></header><p>{text(cap, 'description') || '没有能力说明。'}</p><footer><span>{Object.keys(obj(cap.scope)).length ? JSON.stringify(cap.scope) : '未配置权限范围'}</span>{text(cap, 'risk_level') && <em>{text(cap, 'risk_level')} risk</em>}</footer></article>; })}</div> : <EmptyState icon={<IconTool size={22} />} title="没有符合条件的能力" description="添加项目共享 Skill/MCP，或启用成员带入的能力后会出现在这里。" />}</>;
}

function CapabilityMatrix({ members, capabilities }: { members: RecordValue[]; capabilities: RecordValue[] }) {
    return <><SectionHeading eyebrow="EFFECTIVE CAPABILITIES" title="能力矩阵" description="最终能力 = 项目分配 ∩ 成员启用 ∩ 角色策略 ∩ 安全边界；每次解析均写入审计。" />{members.length && capabilities.length ? <div className="project-workspace__matrix-wrap"><ProjectDataTable><ProjectDataTableHead><ProjectDataTableRow><ProjectDataTableHeader>能力</ProjectDataTableHeader><ProjectDataTableHeader>来源</ProjectDataTableHeader>{members.map((member) => <ProjectDataTableHeader key={text(member, 'agent_id', 'id', 'member_id')}>{text(member, 'agent_name', 'name_snapshot', 'name')}</ProjectDataTableHeader>)}</ProjectDataTableRow></ProjectDataTableHead><ProjectDataTableBody>{capabilities.map((cap) => { const assignments = obj(cap.assignments); const inheritedAgentId = text(cap, 'inherited_from_agent_id'); const inherited = Boolean(inheritedAgentId) || ['agent', 'inherited'].includes(text(cap, 'source')); return <ProjectDataTableRow key={text(cap, 'id', 'binding_id', 'capability_id')}><ProjectDataTableCell><strong>{text(cap, 'name', 'capability_name')}</strong><small>{text(cap, 'capability_type', 'kind', 'type')}</small></ProjectDataTableCell><ProjectDataTableCell>{inherited ? 'Agent 带入' : '项目共享'}</ProjectDataTableCell>{members.map((member) => { const id = text(member, 'agent_id', 'id', 'member_id'); const explicitlyAssigned = Object.prototype.hasOwnProperty.call(assignments, id); const enabled = cap.is_enabled !== false; const resolved: 'yes' | 'no' | 'unknown' = !enabled ? 'no' : inheritedAgentId ? (inheritedAgentId === id ? 'yes' : 'no') : explicitlyAssigned ? (assignments[id] === false ? 'no' : 'yes') : 'unknown'; return <ProjectDataTableCell key={id}><span className={`project-workspace__matrix-${resolved}`}>{resolved === 'yes' ? <IconCircleCheck size={17} /> : resolved === 'no' ? <IconX size={16} /> : <IconClock size={16} />}<small>{resolved === 'yes' ? '可用' : resolved === 'no' ? '不可用' : '按策略解析'}</small></span></ProjectDataTableCell>; })}</ProjectDataTableRow>; })}</ProjectDataTableBody></ProjectDataTable></div> : <EmptyState icon={<IconCodeDots size={22} />} title="能力矩阵尚不可计算" description="至少需要一个项目成员和一个能力绑定。" />}</>;
}

function PoliciesPanel({ projectId, policies, runAction, busyAction }: { projectId: string; policies: RecordValue | null; runAction: (key: string, action: () => Promise<unknown>, success: string) => Promise<boolean>; busyAction: string }) {
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
    return <><SectionHeading eyebrow="POLICIES / SAFETY" title="运行与安全策略" description="模型、预算、凭证和审批规则在项目范围生效；保存后由服务端持久化。" actions={<Button variant="primary" onClick={save} disabled={busyAction === 'save-policies'}>{busyAction === 'save-policies' ? <IconLoader2 className="project-workspace__spinner" size={16} /> : <IconDeviceFloppy size={16} />}保存策略</Button>} /><div className="project-workspace__settings-grid"><ProjectField label="默认模型" labelFor="project-policy-model"><TextInput id="project-policy-model" value={model} onChange={(event) => setModel(event.target.value)} placeholder="default" /></ProjectField><ProjectField label="月度预算" labelFor="project-policy-budget"><TextInput id="project-policy-budget" type="number" min="0" step="1" value={budget} onChange={(event) => setBudget(event.target.value)} /></ProjectField><ProjectField label="审批策略"><ProjectSelect value={approval} options={approvalOptions} onChange={setApproval} ariaLabel="审批策略" /></ProjectField><ProjectField label="最大并行 Run" labelFor="project-policy-parallel"><TextInput id="project-policy-parallel" type="number" min="1" max="32" value={parallel} onChange={(event) => setParallel(event.target.value)} /></ProjectField><ProjectField label="单次 Run 最大 A2A 唤醒" labelFor="project-policy-a2a-limit"><TextInput id="project-policy-a2a-limit" type="number" min="1" max="100" value={a2aLimit} onChange={(event) => setA2aLimit(event.target.value)} /></ProjectField><div className="project-workspace__switch-setting"><span><strong>循环保护</strong><small>阻止重复唤醒与无界委派</small></span><ToggleSwitch checked={loopGuard} onChange={setLoopGuard} ariaLabel="循环保护" /></div></div><div className="project-workspace__policy-grid">{[
        ['模型策略', model, '成员新运行默认使用的模型策略。', <IconSparkles size={18} />],
        ['预算边界', budget ? `每月 ${budget}` : '未设置', '到达阈值时阻止创建新 Run。', <IconActivityHeartbeat size={18} />],
        ['审批规则', approval, '高风险写入和外部动作按此审批。', <IconShieldCheck size={18} />],
        ['循环保护', loopGuard ? '已启用' : '已关闭', `最大 A2A 唤醒 ${a2aLimit} 次。`, <IconRefresh size={18} />],
    ].map(([title, value, desc, icon]) => <article key={String(title)}><span>{icon}</span><div><strong>{title}</strong><p>{desc}</p><code>{String(value)}</code></div></article>)}</div></>;
}

function GitPanel({ commits, events, selected, onSelect, onDialog, onOpenSession }: { commits: RecordValue[]; events: RecordValue[]; selected: RecordValue | null; onSelect: (commit: RecordValue) => void; onDialog: (mode: 'restore' | 'branch') => void; onOpenSession: OpenSession }) {
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
    return <><SectionHeading eyebrow="GIT / TRACEABILITY" title="Git 历史与恢复" description="使用标准 Git 日志追溯项目产物；恢复会创建新提交，禁止 reset --hard 与 force push。" />{commits.length ? <div className="project-workspace__git-layout"><section className="project-workspace__card project-workspace__git-log" aria-label="Git 提交历史"><header><div><span>COMMIT LOG</span><h3>提交历史</h3></div><ProjectCountBadge>{commits.length}</ProjectCountBadge></header><div className="project-workspace__git-log-head" aria-hidden="true"><span>Commit</span><span>说明</span><span>作者</span><span>时间</span><span>引用</span></div><ol>{commits.map((commit, index) => { const hash = text(commit, 'commit', 'hash', 'commit_hash', 'id'); const refs = refsForCommit(commit, index); const milestone = isMilestone(commit); return <li key={hash}><Button type="button" variant="ghost" className={hash === selectedId ? 'is-active' : ''} aria-pressed={hash === selectedId} onClick={() => onSelect(commit)}><code title={hash}>{text(commit, 'short_commit') || hash.slice(0, 12)}</code><span className="project-workspace__git-log-message"><strong>{text(commit, 'message', 'subject', 'title') || '未命名提交'}</strong><small>{hash}</small></span><span>{text(commit, 'author', 'author_name', 'actor_name') || 'Clawith'}</span><time dateTime={text(commit, 'created_at', 'timestamp')}>{dateLabel(commit.created_at || commit.timestamp)}</time><span className="project-workspace__git-refs">{refs.map((ref) => <ProjectStatusBadge key={ref} tone={ref === 'HEAD' ? 'success' : 'info'}>{ref}</ProjectStatusBadge>)}{milestone && <ProjectStatusBadge tone="warning">里程碑</ProjectStatusBadge>}{!refs.length && !milestone && <small>—</small>}</span></Button></li>; })}</ol></section><aside className="project-workspace__card project-workspace__commit-detail"><header><div><span>提交详情</span><h3>{text(selected || {}, 'message', 'subject', 'title') || '未命名提交'}</h3></div></header><div className="project-workspace__repro"><IconCircleCheck size={18} /><span><strong>历史保留</strong><small>恢复操作不会删除现有提交</small></span></div><dl className="project-workspace__definition-list"><div><dt>Commit</dt><dd><code>{selectedId || '—'}</code></dd></div><div><dt>作者</dt><dd>{text(selected || {}, 'author', 'author_name', 'actor_name') || 'Clawith'}</dd></div><div><dt>时间</dt><dd>{dateLabel(selected?.created_at || selected?.timestamp)}</dd></div><div><dt>引用</dt><dd className="project-workspace__git-detail-refs">{selectedRefs.map((ref) => <ProjectStatusBadge key={ref} tone={ref === 'HEAD' ? 'success' : 'info'}>{ref}</ProjectStatusBadge>)}{selected && isMilestone(selected) && <ProjectStatusBadge tone="warning">里程碑</ProjectStatusBadge>}{!selectedRefs.length && !(selected && isMilestone(selected)) && '—'}</dd></div><div><dt>关联 Run</dt><dd>{traceValue(traceRecords(linkedEvent || selected || {}), 'run_id') || '—'}</dd></div><div><dt>工作项</dt><dd>{traceValue(traceRecords(linkedEvent || selected || {}), 'work_item_id') || '—'}</dd></div><div><dt>会话</dt><dd>{sessionIdOf(sessionSource) || '—'}<SessionButton source={sessionSource} onOpen={onOpenSession} /></dd></div><div><dt>变更文件</dt><dd>{text(selected || {}, 'file_count', 'files_changed') || '—'}</dd></div></dl><div className="project-workspace__git-actions"><Button variant="secondary" onClick={() => onDialog('branch')}><IconGitBranch size={16} />从此创建分支</Button><Button variant="danger" onClick={() => onDialog('restore')}><IconRestore size={16} />还原为新提交</Button></div><p><IconLock size={14} /> 保护规则：禁止 force push 与 reset --hard</p></aside></div> : <EmptyState icon={<IconBrandGit size={22} />} title="还没有提交历史" description="项目产物提交后，可在这里查看 Commit、作者、时间、分支、里程碑与关联会话。" />}</>;
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
