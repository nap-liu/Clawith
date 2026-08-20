import { useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';
import {
    IconAlertTriangle,
    IconArrowLeft,
    IconArrowRight,
    IconCheck,
    IconCrown,
    IconGitCommit,
    IconLoader2,
    IconMessageCircle,
    IconRefresh,
    IconShieldCheck,
    IconSparkles,
    IconTargetArrow,
    IconTool,
    IconUsers,
} from '@tabler/icons-react';

import SessionViewerDrawer, { type SessionViewerTarget } from '../../components/SessionViewerDrawer';
import { projectsApi } from '../../services/projects';
import { Button, ProjectStatusBadge } from './components/ProjectUI';
import './projectPortfolio.css';
import './projectPlanning.css';

type RecordValue = Record<string, unknown>;

const text = (source: RecordValue, ...keys: string[]) => {
    for (const key of keys) {
        const value = source[key];
        if (typeof value === 'string' || typeof value === 'number') return String(value);
    }
    return '';
};

export default function ProjectPlanningPage() {
    const { projectId = '' } = useParams<{ projectId: string }>();
    const navigate = useNavigate();
    const [drawerOpen, setDrawerOpen] = useState(true);
    const projectQuery = useQuery({ queryKey: ['project', projectId], queryFn: () => projectsApi.get(projectId), enabled: Boolean(projectId) });
    const membersQuery = useQuery({ queryKey: ['project', projectId, 'members'], queryFn: () => projectsApi.listMembers(projectId), enabled: Boolean(projectId) });
    const leaderSessionQuery = useQuery({ queryKey: ['project', projectId, 'leader-session'], queryFn: () => projectsApi.getLeaderSession(projectId), enabled: Boolean(projectId) });
    const confirmMutation = useMutation({
        mutationFn: () => projectsApi.confirmKickoff(projectId),
        onSuccess: () => navigate(`/projects/${projectId}`, { replace: true }),
    });

    const members = (Array.isArray(membersQuery.data) ? membersQuery.data : []) as RecordValue[];
    const leader = members.find(member => member.is_leader === true);
    const leaderName = text(leader || {}, 'name_snapshot', 'agent_name', 'name') || '项目 Leader';
    const project = projectQuery.data;
    const session = leaderSessionQuery.data;
    const sessionTarget = useMemo<SessionViewerTarget | null>(() => session ? ({
        sessionId: session.id,
        agentId: session.agent_id,
        title: session.title || `${leaderName} · 项目规划`,
        mode: 'project_planning',
        status: 'planning',
    }) : null, [leaderName, session]);
    const loading = projectQuery.isPending || membersQuery.isPending || leaderSessionQuery.isPending;
    const failed = projectQuery.isError || membersQuery.isError || leaderSessionQuery.isError;
    const discussionCount = Number(session?.discussion_count || 0);
    const canConfirm = discussionCount >= 2;

    if (loading) {
        return <main className="pm-planning-page pm-planning-state"><IconLoader2 className="pm-spinner-icon" size={26} /><strong>正在连接项目 Leader</strong><p>读取项目快照与专属规划会话…</p></main>;
    }

    if (failed || !project || !sessionTarget) {
        const error = projectQuery.error || membersQuery.error || leaderSessionQuery.error;
        return <main className="pm-planning-page pm-planning-state" role="alert"><IconAlertTriangle size={26} /><strong>规划会话暂时不可用</strong><p>{error instanceof Error ? error.message : '项目或 Leader 会话没有准备完成。'}</p><Button variant="secondary" onClick={() => { void projectQuery.refetch(); void membersQuery.refetch(); void leaderSessionQuery.refetch(); }}><IconRefresh size={16} />重新加载</Button></main>;
    }

    const hasStarted = project.status !== 'planning';

    return (
        <main className="pm-planning-page">
            <header className="pm-planning-header">
                <Button variant="ghost" onClick={() => navigate('/projects')}><IconArrowLeft size={16} />项目列表</Button>
                <div><span>LEADER PLANNING</span><h1>{project.name}</h1><p>通过对话形成目标、成功标准与执行方案，不需要先填写流程表单。</p></div>
                <ProjectStatusBadge tone={hasStarted ? 'success' : 'warning'}>{hasStarted ? '已启动' : '规划中'}</ProjectStatusBadge>
            </header>

            <div className="pm-planning-layout">
                <section className="pm-planning-conversation">
                    <div className="pm-planning-leader">
                        <span>{leaderName.slice(0, 1)}</span>
                        <div><small>项目 Leader</small><h2>{leaderName}</h2><p>负责把讨论收敛为可执行计划，并在确认后持续驱动项目目标达成。</p></div>
                        <IconCrown size={22} />
                    </div>
                    <div className="pm-planning-dialogue-card">
                        <IconMessageCircle size={22} />
                        <div><strong>与 Leader 共同规划</strong><p>说明背景、约束和期望结果。Leader 会主动追问缺失信息，并给出可确认的目标与执行方案。</p><small>{discussionCount > 0 ? `规划会话已有 ${discussionCount} 条消息` : '先发送一条消息开始规划'}</small></div>
                        <Button variant="primary" onClick={() => setDrawerOpen(true)}><IconMessageCircle size={16} />{discussionCount > 0 ? '继续规划' : '开始讨论'}</Button>
                    </div>

                    <div className="pm-planning-outcomes" aria-label="规划产出">
                        <article><IconTargetArrow size={18} /><span><strong>目标与边界</strong><small>明确做什么、为什么做，以及不做什么</small></span></article>
                        <article><IconCheck size={18} /><span><strong>成功标准</strong><small>形成可验收、可回溯的完成定义</small></span></article>
                        <article><IconSparkles size={18} /><span><strong>执行方案</strong><small>拆解阶段、责任与首批工作项</small></span></article>
                    </div>
                </section>

                <aside className="pm-planning-governance">
                    <header><IconShieldCheck size={20} /><div><strong>项目工具边界</strong><small>规划不会绕过现有权限与审批策略</small></div></header>
                    <div className="pm-planning-role-scope pm-planning-role-scope--leader"><IconCrown size={17} /><span><strong>Leader · 全项目操作</strong><small>可在项目范围内管理工作项、Run、Git 产物、成员协作与项目能力。</small></span></div>
                    <div className="pm-planning-role-scope"><IconUsers size={17} /><span><strong>参与者 · 原子权限</strong><small>仅暴露角色与项目策略允许的单项操作，不继承 Leader 的全局控制权。</small></span></div>
                    <ul>
                        <li><IconGitCommit size={15} />规划确认记录写入项目 Git</li>
                        <li><IconTool size={15} />Skill / MCP 仍受项目快照约束</li>
                        <li><IconShieldCheck size={15} />高风险动作继续走审批策略</li>
                    </ul>
                    {confirmMutation.isError && <div className="pm-planning-error" role="alert"><IconAlertTriangle size={15} />{confirmMutation.error instanceof Error ? confirmMutation.error.message : '启动失败，请检查规划对话后重试。'}</div>}
                    {hasStarted ? (
                        <Button variant="primary" onClick={() => navigate(`/projects/${projectId}`)}>进入项目 Workspace<IconArrowRight size={16} /></Button>
                    ) : (
                        <Button variant="primary" disabled={confirmMutation.isPending || !canConfirm} onClick={() => confirmMutation.mutate()}>{confirmMutation.isPending ? <IconLoader2 className="pm-spinner-icon" size={16} /> : <IconCheck size={16} />}确认方案并启动 Leader</Button>
                    )}
                    {!hasStarted && <small className="pm-planning-confirm-hint">{canConfirm ? '确认会冻结规划记录、创建普通 Git 提交，并且只唤醒 Leader。' : '与 Leader 至少完成一轮问答后，才可确认并启动。'}</small>}
                </aside>
            </div>

            <SessionViewerDrawer
                agentId={session?.agent_id || ''}
                agentName={leaderName}
                target={drawerOpen ? sessionTarget : null}
                interactive
                onClose={() => {
                    setDrawerOpen(false);
                    void leaderSessionQuery.refetch();
                }}
            />
        </main>
    );
}
