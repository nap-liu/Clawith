import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
    IconAlertTriangle,
    IconArrowLeft,
    IconArrowRight,
    IconBolt,
    IconCheck,
    IconCode,
    IconCrown,
    IconLock,
    IconMessageCircle,
    IconRefresh,
    IconShieldCheck,
    IconUsers,
} from '@tabler/icons-react';

import { projectsApi } from '../../services/projects';
import type {
    ProjectAgentOption,
    ProjectCapabilityOption,
    ProjectCreatePayload,
    ProjectTemplate,
    ProjectVisibility,
} from './types';
import {
    Button,
    ProjectCountBadge,
    ProjectEmptyState,
    ProjectField,
    TextInput,
    ToggleSwitch,
} from './components/ProjectUI';
import './projectPortfolio.css';

const steps = [
    ['组建团队', '项目名、Agent 与 Leader'],
    ['配置能力', '共享 Skill / MCP 与带入能力'],
    ['确认边界', '可见性与下一步规划'],
] as const;

type Draft = {
    name: string;
    memberIds: string[];
    leaderId: string;
    sharedCapabilityIds: string[];
    inheritedCapabilityIds: string[];
    visibility: ProjectVisibility;
    shareTargets: string[];
};

function defaultProjectName() {
    const now = new Date();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    return `新项目 · ${month}-${day}`;
}

function initialDraft(): Draft {
    return {
        name: defaultProjectName(),
        memberIds: [],
        leaderId: '',
        sharedCapabilityIds: [],
        inheritedCapabilityIds: [],
        visibility: 'private',
        shareTargets: [],
    };
}

function toggleItem(items: string[], id: string) {
    return items.includes(id) ? items.filter(item => item !== id) : [...items, id];
}

function AgentAvatar({ agent }: { agent: ProjectAgentOption }) {
    return agent.avatar_url
        ? <img className="pm-agent-avatar" src={agent.avatar_url} alt="" />
        : <span className="pm-agent-avatar">{agent.name.slice(0, 1)}</span>;
}

function CapabilityRow({ capability, selected, onToggle }: { capability: ProjectCapabilityOption; selected: boolean; onToggle: (checked: boolean) => void }) {
    const kindLabel = capability.kind === 'skill' ? 'Skill' : 'MCP';
    return (
        <div className={`pm-capability-row ${selected ? 'is-selected' : ''}`}>
            <span className={`pm-capability-kind pm-kind-${capability.kind}`}>{capability.kind === 'skill' ? <IconBolt size={15} /> : <IconCode size={15} />}</span>
            <span><strong>{capability.name}</strong><small>{capability.description || `${kindLabel} · ${capability.version || '当前版本'}`}</small></span>
            {capability.risk_level && <em className={`pm-risk pm-risk-${capability.risk_level}`}>{capability.risk_level === 'high' ? '高风险' : capability.risk_level === 'medium' ? '中风险' : '低风险'}</em>}
            <ToggleSwitch checked={selected} onChange={onToggle} ariaLabel={`${selected ? '停用' : '启用'} ${capability.name}`} />
        </div>
    );
}

export default function ProjectCreatePage() {
    const navigate = useNavigate();
    const [searchParams] = useSearchParams();
    const templateId = searchParams.get('template');
    const seededTemplateId = useRef<string | null>(null);
    const [step, setStep] = useState(1);
    const [draft, setDraft] = useState<Draft>(initialDraft);
    const [submitError, setSubmitError] = useState('');

    const bootstrapQuery = useQuery({ queryKey: ['projects', 'bootstrap-options'], queryFn: projectsApi.bootstrapOptions });
    const templateQuery = useQuery({
        queryKey: ['projects', 'template', templateId],
        queryFn: () => projectsApi.getTemplate(templateId || ''),
        enabled: Boolean(templateId),
    });
    const createMutation = useMutation({
        mutationFn: (payload: ProjectCreatePayload) => projectsApi.create(payload),
        onSuccess: project => navigate(`/projects/${project.id}/planning`),
        onError: error => setSubmitError(error instanceof Error ? error.message : '项目创建失败'),
    });

    useEffect(() => {
        const template = templateQuery.data;
        const availableCapabilities = bootstrapQuery.data?.capabilities;
        if (!template || !availableCapabilities || seededTemplateId.current === template.id) return;
        seededTemplateId.current = template.id;
        const templateCapabilities = [...template.skills, ...template.mcp_servers];
        const sharedCapabilityIds = templateCapabilities
            .map(item => item.id || availableCapabilities.find(capability => capability.name.toLocaleLowerCase() === item.name.toLocaleLowerCase())?.id)
            .filter((id): id is string => Boolean(id));
        setDraft(current => ({ ...current, name: `${template.name}项目`, sharedCapabilityIds }));
    }, [bootstrapQuery.data?.capabilities, templateQuery.data]);

    const options = bootstrapQuery.data;
    const agents = options?.agents ?? [];
    const capabilities = options?.capabilities ?? [];
    const selectedAgents = agents.filter(agent => draft.memberIds.includes(agent.id));
    const projectCapabilities = capabilities.filter(capability => capability.source === 'project');
    const inheritedCapabilities = capabilities.filter(capability => capability.source === 'agent' && draft.memberIds.includes(capability.owner_agent_id || ''));
    const leader = selectedAgents.find(agent => agent.id === draft.leaderId);

    const validation = useMemo(() => [
        Boolean(draft.name.trim() && draft.memberIds.length && draft.leaderId && draft.memberIds.includes(draft.leaderId)),
        true,
        draft.visibility === 'private' || draft.shareTargets.length > 0,
    ], [draft]);

    const patchDraft = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft(current => ({ ...current, [key]: value }));
    const goNext = () => { if (validation[step - 1]) setStep(current => Math.min(3, current + 1)); };
    const submit = () => {
        if (!validation.every(Boolean) || createMutation.isPending) return;
        setSubmitError('');
        createMutation.mutate({
            name: draft.name.trim() || defaultProjectName(),
            description: '',
            objective: '',
            success_criteria: [],
            template_id: templateId,
            members: draft.memberIds.map(agentId => ({
                agent_id: agentId,
                is_leader: agentId === draft.leaderId,
                enabled_inherited_capability_ids: inheritedCapabilities
                    .filter(capability => capability.owner_agent_id === agentId && draft.inheritedCapabilityIds.includes(capability.id))
                    .map(capability => capability.capability_id || capability.id),
            })),
            shared_capability_ids: draft.sharedCapabilityIds,
            git: { repository_mode: 'managed', repository_url: null, branch_policy: 'work_item' },
            runtime: { mode: 'balanced', monthly_budget: 300, approval_policy: 'risk' },
            visibility: draft.visibility,
            shared_with_user_ids: draft.visibility === 'shared' ? draft.shareTargets : [],
        });
    };

    if (bootstrapQuery.isPending || (templateId && templateQuery.isPending)) {
        return <main className="pm-page"><div className="pm-state pm-state-full"><span className="pm-spinner" /><strong>正在准备项目环境</strong><p>读取可用 Agent、Skill、MCP 和模板版本…</p></div></main>;
    }
    if (bootstrapQuery.isError || templateQuery.isError) {
        const error = bootstrapQuery.error || templateQuery.error;
        return <main className="pm-page"><ProjectEmptyState className="pm-state-full" tone="error" title="创建环境加载失败" description={error instanceof Error ? error.message : '无法读取项目初始化选项'} action={<Button variant="secondary" className="pm-button pm-button-secondary" onClick={() => { bootstrapQuery.refetch(); templateQuery.refetch(); }}><IconRefresh size={16} />重新加载</Button>} /></main>;
    }

    return (
        <main className="pm-create-page pm-create-page--planning">
            <aside className="pm-create-aside">
                <Button variant="ghost" className="pm-back-link" onClick={() => navigate('/projects')}><IconArrowLeft size={16} />退出创建</Button>
                <div className="pm-create-brand"><span>◆</span><div><strong>初始化 AI Native 项目</strong><small>先建立协作空间，再与 Leader 完成规划</small></div></div>
                <ol>{steps.map((item, index) => {
                    const number = index + 1;
                    return <li key={item[0]} className={`${step === number ? 'is-active' : ''} ${step > number ? 'is-done' : ''}`}><Button variant="ghost" onClick={() => { if (number <= step || validation.slice(0, number - 1).every(Boolean)) setStep(number); }} aria-current={step === number ? 'step' : undefined}><i>{step > number ? <IconCheck size={14} /> : number}</i><span><strong>{item[0]}</strong><small>{item[1]}</small></span></Button></li>;
                })}</ol>
                <div className="pm-snapshot-note"><IconShieldCheck size={18} /><div><strong>稳定基线</strong><p>Agent 与能力会生成项目快照；后续讨论和改动只在当前项目生效。</p></div></div>
            </aside>
            <section className="pm-create-main">
                <header><div><span>初始化 {step} / 3</span><strong>{steps[step - 1][0]}</strong></div><small>{templateQuery.data ? `基于 ${templateQuery.data.name} · ${templateQuery.data.version}` : '最小配置 · 创建后进入规划'}</small></header>
                <div className="pm-create-content">
                    {step === 1 && <TeamStep draft={draft} template={templateQuery.data} agents={agents} capabilities={capabilities} patch={patchDraft} />}
                    {step === 2 && <CapabilitiesStep projectCapabilities={projectCapabilities} inheritedCapabilities={inheritedCapabilities} draft={draft} patch={patchDraft} />}
                    {step === 3 && <BoundaryStep draft={draft} selectedAgents={selectedAgents} leader={leader} shareTargets={options?.users ?? []} capabilityCount={draft.sharedCapabilityIds.length + draft.inheritedCapabilityIds.length} patch={patchDraft} />}
                </div>
                <footer>
                    <Button variant="secondary" className="pm-button pm-button-secondary" disabled={step === 1 || createMutation.isPending} onClick={() => setStep(current => current - 1)}>上一步</Button>
                    <div>{!validation[step - 1] && <span><IconAlertTriangle size={14} />请完成本步骤的必填项</span>}{submitError && <span className="pm-submit-error"><IconAlertTriangle size={14} />{submitError}</span>}</div>
                    {step < 3
                        ? <Button variant="primary" className="pm-button pm-button-primary" disabled={!validation[step - 1]} onClick={goNext}>继续<IconArrowRight size={16} /></Button>
                        : <Button variant="primary" className="pm-button pm-button-primary" disabled={!validation.every(Boolean) || createMutation.isPending} onClick={submit}>{createMutation.isPending ? <><span className="pm-spinner pm-spinner-small" />正在建立空间</> : <>创建并与 Leader 规划<IconMessageCircle size={16} /></>}</Button>}
                </footer>
            </section>
        </main>
    );
}

type PatchDraft = <K extends keyof Draft>(key: K, value: Draft[K]) => void;

function StepTitle({ number, title, description }: { number: string; title: string; description: string }) {
    return <div className="pm-step-title"><span>{number}</span><div><h1>{title}</h1><p>{description}</p></div></div>;
}

function TeamStep({ draft, template, agents, capabilities, patch }: { draft: Draft; template?: ProjectTemplate; agents: ProjectAgentOption[]; capabilities: ProjectCapabilityOption[]; patch: PatchDraft }) {
    return <div className="pm-step-section"><StepTitle number="01" title="先组建项目团队" description="这里只建立协作空间，不要求提前写完整目标。创建后与 Leader 讨论目标和方案，再确认启动。" />
        <div className="pm-form-grid pm-create-minimal-name">
            <ProjectField className="pm-span-2" label="项目名称" labelFor="project-name" hint="已提供默认名称，可以随时修改。"><TextInput id="project-name" value={draft.name} onChange={event => patch('name', event.target.value)} placeholder={defaultProjectName()} /></ProjectField>
            {template && <ProjectField className="pm-span-2" label="起始模板"><div className="pm-static-field"><strong>{template.name}</strong><small>{template.version} · 只固定团队与能力起点，不预设最终目标</small></div></ProjectField>}
        </div>
        <div className="pm-planning-intro"><IconMessageCircle size={19} /><div><strong>目标将在项目内共同形成</strong><p>项目创建后先进入规划阶段，由你和 Leader 澄清背景、目标、成功标准与执行方案；确认后再启动 Agent 运行。</p></div></div>
        <div className="pm-team-summary"><div className="pm-agent-stack">{agents.filter(agent => draft.memberIds.includes(agent.id)).slice(0, 5).map(agent => <AgentAvatar key={agent.id} agent={agent} />)}</div><div><strong>{draft.memberIds.length} 个 Agent 已选择</strong><small>创建时分别生成项目专用快照</small></div><span><i />成员间 A2A 直接唤醒</span></div>
        {agents.length ? <div className="pm-agent-grid">{agents.map(agent => {
            const selected = draft.memberIds.includes(agent.id);
            const isLeader = draft.leaderId === agent.id;
            const toggleAgent = () => {
                const next = toggleItem(draft.memberIds, agent.id);
                patch('memberIds', next);
                if (!next.includes(draft.leaderId)) patch('leaderId', next[0] || '');
                const ownedCapabilities = capabilities.filter(capability => capability.source === 'agent' && capability.owner_agent_id === agent.id);
                const ownedCapabilityIds = ownedCapabilities.map(capability => capability.id);
                const ownedDefaultIds = ownedCapabilities.filter(capability => capability.enabled_by_default !== false).map(capability => capability.id);
                patch('inheritedCapabilityIds', selected ? draft.inheritedCapabilityIds.filter(id => !ownedCapabilityIds.includes(id)) : Array.from(new Set([...draft.inheritedCapabilityIds, ...ownedDefaultIds])));
            };
            return <article key={agent.id} className={selected ? 'is-selected' : ''}>
                <Button variant="ghost" type="button" className="pm-agent-select" onClick={toggleAgent} aria-pressed={selected}><i>{selected && <IconCheck size={13} />}</i><AgentAvatar agent={agent} /><span><strong>{agent.name}</strong><small>{agent.role_description}</small></span><em>{(agent.skill_count || 0) + (agent.mcp_count || 0)} 项能力</em></Button>
                {selected && <Button variant="ghost" type="button" className={`pm-leader-select ${isLeader ? 'is-active' : ''}`} onClick={() => patch('leaderId', agent.id)} aria-pressed={isLeader}><IconCrown size={14} />{isLeader ? '项目 Leader' : '设为 Leader'}</Button>}
            </article>;
        })}</div> : <ProjectEmptyState title="没有可加入项目的 Agent" description="请先在工作区创建或启用至少一个 Agent。" />}
    </div>;
}

function CapabilitiesStep({ projectCapabilities, inheritedCapabilities, draft, patch }: { projectCapabilities: ProjectCapabilityOption[]; inheritedCapabilities: ProjectCapabilityOption[]; draft: Draft; patch: PatchDraft }) {
    return <div className="pm-step-section"><StepTitle number="02" title="确定项目可用能力" description="项目能力对所有成员共享；Agent 自身能力可以逐项启停，所有选择只影响当前项目快照。" />
        <section className="pm-capability-group"><header><div><strong>项目共享 Skill / MCP</strong><small>项目成员共同使用，版本与权限由项目固定</small></div><ProjectCountBadge>{draft.sharedCapabilityIds.length} 项</ProjectCountBadge></header>{projectCapabilities.length ? <div>{projectCapabilities.map(capability => <CapabilityRow key={capability.id} capability={capability} selected={draft.sharedCapabilityIds.includes(capability.id)} onToggle={() => patch('sharedCapabilityIds', toggleItem(draft.sharedCapabilityIds, capability.id))} />)}</div> : <div className="pm-inline-empty"><p>当前组织尚未提供项目级 Skill 或 MCP，可创建后在能力中心添加。</p></div>}</section>
        <section className="pm-capability-group"><header><div><strong>Agent 带入能力</strong><small>按来源 Agent 展示，关闭不会修改原始 Agent</small></div><ProjectCountBadge>{draft.inheritedCapabilityIds.length} 项</ProjectCountBadge></header>{inheritedCapabilities.length ? <div>{inheritedCapabilities.map(capability => <CapabilityRow key={capability.id} capability={capability} selected={draft.inheritedCapabilityIds.includes(capability.id)} onToggle={() => patch('inheritedCapabilityIds', toggleItem(draft.inheritedCapabilityIds, capability.id))} />)}</div> : <div className="pm-inline-empty"><p>所选 Agent 没有可带入的能力。</p></div>}</section>
        <div className="pm-policy-callout"><IconLock size={18} /><div><strong>凭证不会写入项目快照</strong><p>MCP 只记录凭证引用和最小作用域；项目 Agent 无法读取凭证明文。</p></div></div>
    </div>;
}

function BoundaryStep({ draft, selectedAgents, leader, shareTargets, capabilityCount, patch }: { draft: Draft; selectedAgents: ProjectAgentOption[]; leader?: ProjectAgentOption; shareTargets: Array<{ id: string; name: string; email?: string | null }>; capabilityCount: number; patch: PatchDraft }) {
    return <div className="pm-step-section"><StepTitle number="03" title="确认项目边界" description="项目默认只有你可见。分享只开放项目访问，不会扩大 Agent、MCP 或凭证权限。" />
        <div className="pm-visibility-options"><Button variant="ghost" type="button" className={draft.visibility === 'private' ? 'is-selected' : ''} onClick={() => patch('visibility', 'private')} aria-pressed={draft.visibility === 'private'}><IconLock size={21} /><span><strong>仅自己可见</strong><small>只有你可以查看和管理项目</small></span><i>{draft.visibility === 'private' && <IconCheck size={14} />}</i></Button><Button variant="ghost" type="button" className={draft.visibility === 'shared' ? 'is-selected' : ''} onClick={() => patch('visibility', 'shared')} aria-pressed={draft.visibility === 'shared'}><IconUsers size={21} /><span><strong>与指定成员共享</strong><small>选择组织成员查看项目进展与产出</small></span><i>{draft.visibility === 'shared' && <IconCheck size={14} />}</i></Button></div>
        {draft.visibility === 'shared' && <section className="pm-share-box"><header><strong>共享成员</strong><small>创建后仍可继续调整项目访问</small></header>{shareTargets.length ? <div className="pm-share-targets">{shareTargets.map(target => { const selected = draft.shareTargets.includes(target.id); return <Button variant="ghost" type="button" key={target.id} className={selected ? 'is-selected' : ''} onClick={() => patch('shareTargets', toggleItem(draft.shareTargets, target.id))} aria-pressed={selected}><i>{selected && <IconCheck size={12} />}</i><span><strong>{target.name}</strong><small>{target.email || target.id}</small></span></Button>; })}</div> : <div className="pm-inline-empty"><p>组织内没有其他可共享成员；请保持“仅自己可见”。</p></div>}</section>}
        <section className="pm-final-review"><header><strong>初始化确认</strong><small>只创建稳定协作基线，不会直接启动 Agent 执行</small></header><dl><div><dt>项目</dt><dd>{draft.name}</dd></div><div><dt>团队</dt><dd>{selectedAgents.length} 个 Agent · Leader：{leader?.name || '未指定'}</dd></div><div><dt>能力</dt><dd>{capabilityCount} 项已启用 · 项目快照隔离</dd></div><div><dt>可见性</dt><dd>{draft.visibility === 'private' ? '仅自己可见' : `与 ${draft.shareTargets.length} 名成员共享`}</dd></div><div className="pm-review-planning"><dt>下一步</dt><dd>与 Leader 讨论目标和方案，再确认启动</dd></div></dl></section>
        <div className="pm-policy-callout pm-policy-success"><IconMessageCircle size={18} /><div><strong>创建后先进入规划，不直接运行</strong><p>系统先建立项目 Git 基线、成员快照与能力边界；随后进入项目 Workspace，与 Leader 共同补齐目标、成功标准和执行方案。</p></div></div>
    </div>;
}
