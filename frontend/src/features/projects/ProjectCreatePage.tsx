import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
    IconAlertTriangle,
    IconArrowLeft,
    IconArrowRight,
    IconBolt,
    IconBrandGit,
    IconCheck,
    IconChevronRight,
    IconCode,
    IconCrown,
    IconLock,
    IconPlus,
    IconRefresh,
    IconShieldCheck,
    IconTrash,
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
    ProjectEmptyState,
    ProjectField,
    ProjectIconButton,
    ProjectRadioGroup,
    ProjectSelect,
    ProjectTextarea,
    TextInput,
} from './components/ProjectUI';
import './projectPortfolio.css';

const steps = [
    ['基础信息', '目标与成功标准'],
    ['项目团队', 'Agent 与 Leader'],
    ['项目能力', '共享与带入能力'],
    ['运行策略', 'Git、预算与审批'],
    ['权限确认', '默认私有与分享'],
] as const;

type Draft = {
    name: string;
    description: string;
    objective: string;
    successCriteria: string[];
    memberIds: string[];
    leaderId: string;
    sharedCapabilityIds: string[];
    inheritedCapabilityIds: string[];
    repositoryMode: 'managed' | 'external';
    repositoryUrl: string;
    branchPolicy: 'project' | 'work_item';
    runtimeMode: 'economy' | 'balanced' | 'quality';
    monthlyBudget: string;
    approvalPolicy: 'risk' | 'all_writes' | 'manual';
    visibility: ProjectVisibility;
    shareTargets: string[];
};

const initialDraft: Draft = {
    name: '',
    description: '',
    objective: '',
    successCriteria: [''],
    memberIds: [],
    leaderId: '',
    sharedCapabilityIds: [],
    inheritedCapabilityIds: [],
    repositoryMode: 'managed',
    repositoryUrl: '',
    branchPolicy: 'work_item',
    runtimeMode: 'balanced',
    monthlyBudget: '300',
    approvalPolicy: 'risk',
    visibility: 'private',
    shareTargets: [],
};

function toggleItem(items: string[], id: string) {
    return items.includes(id) ? items.filter(item => item !== id) : [...items, id];
}

function AgentAvatar({ agent }: { agent: ProjectAgentOption }) {
    return agent.avatar_url
        ? <img className="pm-agent-avatar" src={agent.avatar_url} alt="" />
        : <span className="pm-agent-avatar">{agent.name.slice(0, 1)}</span>;
}

function CapabilityRow({ capability, selected, onToggle }: { capability: ProjectCapabilityOption; selected: boolean; onToggle: () => void }) {
    return (
        <Button variant="ghost" type="button" className={`pm-capability-row ${selected ? 'is-selected' : ''}`} onClick={onToggle} aria-pressed={selected}>
            <span className={`pm-capability-kind pm-kind-${capability.kind}`}>{capability.kind === 'skill' ? <IconBolt size={15} /> : <IconCode size={15} />}</span>
            <span><strong>{capability.name}</strong><small>{capability.description || `${capability.kind === 'skill' ? 'Skill' : 'MCP'} · ${capability.version || '当前版本'}`}</small></span>
            {capability.risk_level && <em className={`pm-risk pm-risk-${capability.risk_level}`}>{capability.risk_level === 'high' ? '高风险' : capability.risk_level === 'medium' ? '中风险' : '低风险'}</em>}
            <i className="pm-switch"><span /></i>
        </Button>
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
        onSuccess: project => navigate(`/projects/${project.id}`),
        onError: error => setSubmitError(error instanceof Error ? error.message : '项目创建失败'),
    });

    useEffect(() => {
        const template = templateQuery.data;
        const availableCapabilities = bootstrapQuery.data?.capabilities;
        if (!template || !availableCapabilities || seededTemplateId.current === template.id) return;
        seededTemplateId.current = template.id;
        const templateCapabilities = [...template.skills, ...template.mcp_servers];
        const sharedCapabilityIds = templateCapabilities.map(item => item.id || availableCapabilities.find(capability => capability.name.toLocaleLowerCase() === item.name.toLocaleLowerCase())?.id).filter((id): id is string => Boolean(id));
        setDraft(current => ({
            ...current,
            name: current.name || `${template.name}项目`,
            description: current.description || template.description,
            objective: current.objective || template.objective_hint || '',
            successCriteria: template.success_criteria?.length ? template.success_criteria : current.successCriteria,
            sharedCapabilityIds,
        }));
    }, [bootstrapQuery.data?.capabilities, templateQuery.data]);

    const options = bootstrapQuery.data;
    const agents = options?.agents ?? [];
    const capabilities = options?.capabilities ?? [];
    const selectedAgents = agents.filter(agent => draft.memberIds.includes(agent.id));
    const projectCapabilities = capabilities.filter(capability => capability.source === 'project');
    const inheritedCapabilities = capabilities.filter(capability => capability.source === 'agent' && draft.memberIds.includes(capability.owner_agent_id || ''));

    const validation = useMemo(() => {
        const checks = [
            Boolean(draft.name.trim() && draft.objective.trim() && draft.successCriteria.some(item => item.trim())),
            Boolean(draft.memberIds.length && draft.leaderId && draft.memberIds.includes(draft.leaderId)),
            true,
            Boolean(Number(draft.monthlyBudget) > 0 && (draft.repositoryMode !== 'external' || draft.repositoryUrl.trim())),
            draft.visibility === 'private' || draft.shareTargets.length > 0,
        ];
        return checks;
    }, [draft]);

    const patchDraft = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft(current => ({ ...current, [key]: value }));
    const goNext = () => { if (validation[step - 1]) setStep(current => Math.min(5, current + 1)); };
    const submit = () => {
        if (!validation.every(Boolean) || createMutation.isPending) return;
        setSubmitError('');
        createMutation.mutate({
            name: draft.name.trim(),
            description: draft.description.trim(),
            objective: draft.objective.trim(),
            success_criteria: draft.successCriteria.map(item => item.trim()).filter(Boolean),
            template_id: templateId,
            members: draft.memberIds.map(agentId => ({
                agent_id: agentId,
                is_leader: agentId === draft.leaderId,
                enabled_inherited_capability_ids: inheritedCapabilities.filter(capability => capability.owner_agent_id === agentId && draft.inheritedCapabilityIds.includes(capability.id)).map(capability => capability.id),
            })),
            shared_capability_ids: draft.sharedCapabilityIds,
            git: { repository_mode: draft.repositoryMode, repository_url: draft.repositoryMode === 'external' ? draft.repositoryUrl.trim() : null, branch_policy: draft.branchPolicy },
            runtime: { mode: draft.runtimeMode, monthly_budget: Number(draft.monthlyBudget), approval_policy: draft.approvalPolicy },
            visibility: draft.visibility,
            shared_with_user_ids: draft.visibility === 'shared' ? draft.shareTargets : [],
        });
    };

    const renderStep = () => {
        if (step === 1) return <StepOne draft={draft} template={templateQuery.data} patch={patchDraft} />;
        if (step === 2) return <StepTwo agents={agents} capabilities={capabilities} draft={draft} patch={patchDraft} />;
        if (step === 3) return <StepThree projectCapabilities={projectCapabilities} inheritedCapabilities={inheritedCapabilities} draft={draft} patch={patchDraft} />;
        if (step === 4) return <StepFour draft={draft} patch={patchDraft} />;
        return <StepFive draft={draft} selectedAgents={selectedAgents} shareTargets={options?.users ?? []} capabilityCount={draft.sharedCapabilityIds.length + draft.inheritedCapabilityIds.length} patch={patchDraft} />;
    };

    if (bootstrapQuery.isPending || (templateId && templateQuery.isPending)) return <main className="pm-page"><div className="pm-state pm-state-full"><span className="pm-spinner" /><strong>正在准备项目创建环境</strong><p>读取可用 Agent、Skill、MCP 和模板版本…</p></div></main>;
    if (bootstrapQuery.isError || templateQuery.isError) {
        const error = bootstrapQuery.error || templateQuery.error;
        return <main className="pm-page"><ProjectEmptyState className="pm-state-full" tone="error" title="创建环境加载失败" description={error instanceof Error ? error.message : '无法读取项目初始化选项'} action={<Button variant="secondary" className="pm-button pm-button-secondary" onClick={() => { bootstrapQuery.refetch(); templateQuery.refetch(); }}><IconRefresh size={16} />重新加载</Button>} /></main>;
    }

    return (
        <main className="pm-create-page">
            <aside className="pm-create-aside">
                <Button variant="ghost" className="pm-back-link" onClick={() => navigate('/projects')}><IconArrowLeft size={16} />退出创建</Button>
                <div className="pm-create-brand"><span>◆</span><div><strong>创建 AI Native 项目</strong><small>将目标变成可运行的协作系统</small></div></div>
                <ol>{steps.map((item, index) => { const number = index + 1; return <li key={item[0]} className={`${step === number ? 'is-active' : ''} ${step > number ? 'is-done' : ''}`}><Button variant="ghost" onClick={() => { if (number <= step || validation.slice(0, number - 1).every(Boolean)) setStep(number); }} aria-current={step === number ? 'step' : undefined}><i>{step > number ? <IconCheck size={14} /> : number}</i><span><strong>{item[0]}</strong><small>{item[1]}</small></span></Button></li>; })}</ol>
                <div className="pm-snapshot-note"><IconShieldCheck size={18} /><div><strong>快照隔离</strong><p>项目内对 Agent 角色、记忆和能力的调整，不会改变原始 Agent。</p></div></div>
            </aside>
            <section className="pm-create-main">
                <header><div><span>步骤 {step} / 5</span><strong>{steps[step - 1][0]}</strong></div><small>{templateQuery.data ? `基于 ${templateQuery.data.name} · ${templateQuery.data.version}` : '从空白项目创建'}</small></header>
                <div className="pm-create-content">{renderStep()}</div>
                <footer>
                    <Button variant="secondary" className="pm-button pm-button-secondary" disabled={step === 1 || createMutation.isPending} onClick={() => setStep(current => current - 1)}>上一步</Button>
                    <div>{!validation[step - 1] && <span><IconAlertTriangle size={14} />请完成本步骤的必填项</span>}{submitError && <span className="pm-submit-error"><IconAlertTriangle size={14} />{submitError}</span>}</div>
                    {step < 5 ? <Button variant="primary" className="pm-button pm-button-primary" disabled={!validation[step - 1]} onClick={goNext}>继续<IconArrowRight size={16} /></Button> : <Button variant="primary" className="pm-button pm-button-primary" disabled={!validation.every(Boolean) || createMutation.isPending} onClick={submit}>{createMutation.isPending ? <><span className="pm-spinner pm-spinner-small" />正在初始化</> : <>创建并进入项目<IconArrowRight size={16} /></>}</Button>}
                </footer>
            </section>
        </main>
    );
}

type PatchDraft = <K extends keyof Draft>(key: K, value: Draft[K]) => void;

function StepTitle({ number, title, description }: { number: string; title: string; description: string }) {
    return <div className="pm-step-title"><span>{number}</span><div><h1>{title}</h1><p>{description}</p></div></div>;
}

function StepOne({ draft, template, patch }: { draft: Draft; template?: ProjectTemplate; patch: PatchDraft }) {
    return <div className="pm-step-section"><StepTitle number="01" title="定义一个可验收的目标" description="Leader 会依据目标和成功标准拆解工作；清晰的边界能减少无效协作。" />
        <div className="pm-form-grid">
            <ProjectField className="pm-span-2" label="项目名称" labelFor="project-name" required><TextInput id="project-name" value={draft.name} onChange={event => patch('name', event.target.value)} placeholder="例如：客户洞察看板 2.0" /></ProjectField>
            <ProjectField label="起始方式"><div className="pm-static-field"><strong>{template?.name || '空白项目'}</strong><small>{template ? `${template.version} · 创建后固定版本` : '从零配置团队与能力'}</small></div></ProjectField>
            <ProjectField label="项目标识"><div className="pm-static-field"><strong>{draft.name.trim() ? draft.name.trim().toLowerCase().replace(/[^a-z0-9\u4e00-\u9fa5]+/g, '-').slice(0, 36) : '创建后生成'}</strong><small>用于 Git 仓库和运行标识</small></div></ProjectField>
            <ProjectField className="pm-span-2" label="项目简介" labelFor="project-description"><ProjectTextarea id="project-description" value={draft.description} onChange={event => patch('description', event.target.value)} placeholder="说明背景、范围和主要交付物" /></ProjectField>
            <ProjectField className="pm-span-2" label="核心目标" labelFor="project-objective" required hint="建议包含范围、时间和最终交付物"><ProjectTextarea id="project-objective" className="pm-objective" value={draft.objective} onChange={event => patch('objective', event.target.value)} placeholder="在什么时间内，为谁解决什么问题，并交付什么结果？" /></ProjectField>
        </div>
        <section className="pm-criteria"><header><div><strong>成功标准</strong><small>Leader 在最终验收时逐项检查</small></div><Button variant="ghost" type="button" onClick={() => patch('successCriteria', [...draft.successCriteria, ''])}><IconPlus size={14} />添加标准</Button></header>{draft.successCriteria.map((criterion, index) => <label key={index}><i>{index + 1}</i><TextInput value={criterion} onChange={event => patch('successCriteria', draft.successCriteria.map((item, itemIndex) => itemIndex === index ? event.target.value : item))} placeholder="输入可明确判断是否达成的标准" /><ProjectIconButton disabled={draft.successCriteria.length === 1} onClick={() => patch('successCriteria', draft.successCriteria.filter((_, itemIndex) => itemIndex !== index))} aria-label={`删除成功标准 ${index + 1}`}><IconTrash size={14} /></ProjectIconButton></label>)}</section>
    </div>;
}

function StepTwo({ agents, capabilities, draft, patch }: { agents: ProjectAgentOption[]; capabilities: ProjectCapabilityOption[]; draft: Draft; patch: PatchDraft }) {
    return <div className="pm-step-section"><StepTitle number="02" title="组建能直接协作的 Agent 团队" description="任意成员都能直接唤醒其他成员。Leader 负责方向、阻塞处理与最终收敛，不充当消息中转。" />
        <div className="pm-team-summary"><div className="pm-agent-stack">{agents.filter(agent => draft.memberIds.includes(agent.id)).slice(0, 5).map(agent => <AgentAvatar key={agent.id} agent={agent} />)}</div><div><strong>{draft.memberIds.length} 个 Agent 已选择</strong><small>创建时分别生成项目专用快照</small></div><span><i />成员间 A2A 直接唤醒</span></div>
        {agents.length ? <div className="pm-agent-grid">{agents.map(agent => {
            const selected = draft.memberIds.includes(agent.id);
            const leader = draft.leaderId === agent.id;
            const toggleAgent = () => {
                const next = toggleItem(draft.memberIds, agent.id);
                patch('memberIds', next);
                if (!next.includes(draft.leaderId)) patch('leaderId', next[0] || '');
                const ownedCapabilityIds = capabilities.filter(capability => capability.source === 'agent' && capability.owner_agent_id === agent.id).map(capability => capability.id);
                patch('inheritedCapabilityIds', selected ? draft.inheritedCapabilityIds.filter(id => !ownedCapabilityIds.includes(id)) : Array.from(new Set([...draft.inheritedCapabilityIds, ...ownedCapabilityIds])));
            };
            return <article key={agent.id} className={selected ? 'is-selected' : ''}>
                <Button variant="ghost" type="button" className="pm-agent-select" onClick={toggleAgent} aria-pressed={selected}>
                    <i>{selected && <IconCheck size={13} />}</i><AgentAvatar agent={agent} /><span><strong>{agent.name}</strong><small>{agent.role_description}</small></span><em>{(agent.skill_count || 0) + (agent.mcp_count || 0)} 项能力</em>
                </Button>
                {selected && <Button variant="ghost" type="button" className={`pm-leader-select ${leader ? 'is-active' : ''}`} onClick={() => patch('leaderId', agent.id)} aria-pressed={leader}><IconCrown size={14} />{leader ? '项目 Leader' : '设为 Leader'}</Button>}
            </article>;
        })}</div> : <div className="pm-inline-empty"><strong>没有可加入项目的 Agent</strong><p>请先在工作区创建或启用至少一个 Agent。</p></div>}
        <div className="pm-policy-callout"><IconShieldCheck size={18} /><div><strong>Leader 权限边界</strong><p>可以拆解目标、分派任务、请求评审和结束运行；不能绕过项目能力策略、预算上限与人工审批。</p></div></div>
    </div>;
}

function StepThree({ projectCapabilities, inheritedCapabilities, draft, patch }: { projectCapabilities: ProjectCapabilityOption[]; inheritedCapabilities: ProjectCapabilityOption[]; draft: Draft; patch: PatchDraft }) {
    return <div className="pm-step-section"><StepTitle number="03" title="配置项目共享能力" description="共享能力对项目内所有 Agent 生效；Agent 自身带入的能力可以逐项关闭，且只影响当前项目快照。" />
        <section className="pm-capability-group"><header><div><strong>项目共享 Skill / MCP</strong><small>由项目统一固定版本和权限范围</small></div><em>{draft.sharedCapabilityIds.length} 项已启用</em></header>{projectCapabilities.length ? <div>{projectCapabilities.map(capability => <CapabilityRow key={capability.id} capability={capability} selected={draft.sharedCapabilityIds.includes(capability.id)} onToggle={() => patch('sharedCapabilityIds', toggleItem(draft.sharedCapabilityIds, capability.id))} />)}</div> : <div className="pm-inline-empty"><p>当前组织尚未提供项目级 Skill 或 MCP，可创建后在能力中心继续添加。</p></div>}</section>
        <section className="pm-capability-group"><header><div><strong>Agent 带入能力</strong><small>按 Agent 来源展示，关闭后不会从原始 Agent 删除</small></div><em>{draft.inheritedCapabilityIds.length} 项已启用</em></header>{inheritedCapabilities.length ? <div>{inheritedCapabilities.map(capability => <CapabilityRow key={capability.id} capability={capability} selected={draft.inheritedCapabilityIds.includes(capability.id)} onToggle={() => patch('inheritedCapabilityIds', toggleItem(draft.inheritedCapabilityIds, capability.id))} />)}</div> : <div className="pm-inline-empty"><p>已选 Agent 没有可带入的能力，或尚未选择项目成员。</p></div>}</section>
        <div className="pm-policy-callout"><IconLock size={18} /><div><strong>凭证不会写入项目快照</strong><p>MCP 只记录凭证引用和最小作用域；项目 Agent 无法读取凭证明文。</p></div></div>
    </div>;
}

function StepFour({ draft, patch }: { draft: Draft; patch: PatchDraft }) {
    return <div className="pm-step-section"><StepTitle number="04" title="定义运行与产出策略" description="每次运行都在隔离工作区内产生可追溯提交。预算和审批策略在所有 Agent 之上生效。" />
        <div className="pm-strategy-grid">
            <section className="pm-option-section"><header><IconBrandGit size={18} /><div><strong>Git 产出仓库</strong><small>提交、里程碑和恢复均由 Git 驱动</small></div></header><ProjectRadioGroup value={draft.repositoryMode} onChange={value => patch('repositoryMode', value)} ariaLabel="Git 产出仓库" options={[{ value: 'managed', label: '平台托管仓库', description: '自动创建私有仓库与项目主分支' }, { value: 'external', label: '连接外部仓库', description: '仅使用你授权的仓库和分支范围' }]} />{draft.repositoryMode === 'external' && <TextInput className="pm-nested-input" value={draft.repositoryUrl} onChange={event => patch('repositoryUrl', event.target.value)} placeholder="https://github.com/org/repository.git" />}</section>
            <section className="pm-option-section"><header><IconBolt size={18} /><div><strong>运行模式</strong><small>控制模型质量、并行度和成本偏好</small></div></header><ProjectRadioGroup value={draft.runtimeMode} onChange={value => patch('runtimeMode', value)} ariaLabel="运行模式" options={[{ value: 'economy', label: '经济', description: '限制并行，优先低成本模型' }, { value: 'balanced', label: '均衡', description: '在质量、速度和成本间平衡' }, { value: 'quality', label: '质量优先', description: '允许更多复核与高质量模型' }]} /></section>
            <ProjectField label="分支策略" hint="Agent 改动先进入隔离 worktree，再由 Leader 收敛"><ProjectSelect value={draft.branchPolicy} onChange={value => patch('branchPolicy', value)} ariaLabel="分支策略" options={[{ value: 'work_item', label: '每个工作项独立分支' }, { value: 'project', label: '共享项目分支' }]} /></ProjectField>
            <ProjectField label="月度预算上限" labelFor="project-monthly-budget" hint="达到 80% 时提醒，达到上限后自动暂停新运行"><div className="pm-money-input"><span>¥</span><TextInput id="project-monthly-budget" type="number" min="1" value={draft.monthlyBudget} onChange={event => patch('monthlyBudget', event.target.value)} /></div></ProjectField>
            <ProjectField className="pm-span-2" label="人工审批策略"><ProjectSelect value={draft.approvalPolicy} onChange={value => patch('approvalPolicy', value)} ariaLabel="人工审批策略" options={[{ value: 'risk', label: '仅高风险写操作需要审批' }, { value: 'all_writes', label: '所有外部写操作需要审批' }, { value: 'manual', label: '所有交付和写操作均需人工确认' }]} /></ProjectField>
        </div>
    </div>;
}

function StepFive({ draft, selectedAgents, shareTargets, capabilityCount, patch }: { draft: Draft; selectedAgents: ProjectAgentOption[]; shareTargets: Array<{ id: string; name: string; email?: string | null }>; capabilityCount: number; patch: PatchDraft }) {
    return <div className="pm-step-section"><StepTitle number="05" title="确认项目边界并开始" description="项目默认只有你可见。分享只开放项目访问权，不会扩大 Agent、MCP 或凭证权限。" />
        <div className="pm-visibility-options"><Button variant="ghost" type="button" className={draft.visibility === 'private' ? 'is-selected' : ''} onClick={() => patch('visibility', 'private')} aria-pressed={draft.visibility === 'private'}><IconLock size={21} /><span><strong>仅自己可见</strong><small>只有你可以查看和管理项目</small></span><i>{draft.visibility === 'private' && <IconCheck size={14} />}</i></Button><Button variant="ghost" type="button" className={draft.visibility === 'shared' ? 'is-selected' : ''} onClick={() => patch('visibility', 'shared')} aria-pressed={draft.visibility === 'shared'}><IconUsers size={21} /><span><strong>与指定成员共享</strong><small>按项目角色授予查看或协作权限</small></span><i>{draft.visibility === 'shared' && <IconCheck size={14} />}</i></Button></div>
        {draft.visibility === 'shared' && <section className="pm-share-box"><header><strong>共享成员</strong><small>选择当前组织成员；创建后可继续调整项目角色</small></header>{shareTargets.length ? <div className="pm-share-targets">{shareTargets.map(target => { const selected = draft.shareTargets.includes(target.id); return <Button variant="ghost" type="button" key={target.id} className={selected ? 'is-selected' : ''} onClick={() => patch('shareTargets', toggleItem(draft.shareTargets, target.id))} aria-pressed={selected}><i>{selected && <IconCheck size={12} />}</i><span><strong>{target.name}</strong><small>{target.email || target.id}</small></span></Button>; })}</div> : <div className="pm-inline-empty"><p>组织内没有其他可共享成员；请保持“仅自己可见”。</p></div>}</section>}
        <section className="pm-final-review"><header><strong>创建确认</strong><small>以下配置会写入项目初始化提交和审计事件</small></header><dl><div><dt>目标</dt><dd>{draft.objective}</dd></div><div><dt>Agent 团队</dt><dd>{selectedAgents.length} 个成员 · Leader：{selectedAgents.find(agent => agent.id === draft.leaderId)?.name || '未指定'}</dd></div><div><dt>能力</dt><dd>{capabilityCount} 项已启用 · 项目快照隔离</dd></div><div><dt>运行</dt><dd>{draft.repositoryMode === 'managed' ? '平台托管 Git' : '外部 Git'} · ¥{draft.monthlyBudget}/月</dd></div><div><dt>可见性</dt><dd>{draft.visibility === 'private' ? '仅自己可见' : `与 ${draft.shareTargets.length} 名成员共享`}</dd></div></dl></section>
        <div className="pm-policy-callout pm-policy-success"><IconShieldCheck size={18} /><div><strong>创建后立即获得稳定基线</strong><p>系统会固定模板、Agent 与能力版本，初始化 Git 仓库并记录首条审计事件；后续变更都只在项目内生效。</p></div></div>
    </div>;
}
