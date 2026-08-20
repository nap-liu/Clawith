import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import {
    IconArchive,
    IconArrowRight,
    IconFolder,
    IconLayoutGrid,
    IconList,
    IconLock,
    IconPlus,
    IconRefresh,
    IconTemplate,
    IconUsers,
} from '@tabler/icons-react';
import { projectsApi } from '../../services/projects';
import type { ProjectScope, ProjectStatus, ProjectSummary } from './types';
import {
    Button,
    ProjectCard,
    ProjectEmptyState,
    ProjectProgressBar,
    ProjectSegmentedControl,
    ProjectSelect,
    ProjectStatusBadge,
    SearchInput,
} from './components/ProjectUI';
import './projectPortfolio.css';

const scopeTabs: Array<{ value: ProjectScope; label: string }> = [
    { value: 'mine', label: '我的项目' },
    { value: 'shared', label: '与我共享' },
    { value: 'running', label: '运行中' },
    { value: 'archived', label: '已归档' },
];

const statusLabels: Record<ProjectStatus, string> = {
    initializing: '初始化中',
    running: '进行中',
    waiting: '等待确认',
    paused: '已暂停',
    completed: '已完成',
    archived: '已归档',
    failed: '运行异常',
};

const statusTones: Record<ProjectStatus, 'neutral' | 'success' | 'warning' | 'error'> = {
    initializing: 'neutral',
    running: 'success',
    waiting: 'warning',
    paused: 'neutral',
    completed: 'success',
    archived: 'neutral',
    failed: 'error',
};

function timeAgo(value: string) {
    const delta = Date.now() - new Date(value).getTime();
    if (!Number.isFinite(delta) || delta < 0) return '刚刚';
    if (delta < 60_000) return '刚刚';
    if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`;
    if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`;
    return new Date(value).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
}

function AgentStack({ project }: { project: ProjectSummary }) {
    const members = project.members || [];
    return (
        <div className="pm-agent-stack" aria-label={`${members.length} 个 Agent`}>
            {members.slice(0, 4).map(member => (
                member.avatar_url
                    ? <img key={member.agent_id} src={member.avatar_url} alt={member.agent_name} />
                    : <span key={member.agent_id} title={member.agent_name}>{member.agent_name.slice(0, 1)}</span>
            ))}
            {members.length > 4 && <i>+{members.length - 4}</i>}
        </div>
    );
}

export default function ProjectPortfolioPage() {
    const navigate = useNavigate();
    const [scope, setScope] = useState<ProjectScope>('mine');
    const [queryDraft, setQueryDraft] = useState('');
    const [query, setQuery] = useState('');
    const [status, setStatus] = useState('all');
    const [view, setView] = useState<'list' | 'grid'>('list');

    const projectQuery = useQuery({
        queryKey: ['projects', 'portfolio', scope, query, status],
        queryFn: () => projectsApi.list({ scope, query, status }),
    });
    const projects = projectQuery.data?.items ?? [];
    const overview = useMemo(() => ({
        running: projects.filter(project => project.status === 'running').length,
        activeAgents: projects.reduce((sum, project) => sum + (project.active_agent_count ?? 0), 0),
        waiting: projects.filter(project => project.status === 'waiting').length,
        averageProgress: projects.length
            ? Math.round(projects.reduce((sum, project) => sum + project.progress, 0) / projects.length)
            : 0,
    }), [projects]);

    const search = () => setQuery(queryDraft.trim());

    return (
        <main className="pm-page pm-portfolio">
            <header className="pm-page-header">
                <div>
                    <span className="pm-eyebrow">PROJECT PORTFOLIO</span>
                    <h1>项目资产库</h1>
                    <p>管理由多个 Agent 共同负责的目标、运行和可追溯产出。</p>
                </div>
                <div className="pm-header-actions">
                    <Button variant="secondary" className="pm-button pm-button-secondary" onClick={() => navigate('/projects/templates')}>
                        <IconTemplate size={17} />模板市场
                    </Button>
                    <Button variant="primary" className="pm-button pm-button-primary" onClick={() => navigate('/projects/new')}>
                        <IconPlus size={17} />创建项目
                    </Button>
                </div>
            </header>

            <section className="pm-overview" aria-label="项目概览">
                <div className="pm-overview-signal">
                    <span className="pm-live-dot" />
                    <div><strong>{overview.running} 个项目正在运行</strong><small>{overview.activeAgents} 个 Agent 正在执行项目任务</small></div>
                </div>
                <dl>
                    <div><dt>等待我</dt><dd>{overview.waiting}</dd></div>
                    <div><dt>当前项目</dt><dd>{projectQuery.data?.total ?? 0}</dd></div>
                    <div><dt>平均进度</dt><dd>{overview.averageProgress}%</dd></div>
                </dl>
            </section>

            <nav className="pm-tabs" aria-label="项目范围">
                {scopeTabs.map(tab => (
                    <Button
                        variant="ghost"
                        key={tab.value}
                        className={scope === tab.value ? 'is-active' : ''}
                        onClick={() => setScope(tab.value)}
                    >
                        {tab.value === 'archived' && <IconArchive size={15} />}
                        {tab.label}
                        {scope === tab.value && projectQuery.data && <em>{projectQuery.data.total}</em>}
                    </Button>
                ))}
            </nav>

            <form className="pm-toolbar" onSubmit={event => { event.preventDefault(); search(); }}>
                <SearchInput className="pm-search" value={queryDraft} onChange={event => setQueryDraft(event.target.value)} placeholder="搜索项目、目标或 Leader" aria-label="搜索项目" />
                <ProjectSelect
                    value={status}
                    onChange={setStatus}
                    ariaLabel="项目状态"
                    options={[
                        { value: 'all', label: '全部状态' },
                        { value: 'running', label: '进行中' },
                        { value: 'waiting', label: '等待确认' },
                        { value: 'paused', label: '已暂停' },
                        { value: 'completed', label: '已完成' },
                        { value: 'failed', label: '运行异常' },
                    ]}
                />
                <ProjectSegmentedControl
                    className="pm-view-toggle"
                    value={view}
                    onChange={setView}
                    ariaLabel="视图切换"
                    options={[
                        { value: 'list', label: <IconList size={16} aria-label="列表视图" /> },
                        { value: 'grid', label: <IconLayoutGrid size={16} aria-label="网格视图" /> },
                    ]}
                />
            </form>

            {projectQuery.isPending ? (
                <div className="pm-state"><span className="pm-spinner" /><strong>正在读取项目</strong><p>同步项目状态与 Agent 运行信号…</p></div>
            ) : projectQuery.isError ? (
                <ProjectEmptyState tone="error" title="项目列表加载失败" description={projectQuery.error instanceof Error ? projectQuery.error.message : '无法连接项目服务'} action={<Button variant="secondary" className="pm-button pm-button-secondary" onClick={() => projectQuery.refetch()}><IconRefresh size={16} />重新加载</Button>} />
            ) : projects.length === 0 ? (
                <ProjectEmptyState icon={<IconFolder size={24} />} title="这里还没有项目" description={query || status !== 'all' ? '当前筛选条件没有匹配结果，调整条件后再试。' : '创建第一个项目，让一组 Agent 围绕同一个目标协作。'} action={query || status !== 'all' ? <Button variant="secondary" className="pm-button pm-button-secondary" onClick={() => { setQuery(''); setQueryDraft(''); setStatus('all'); }}>清除筛选</Button> : <Button variant="primary" className="pm-button pm-button-primary" onClick={() => navigate('/projects/new')}><IconPlus size={16} />创建项目</Button>} />
            ) : (
                <section className={`pm-projects pm-projects-${view}`}>
                    {projects.map(project => (
                        <ProjectCard key={project.id} className="pm-project-card" role="link" onClick={() => navigate(`/projects/${project.id}`)} tabIndex={0} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); navigate(`/projects/${project.id}`); } }}>
                            <div className="pm-project-icon"><IconFolder size={19} /></div>
                            <div className="pm-project-main">
                                <div className="pm-project-title"><h2>{project.name}</h2><ProjectStatusBadge className={`pm-status pm-status-${project.status}`} tone={statusTones[project.status]}>{statusLabels[project.status]}</ProjectStatusBadge></div>
                                <p>{project.description || project.objective}</p>
                                <div className="pm-project-meta-mobile"><span>{project.visibility === 'private' ? '仅自己' : '已分享'}</span><span>{timeAgo(project.updated_at)}</span></div>
                            </div>
                            <div className="pm-project-team"><AgentStack project={project} /><small>{project.members?.length ?? 0} 个 Agent</small></div>
                            <div className="pm-project-signal"><strong>{project.current_signal || (project.status === 'running' ? 'Leader 正在推进目标' : '暂无运行事件')}</strong><small>{project.next_action || `Leader · ${project.leader_name || '未指定'}`}</small></div>
                            <ProjectProgressBar className="pm-project-progress" value={project.progress} />
                            <div className="pm-project-update"><strong>{timeAgo(project.updated_at)}</strong><small>{project.visibility === 'private' ? <IconLock size={13} /> : <IconUsers size={13} />}{project.visibility === 'private' ? '仅自己' : (project.shared_with_names?.join('、') || '已分享')}</small></div>
                            <IconArrowRight className="pm-project-arrow" size={17} />
                        </ProjectCard>
                    ))}
                </section>
            )}
        </main>
    );
}
