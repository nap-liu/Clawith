import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import {
    IconArrowLeft,
    IconArrowRight,
    IconBolt,
    IconBrandGit,
    IconCheck,
    IconChevronRight,
    IconCode,
    IconRefresh,
    IconSparkles,
    IconUsers,
    IconX,
} from '@tabler/icons-react';
import { projectsApi } from '../../services/projects';
import type { ProjectTemplate } from './types';
import { Button, ProjectCard, ProjectDialog, ProjectEmptyState, ProjectIconButton, SearchInput } from './components/ProjectUI';
import './projectPortfolio.css';

function TemplateDetail({ template, onClose, onUse }: { template: ProjectTemplate; onClose: () => void; onUse: () => void }) {
    return (
        <ProjectDialog open onClose={onClose} ariaLabel={`${template.name} 模板详情`} className="pm-modal pm-template-detail">
                <header>
                    <div className="pm-template-mark"><IconSparkles size={21} /></div>
                    <div><span>{template.category} · {template.version}</span><h2 id="template-detail-title">{template.name}</h2><p>{template.description}</p></div>
                    <ProjectIconButton onClick={onClose} aria-label="关闭"><IconX size={18} /></ProjectIconButton>
                </header>
                <div className="pm-template-detail-stats">
                    <div><span>模板作者</span><strong>{template.author_name || '平台精选'}</strong></div>
                    <div><span>已被使用</span><strong>{template.usage_count.toLocaleString()} 次</strong></div>
                    <div><span>快照版本</span><strong>{template.version}</strong></div>
                </div>
                <section><h3>团队角色</h3><div className="pm-role-list">{template.roles.map(role => <div key={role.key}><IconUsers size={16} /><span><strong>{role.name}</strong><small>{role.description || '创建时选择对应 Agent'}</small></span>{role.required && <em>必需</em>}</div>)}</div></section>
                <section className="pm-template-capabilities">
                    <div><h3>共享 Skill</h3><div className="pm-chip-row">{template.skills.length ? template.skills.map(item => <span key={item.id || item.name}><IconBolt size={14} />{item.name}{item.version && <small>{item.version}</small>}</span>) : <p>此模板未预设 Skill</p>}</div></div>
                    <div><h3>共享 MCP</h3><div className="pm-chip-row">{template.mcp_servers.length ? template.mcp_servers.map(item => <span key={item.id || item.name}><IconCode size={14} />{item.name}{item.version && <small>{item.version}</small>}</span>) : <p>此模板未预设 MCP</p>}</div></div>
                </section>
                <section><h3>创建后的项目边界</h3><ul className="pm-check-list"><li><IconCheck size={15} />固定当前模板版本，市场升级不会改变运行中的项目</li><li><IconCheck size={15} />Agent 会生成项目专用快照，改动不影响原始 Agent</li><li><IconCheck size={15} />所有产出进入项目 Git 仓库，支持提交级恢复</li></ul></section>
                <footer><Button variant="secondary" className="pm-button pm-button-secondary" onClick={onClose}>暂不使用</Button><Button variant="primary" className="pm-button pm-button-primary" onClick={onUse}>使用此模板<IconArrowRight size={16} /></Button></footer>
        </ProjectDialog>
    );
}

export default function ProjectTemplatesPage() {
    const navigate = useNavigate();
    const [category, setCategory] = useState('all');
    const [queryDraft, setQueryDraft] = useState('');
    const [query, setQuery] = useState('');
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const templatesQuery = useQuery({
        queryKey: ['projects', 'templates', 'catalog'],
        queryFn: () => projectsApi.listTemplates(),
    });
    const detailQuery = useQuery({
        queryKey: ['projects', 'template', selectedId],
        queryFn: () => projectsApi.getTemplate(selectedId || ''),
        enabled: Boolean(selectedId),
    });
    const allTemplates = templatesQuery.data ?? [];
    const categories = useMemo(() => ['all', ...Array.from(new Set(allTemplates.map(item => item.category)))], [allTemplates]);
    const templates = useMemo(() => {
        const normalizedQuery = query.toLocaleLowerCase();
        return allTemplates.filter(template => {
            if (category !== 'all' && template.category !== category) return false;
            if (!normalizedQuery) return true;
            return [template.name, template.description, ...template.roles.map(role => role.name), ...template.skills.map(skill => skill.name), ...template.mcp_servers.map(mcp => mcp.name)]
                .join(' ')
                .toLocaleLowerCase()
                .includes(normalizedQuery);
        });
    }, [allTemplates, category, query]);
    const featured = allTemplates.find(item => item.featured) || allTemplates[0];

    const useTemplate = (id: string) => navigate(`/projects/new?template=${encodeURIComponent(id)}`);

    return (
        <main className="pm-page pm-template-market">
            <Button variant="ghost" className="pm-back-link" onClick={() => navigate('/projects')}><IconArrowLeft size={16} />返回项目资产库</Button>
            <header className="pm-page-header">
                <div><span className="pm-eyebrow">PROJECT BLUEPRINTS</span><h1>项目模板市场</h1><p>复用经过验证的团队角色、能力边界、运行策略和交付标准。</p></div>
                <Button variant="primary" className="pm-button pm-button-primary" onClick={() => navigate('/projects/new')}>从空白创建<IconArrowRight size={16} /></Button>
            </header>

            {featured && (
                <section className="pm-market-hero">
                    <div className="pm-market-copy"><span>精选模板 / {featured.category}</span><h2>不只是一组提示词，<br />而是一套可运行的协作系统</h2><p>{featured.description} 模板只负责提供可靠起点，所有 Agent、能力与凭证仍会在创建时由你确认。</p><Button variant="primary" className="pm-button pm-button-primary" onClick={() => setSelectedId(featured.id)}>查看精选模板<IconArrowRight size={16} /></Button></div>
                    <div className="pm-blueprint" aria-label="项目模板组成">
                        <div className="pm-blueprint-line" />
                        {[['01', '目标', '成功标准'], ['02', '团队', 'Agent 快照'], ['03', '能力', 'Skill / MCP'], ['04', '运行', 'Git 产出']].map(item => <article key={item[0]}><i>{item[0]}</i><strong>{item[1]}</strong><small>{item[2]}</small></article>)}
                    </div>
                </section>
            )}

            <form className="pm-market-controls" onSubmit={event => { event.preventDefault(); setQuery(queryDraft.trim()); }}>
                <SearchInput className="pm-search" value={queryDraft} onChange={event => setQueryDraft(event.target.value)} placeholder="搜索场景、角色、Skill 或 MCP" aria-label="搜索模板" />
                <div className="pm-category-row">{categories.map(item => <Button variant="ghost" type="button" key={item} className={category === item ? 'is-active' : ''} onClick={() => setCategory(item)}>{item === 'all' ? '全部模板' : item}</Button>)}</div>
            </form>

            <div className="pm-market-heading"><div><strong>{category === 'all' ? '全部模板' : category}</strong><span>{templates.length} 个结果</span></div></div>
            {templatesQuery.isPending ? (
                <div className="pm-state"><span className="pm-spinner" /><strong>正在加载模板市场</strong></div>
            ) : templatesQuery.isError ? (
                <ProjectEmptyState tone="error" title="模板市场加载失败" description={templatesQuery.error instanceof Error ? templatesQuery.error.message : '无法连接模板服务'} action={<Button variant="secondary" className="pm-button pm-button-secondary" onClick={() => templatesQuery.refetch()}><IconRefresh size={16} />重新加载</Button>} />
            ) : templates.length === 0 ? (
                <ProjectEmptyState icon={<IconSparkles size={23} />} title="没有匹配的模板" description="尝试缩短关键词，或从空白项目开始。" action={<Button variant="secondary" className="pm-button pm-button-secondary" onClick={() => { setQuery(''); setQueryDraft(''); setCategory('all'); }}>清除筛选</Button>} />
            ) : (
                <section className="pm-template-grid">
                    {templates.map(template => (
                        <ProjectCard className="pm-template-card" key={template.id} role="button" tabIndex={0} onClick={() => setSelectedId(template.id)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelectedId(template.id); } }}>
                            <header><span className="pm-template-mark"><IconSparkles size={19} /></span><div><span>{template.category}</span><small>{template.version}</small></div>{template.featured && <em>精选</em>}</header>
                            <h2>{template.name}</h2><p>{template.description}</p>
                            <div className="pm-template-roleline"><IconUsers size={14} />{template.roles.slice(0, 3).map(role => role.name).join(' · ')}{template.roles.length > 3 && <em>+{template.roles.length - 3}</em>}</div>
                            <div className="pm-template-tags"><span><IconBolt size={13} />{template.skills.length} Skill</span><span><IconBrandGit size={13} />{template.mcp_servers.length} MCP</span></div>
                            <footer><span>{template.author_name || '平台精选'} · {template.usage_count.toLocaleString()} 次使用</span><Button variant="ghost" onClick={event => { event.stopPropagation(); useTemplate(template.id); }}>使用模板<IconChevronRight size={14} /></Button></footer>
                        </ProjectCard>
                    ))}
                </section>
            )}

            {selectedId && detailQuery.isPending && <ProjectDialog open onClose={() => setSelectedId(null)} ariaLabel="正在读取模板详情" className="pm-modal pm-modal-loading"><span className="pm-spinner" />正在读取模板详情…</ProjectDialog>}
            {selectedId && detailQuery.isError && <ProjectDialog open onClose={() => setSelectedId(null)} ariaLabel="模板详情加载失败" className="pm-modal pm-modal-error"><ProjectIconButton onClick={() => setSelectedId(null)} aria-label="关闭"><IconX size={18} /></ProjectIconButton><strong>模板详情加载失败</strong><p>{detailQuery.error instanceof Error ? detailQuery.error.message : '请稍后重试'}</p><Button variant="secondary" className="pm-button pm-button-secondary" onClick={() => detailQuery.refetch()}><IconRefresh size={16} />重新加载</Button></ProjectDialog>}
            {selectedId && detailQuery.data && <TemplateDetail template={detailQuery.data} onClose={() => setSelectedId(null)} onUse={() => useTemplate(detailQuery.data.id)} />}
        </main>
    );
}
