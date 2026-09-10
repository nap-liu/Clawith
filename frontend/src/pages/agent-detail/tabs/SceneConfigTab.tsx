import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import SceneAutoActivationSection from './SceneAutoActivationSection';
import SceneRuntimeSection from './SceneRuntimeSection';
import {
    IconArrowDown,
    IconArrowUp,
    IconExternalLink,
    IconMessage,
    IconPlus,
    IconRefresh,
    IconTrash,
} from '@tabler/icons-react';

import {
    sceneApi,
    type Scene,
    type SceneQuickAction,
    type SceneQuickActionStyle,
    type SceneSystemPrompt,
} from '../../../services/api';
import { parseMiniProgramUri } from '../../../utils/miniProgramUri';
import SelectDropdown from '../../../components/SelectDropdown';
import ToggleSwitch from '../../../components/ToggleSwitch';
import { useDialog } from '../../../components/Dialog/DialogProvider';
import './SceneConfigTab.css';

type Section = 'welcome' | 'prompts' | 'actions' | 'activation' | 'runtime';

const emptyScene = (): Scene => ({
    scene_key: '',
    name: '',
    enabled: true,
    revision: 0,
    has_unpublished_changes: false,
    welcome_message: '',
    system_prompts: [],
    quick_actions: [],
    revisions: [],
});

const discardWarning = '当前有未发布的编辑内容，继续操作将丢失这些修改。是否继续？';
const defaultQuickActionStyle: SceneQuickActionStyle = {
    bold: false,
    italic: false,
    color: null,
    font: 'default',
};

const editableSceneSnapshot = (scene: Scene) => JSON.stringify({
    include_soul: scene.include_soul ?? true,
    include_memory: scene.include_memory ?? true,
    tools: scene.tools ?? null,
    mcp_server_overrides: scene.mcp_server_overrides ?? [],
    auto_activation: scene.auto_activation,
    scene_key: scene.scene_key,
    name: scene.name,
    enabled: scene.enabled,
    welcome_message: scene.welcome_message,
    system_prompts: scene.system_prompts,
    quick_actions: scene.quick_actions,
});

const itemId = (prefix: string) =>
    `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;

function moveItem<T>(items: T[], index: number, offset: number): T[] {
    const nextIndex = index + offset;
    if (nextIndex < 0 || nextIndex >= items.length) return items;
    const next = [...items];
    [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
    return next;
}

function validateSceneDraft(scene: Scene): string {
    if (!scene.name.trim()) return '请填写场景名称。';
    if (!/^[a-z][a-z0-9_-]{0,63}$/.test(scene.scene_key)) {
        return '场景标识需以小写字母开头，且只能包含小写字母、数字、_ 或 -。';
    }

    const promptIds = new Set<string>();
    for (const [index, prompt] of scene.system_prompts.entries()) {
        if (!prompt.name.trim()) return `请填写第 ${index + 1} 个系统提示词的名称。`;
        if (!prompt.content.trim()) return `请填写第 ${index + 1} 个系统提示词的内容。`;
        if (promptIds.has(prompt.id)) return '系统提示词存在重复标识，请删除后重新添加。';
        promptIds.add(prompt.id);
    }

    const actionIds = new Set<string>();
    for (const [index, action] of scene.quick_actions.entries()) {
        if (!action.label.trim()) return `请填写第 ${index + 1} 个快捷入口的显示文案。`;
        if (actionIds.has(action.id)) return '快捷入口存在重复标识，请删除后重新添加。';
        actionIds.add(action.id);
        if (action.type === 'send_message') {
            if (!(action.message || '').trim()) return `请填写第 ${index + 1} 个快捷入口发送的消息。`;
            continue;
        }

        const uri = (action.uri || '').trim();
        if (!uri) return `请填写第 ${index + 1} 个快捷入口打开的地址。`;
        if (uri.startsWith('/') && !uri.startsWith('//')) continue;
        try {
            const parsed = new URL(uri);
            const isHttp = (parsed.protocol === 'http:' || parsed.protocol === 'https:') && !!parsed.host;
            const isMiniProgram = parseMiniProgramUri(uri) !== null;
            if (isHttp || isMiniProgram) continue;
        } catch {
            // Report the shared format hint below.
        }
        return `第 ${index + 1} 个快捷入口的地址格式不正确。`;
    }
    return '';
}

export default function SceneConfigTab({
    agentId,
    onDirtyChange,
}: {
    agentId: string;
    onDirtyChange?: (dirty: boolean) => void;
}) {
    const dialog = useDialog();
    const { t } = useTranslation();
    const [scenes, setScenes] = useState<Scene[]>([]);
    const [draft, setDraft] = useState<Scene>(emptyScene);
    const [savedSnapshot, setSavedSnapshot] = useState(() => editableSceneSnapshot(emptyScene()));
    const [selectedKey, setSelectedKey] = useState<string | null>(null);
    const [section, setSection] = useState<Section>('welcome');
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [preview, setPreview] = useState<Scene | null>(null);
    const [previewLoading, setPreviewLoading] = useState<number | null>(null);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');

    const isNew = !draft.id;
    const isPreviewing = preview !== null;
    const visibleScene = preview || draft;
    const isDirty = useMemo(
        () => editableSceneSnapshot(draft) !== savedSnapshot,
        [draft, savedSnapshot],
    );
    const primaryAction = isNew
        ? 'create'
        : isDirty
            ? 'save'
            : draft.has_unpublished_changes
                ? 'publish'
                : 'published';

    const setCleanDraft = (scene: Scene) => {
        setDraft(scene);
        setSavedSnapshot(editableSceneSnapshot(scene));
        setPreview(null);
    };

    const confirmDiscard = () => (
        !isDirty
            ? Promise.resolve(true)
            : dialog.confirm(discardWarning, {
                title: '放弃未保存修改？',
                danger: true,
                confirmLabel: '继续',
            })
    );

    useEffect(() => {
        onDirtyChange?.(isDirty);
    }, [isDirty, onDirtyChange]);

    useEffect(() => () => {
        onDirtyChange?.(false);
    }, [onDirtyChange]);

    const selectScene = async (sceneKey: string, clearFeedback = true) => {
        if (clearFeedback) {
            setError('');
            setNotice('');
        }
        try {
            const scene = await sceneApi.get(agentId, sceneKey);
            setSelectedKey(sceneKey);
            setCleanDraft(scene);
        } catch (e: any) {
            setError(e?.message || '加载场景失败');
        }
    };

    const loadScenes = async (preferredKey?: string, preserveFeedback = false) => {
        setLoading(true);
        setError('');
        try {
            const list = await sceneApi.list(agentId);
            setScenes(list);
            const nextKey = preferredKey || selectedKey || list[0]?.scene_key;
            if (nextKey && list.some((item) => item.scene_key === nextKey)) {
                await selectScene(nextKey, !preserveFeedback);
            } else {
                setSelectedKey(null);
                setCleanDraft(emptyScene());
            }
        } catch (e: any) {
            setError(e?.message || '加载场景失败');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        void loadScenes();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [agentId]);

    const updatePrompt = (index: number, patch: Partial<SceneSystemPrompt>) => {
        setDraft((current) => ({
            ...current,
            system_prompts: current.system_prompts.map((item, itemIndex) =>
                itemIndex === index ? { ...item, ...patch } : item,
            ),
        }));
    };

    const updateAction = (index: number, patch: Partial<SceneQuickAction>) => {
        setDraft((current) => ({
            ...current,
            quick_actions: current.quick_actions.map((item, itemIndex) =>
                itemIndex === index ? { ...item, ...patch } : item,
            ),
        }));
    };

    const updateActionStyle = (index: number, patch: Partial<SceneQuickActionStyle>) => {
        setDraft((current) => ({
            ...current,
            quick_actions: current.quick_actions.map((item, itemIndex) => (
                itemIndex === index
                    ? {
                        ...item,
                        style: {
                            ...defaultQuickActionStyle,
                            ...(item.style || {}),
                            ...patch,
                        },
                    }
                    : item
            )),
        }));
    };

    const switchActionType = (index: number, type: SceneQuickAction['type']) => {
        setDraft((current) => ({
            ...current,
            quick_actions: current.quick_actions.map((item, itemIndex) => {
                if (itemIndex !== index || item.type === type) return item;
                const visibleValue = item.type === 'open_uri' ? item.uri : item.message;
                return type === 'open_uri'
                    ? { ...item, type, uri: item.uri ?? visibleValue ?? '' }
                    : { ...item, type, message: item.message ?? visibleValue ?? '' };
            }),
        }));
    };

    const saveDraft = async () => {
        if (isPreviewing) return;
        const validationError = draft.auto_activation?.enabled && !draft.auto_activation.targets.length
            ? t('sceneAuto.selectRequired') : validateSceneDraft(draft);
        if (validationError) {
            setError(validationError);
            setNotice('');
            return;
        }
        setSaving(true);
        setError('');
        setNotice('');
        try {
            const saved = await sceneApi.save(agentId, draft.scene_key, {
                include_soul: draft.include_soul ?? true,
                include_memory: draft.include_memory ?? true,
                tools: draft.tools ?? null,
                mcp_server_overrides: draft.mcp_server_overrides ?? [],
                auto_activation: draft.auto_activation,
                name: draft.name.trim(),
                enabled: draft.enabled,
                expected_revision: draft.revision,
                welcome_message: draft.welcome_message,
                system_prompts: draft.system_prompts,
                quick_actions: draft.quick_actions,
            });
            setSelectedKey(saved.scene_key);
            setCleanDraft(saved);
            setNotice(isNew ? '场景已创建为草稿' : '草稿已保存');
            await loadScenes(saved.scene_key, true);
        } catch (e: any) {
            setError(e?.message || (isNew ? '创建失败' : '保存失败'));
        } finally {
            setSaving(false);
        }
    };

    const publish = async () => {
        if (isPreviewing || !draft.id || !draft.has_unpublished_changes) return;
        setSaving(true);
        setError('');
        setNotice('');
        try {
            const published = await sceneApi.publish(agentId, draft.scene_key, draft.revision);
            setCleanDraft(published);
            setNotice(`已发布版本 ${published.revision}`);
            await loadScenes(published.scene_key, true);
        } catch (e: any) {
            setError(e?.message || '发布失败');
        } finally {
            setSaving(false);
        }
    };

    const runPrimaryAction = () => {
        if (isPreviewing) return;
        if (primaryAction === 'create' || primaryAction === 'save') {
            void saveDraft();
        } else if (primaryAction === 'publish') {
            void publish();
        }
    };

    const remove = async () => {
        if (isPreviewing || !draft.id) return;
        const confirmed = await dialog.confirm(
            `确认删除场景“${draft.name}”？历史版本也会一并删除。`,
            {
                title: '删除场景',
                danger: true,
                confirmLabel: '删除',
            },
        );
        if (!confirmed) return;
        setSaving(true);
        try {
            await sceneApi.delete(agentId, draft.scene_key);
            setNotice('场景已删除');
            setSelectedKey(null);
            setCleanDraft(emptyScene());
            await loadScenes();
        } catch (e: any) {
            setError(e?.message || '删除失败');
        } finally {
            setSaving(false);
        }
    };

    const previewRevision = async (targetRevision: number) => {
        setPreviewLoading(targetRevision);
        setError('');
        setNotice('');
        try {
            const revision = await sceneApi.revision(agentId, draft.scene_key, targetRevision);
            setPreview(revision);
        } catch (e: any) {
            setError(e?.message || '加载历史版本失败');
        } finally {
            setPreviewLoading(null);
        }
    };

    return (
        <div className="scene-config">
            <aside className="scene-config__rail">
                <div className="scene-config__rail-head">
                    <div>
                        <strong>场景</strong>
                        <span>管理场景内容与快捷入口</span>
                    </div>
                    <button
                        className="scene-config__icon-button"
                        title="新增场景"
                        onClick={async () => {
                            if (!(await confirmDiscard())) return;
                            setSelectedKey(null);
                            setCleanDraft(emptyScene());
                            setNotice('');
                            setError('');
                        }}
                    >
                        <IconPlus size={17} />
                    </button>
                </div>
                <div className="scene-config__scene-list">
                    {loading && <div className="scene-config__empty">加载中…</div>}
                    {!loading && scenes.length === 0 && (
                        <div className="scene-config__empty">还没有场景，先创建一个。</div>
                    )}
                    {scenes.map((scene) => (
                        <button
                            key={scene.scene_key}
                            className={`scene-config__scene ${selectedKey === scene.scene_key ? 'is-active' : ''}`}
                            onClick={async () => {
                                if (!(await confirmDiscard())) return;
                                void selectScene(scene.scene_key);
                            }}
                        >
                            <span>{scene.name}</span>
                            <small>
                                {scene.scene_key}
                                {scene.revision > 0 ? ` · v${scene.revision}` : ' · 草稿'}
                                {scene.has_unpublished_changes && scene.revision > 0 ? ' · 有未发布修改' : ''}
                                {scene.enabled ? '' : ' · 已停用'}
                            </small>
                        </button>
                    ))}
                </div>
            </aside>

            <section className="scene-config__workspace">
                <header className="scene-config__header">
                    <div>
                        <div className="scene-config__title-row">
                            <h2>{isNew ? '新建场景' : visibleScene.name}</h2>
                            {isPreviewing && (
                                <span className="scene-config__preview-badge">
                                    历史版本 v{visibleScene.revision} · {visibleScene.enabled ? '已启用' : '已停用'}
                                </span>
                            )}
                        </div>
                        <p>
                            {isPreviewing
                                ? '当前为历史版本预览，只可查看，不能修改、保存或发布。'
                                : '编辑内容先保存为草稿，发布后生效。'}
                        </p>
                    </div>
                    <div className="scene-config__header-actions">
                        {isPreviewing ? (
                            <button className="btn btn-secondary" onClick={() => setPreview(null)}>
                                退出预览
                            </button>
                        ) : (
                            <>
                                <div className="scene-config__enabled-control">
                                    <span>启用场景</span>
                                    <ToggleSwitch
                                        checked={draft.enabled}
                                        onChange={(checked) => setDraft({ ...draft, enabled: checked })}
                                        disabled={saving}
                                        ariaLabel="启用场景"
                                    />
                                </div>
                                {!isNew && (
                                    <button
                                        className="btn btn-ghost"
                                        onClick={async () => {
                                            if (!(await confirmDiscard())) return;
                                            void loadScenes(draft.scene_key);
                                        }}
                                        disabled={saving}
                                    >
                                        <IconRefresh size={15} /> 刷新
                                    </button>
                                )}
                                {!isNew && (
                                    <button className="btn btn-ghost scene-config__danger" onClick={() => void remove()} disabled={saving}>
                                        <IconTrash size={15} /> 删除
                                    </button>
                                )}
                                <button
                                    className={`btn scene-config__primary-action is-${primaryAction}`}
                                    onClick={runPrimaryAction}
                                    disabled={saving || primaryAction === 'published'}
                                >
                                    {saving
                                        ? primaryAction === 'publish' ? '发布中…' : '处理中…'
                                        : primaryAction === 'create' ? '创建'
                                            : primaryAction === 'save' ? '保存'
                                                : primaryAction === 'publish' ? '发布'
                                                    : '已发布'}
                                </button>
                            </>
                        )}
                    </div>
                </header>

                {(error || notice) && (
                    <div className={`scene-config__message ${error ? 'is-error' : 'is-success'}`}>{error ? t(error) : notice}</div>
                )}

                <div className="scene-config__identity">
                    <label>
                        <span>场景名称（必填）</span>
                        <input
                            required
                            value={visibleScene.name}
                            maxLength={100}
                            readOnly={isPreviewing}
                            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                            placeholder="请输入用于识别的名称"
                        />
                    </label>
                    <label>
                        <span>场景标识（必填）</span>
                        <input
                            required
                            value={visibleScene.scene_key}
                            disabled={!isNew || isPreviewing}
                            maxLength={64}
                            onChange={(e) => setDraft({ ...draft, scene_key: e.target.value.toLowerCase() })}
                            placeholder="请输入以小写字母开头的唯一标识"
                        />
                    </label>
                </div>

                <nav className="scene-config__subtabs" aria-label="场景配置分区">
                    <button className={section === 'runtime' ? 'is-active' : ''} onClick={() => setSection('runtime')}>{t('sceneRuntime.title')}</button>
                    <button className={section === 'activation' ? 'is-active' : ''} onClick={() => setSection('activation')}>{t('sceneAuto.title')}</button>
                    <button className={section === 'welcome' ? 'is-active' : ''} onClick={() => setSection('welcome')}>初始化欢迎词</button>
                    <button className={section === 'prompts' ? 'is-active' : ''} onClick={() => setSection('prompts')}>系统提示词</button>
                    <button className={section === 'actions' ? 'is-active' : ''} onClick={() => setSection('actions')}>快捷入口</button>
                </nav>

                {section === 'activation' && (
                    <SceneAutoActivationSection agentId={agentId} value={visibleScene.auto_activation}
                        disabled={saving || isPreviewing}
                        onChange={(auto_activation) => setDraft({ ...draft, auto_activation })} />
                )}

                {section === 'runtime' && (
                    <SceneRuntimeSection key={`${visibleScene.scene_key}:${preview?.revision ?? 'draft'}`}
                        agentId={agentId} value={visibleScene} disabled={isPreviewing || saving}
                        onChange={(patch) => setDraft((current) => ({ ...current, ...patch }))} />
                )}
                {section === 'welcome' && (
                    <div className="scene-config__panel">
                        <div className="scene-config__panel-title">
                            <div><strong>初始化欢迎词（选填）</strong><span>首次进入空会话时展示；留空则使用默认欢迎词。</span></div>
                        </div>
                        <textarea
                            className="scene-config__large-textarea"
                            value={visibleScene.welcome_message}
                            maxLength={12000}
                            readOnly={isPreviewing}
                            onChange={(e) => setDraft({ ...draft, welcome_message: e.target.value })}
                            placeholder="请输入首次进入空会话时展示的内容"
                        />
                    </div>
                )}

                {section === 'prompts' && (
                    <div className="scene-config__panel">
                        <div className="scene-config__panel-title">
                            <div><strong>场景系统提示词（选填）</strong><span>留空时不添加；已启用的内容按列表顺序加入对话。</span></div>
                            {!isPreviewing && (
                                <button
                                    className="btn btn-secondary"
                                    onClick={() => setDraft({ ...draft, system_prompts: [...draft.system_prompts, { id: itemId('prompt'), name: '提示词', content: '', enabled: true }] })}
                                >
                                    <IconPlus size={15} /> 添加
                                </button>
                            )}
                        </div>
                        {visibleScene.system_prompts.map((prompt, index) => (
                            <article className="scene-config__item" key={prompt.id}>
                                <div className="scene-config__item-toolbar">
                                    <input value={prompt.name} maxLength={80} readOnly={isPreviewing} onChange={(e) => updatePrompt(index, { name: e.target.value })} aria-label="提示词名称" />
                                    <label><input type="checkbox" checked={prompt.enabled} disabled={isPreviewing} onChange={(e) => updatePrompt(index, { enabled: e.target.checked })} /> 启用</label>
                                    {!isPreviewing && (
                                        <>
                                            <button onClick={() => setDraft({ ...draft, system_prompts: moveItem(draft.system_prompts, index, -1) })} disabled={index === 0}><IconArrowUp size={15} /></button>
                                            <button onClick={() => setDraft({ ...draft, system_prompts: moveItem(draft.system_prompts, index, 1) })} disabled={index === draft.system_prompts.length - 1}><IconArrowDown size={15} /></button>
                                            <button className="scene-config__danger" onClick={() => setDraft({ ...draft, system_prompts: draft.system_prompts.filter((_, i) => i !== index) })}><IconTrash size={15} /></button>
                                        </>
                                    )}
                                </div>
                                <textarea value={prompt.content} maxLength={24000} readOnly={isPreviewing} onChange={(e) => updatePrompt(index, { content: e.target.value })} placeholder="请输入需要追加的系统指令" />
                            </article>
                        ))}
                        {visibleScene.system_prompts.length === 0 && <div className="scene-config__empty">尚未配置场景提示词。</div>}
                    </div>
                )}

                {section === 'actions' && (
                    <div className="scene-config__panel">
                        <div className="scene-config__panel-title">
                            <div><strong>快捷入口（选填）</strong><span>留空时不显示；配置后按列表顺序展示。</span></div>
                            {!isPreviewing && (
                                <button
                                    className="btn btn-secondary"
                                    onClick={() => setDraft({
                                        ...draft,
                                        quick_actions: [
                                            ...draft.quick_actions,
                                            {
                                                id: itemId('action'),
                                                label: '快捷入口',
                                                type: 'send_message',
                                                menu_visible: true,
                                                ai_visible: true,
                                                ai_context: '',
                                                message: '',
                                            },
                                        ],
                                    })}
                                >
                                    <IconPlus size={15} /> 添加
                                </button>
                            )}
                        </div>
                        {visibleScene.quick_actions.map((action, index) => (
                            <article className="scene-config__item scene-config__action-item" key={action.id}>
                                <div className="scene-config__item-toolbar">
                                    <span className="scene-config__kind-icon">{action.type === 'open_uri' ? <IconExternalLink size={16} /> : <IconMessage size={16} />}</span>
                                    <input value={action.label} maxLength={80} readOnly={isPreviewing} onChange={(e) => updateAction(index, { label: e.target.value })} aria-label="入口文案" />
                                    <SelectDropdown
                                        className="scene-config__action-type-select"
                                        value={action.type}
                                        options={[
                                            { value: 'send_message', label: '发送预置消息' },
                                            { value: 'open_uri', label: '打开地址' },
                                        ]}
                                        disabled={isPreviewing}
                                        ariaLabel="快捷入口类型"
                                        onChange={(type) => switchActionType(index, type)}
                                    />
                                    <div className="scene-config__visibility-controls">
                                        <div className="scene-config__item-enabled">
                                            <span>菜单可见</span>
                                            <ToggleSwitch
                                                checked={action.menu_visible ?? action.enabled ?? true}
                                                onChange={(menu_visible) => updateAction(index, { menu_visible })}
                                                disabled={isPreviewing}
                                                ariaLabel={`${action.label || '快捷入口'}菜单可见状态`}
                                            />
                                        </div>
                                        <div className="scene-config__item-enabled">
                                            <span>AI可见</span>
                                            <ToggleSwitch
                                                checked={action.ai_visible ?? action.enabled ?? true}
                                                onChange={(ai_visible) => updateAction(index, { ai_visible })}
                                                disabled={isPreviewing}
                                                ariaLabel={`${action.label || '快捷入口'}AI可见状态`}
                                            />
                                        </div>
                                    </div>
                                    {!isPreviewing && (
                                        <>
                                            <button onClick={() => setDraft({ ...draft, quick_actions: moveItem(draft.quick_actions, index, -1) })} disabled={index === 0}><IconArrowUp size={15} /></button>
                                            <button onClick={() => setDraft({ ...draft, quick_actions: moveItem(draft.quick_actions, index, 1) })} disabled={index === draft.quick_actions.length - 1}><IconArrowDown size={15} /></button>
                                            <button className="scene-config__danger" onClick={() => setDraft({ ...draft, quick_actions: draft.quick_actions.filter((_, i) => i !== index) })}><IconTrash size={15} /></button>
                                        </>
                                    )}
                                </div>
                                <textarea
                                    value={action.type === 'open_uri' ? (action.uri || '') : (action.message || '')}
                                    maxLength={action.type === 'open_uri' ? 2048 : 12000}
                                    readOnly={isPreviewing}
                                    onChange={(e) => updateAction(index, action.type === 'open_uri' ? { uri: e.target.value } : { message: e.target.value })}
                                    placeholder={action.type === 'open_uri'
                                        ? '请输入相对路径、HTTP(S) 地址或 miniprogram://navigate-to/ 路径'
                                        : '请输入点击后直接发送的消息内容'}
                                />
                                <div className="scene-config__action-style">
                                    <span>横向按钮样式</span>
                                    <button
                                        type="button"
                                        className={action.style?.bold ? 'is-active' : ''}
                                        disabled={isPreviewing}
                                        aria-pressed={action.style?.bold || false}
                                        onClick={() => updateActionStyle(index, { bold: !action.style?.bold })}
                                    >
                                        <strong>B</strong>
                                    </button>
                                    <button
                                        type="button"
                                        className={action.style?.italic ? 'is-active' : ''}
                                        disabled={isPreviewing}
                                        aria-pressed={action.style?.italic || false}
                                        onClick={() => updateActionStyle(index, { italic: !action.style?.italic })}
                                    >
                                        <em>I</em>
                                    </button>
                                    <SelectDropdown
                                        className="scene-config__action-font-select"
                                        value={action.style?.font || 'default'}
                                        options={[
                                            { value: 'default', label: '默认字体' },
                                            { value: 'sans', label: '无衬线' },
                                            { value: 'serif', label: '衬线' },
                                            { value: 'monospace', label: '等宽' },
                                        ]}
                                        disabled={isPreviewing}
                                        ariaLabel={`${action.label || '快捷入口'}字体`}
                                        onChange={(font) => updateActionStyle(index, { font })}
                                    />
                                    <label className="scene-config__action-color">
                                        <span>颜色</span>
                                        <input
                                            type="color"
                                            value={action.style?.color || '#8B8B9E'}
                                            disabled={isPreviewing}
                                            aria-label={`${action.label || '快捷入口'}文字颜色`}
                                            onChange={(event) => updateActionStyle(index, { color: event.target.value.toUpperCase() })}
                                        />
                                    </label>
                                    {!isPreviewing && action.style && (
                                        <button
                                            type="button"
                                            className="scene-config__action-style-reset"
                                            onClick={() => updateAction(index, { style: null })}
                                        >
                                            恢复默认
                                        </button>
                                    )}
                                </div>
                                <label className="scene-config__action-context">
                                    <span>
                                        <strong>AI 详细上下文（选填）</strong>
                                        <small>仅在“AI可见”开启时注入，不会展示在快捷入口菜单中。</small>
                                    </span>
                                    <textarea
                                        value={action.ai_context || ''}
                                        maxLength={4000}
                                        readOnly={isPreviewing}
                                        onChange={(e) => updateAction(index, { ai_context: e.target.value })}
                                        placeholder="可填写适用场景、业务含义、使用条件和引导方式"
                                    />
                                </label>
                            </article>
                        ))}
                        {visibleScene.quick_actions.length === 0 && <div className="scene-config__empty">尚未配置快捷入口。</div>}
                    </div>
                )}

                {!isNew && (draft.revisions?.length || 0) > 0 && (
                    <details className="scene-config__history" open={isPreviewing || undefined}>
                        <summary>版本记录（当前发布 v{draft.revision}）</summary>
                        <div>
                            {draft.revisions?.map((item) => (
                                <button
                                    type="button"
                                    key={item.revision}
                                    className={preview?.revision === item.revision ? 'is-active' : ''}
                                    disabled={previewLoading !== null || saving}
                                    onClick={() => void previewRevision(item.revision)}
                                >
                                    {previewLoading === item.revision ? '加载中…' : `v${item.revision}`}
                                    {item.revision === draft.revision && <em>当前发布</em>}
                                    <span>{item.created_at ? new Date(item.created_at).toLocaleString() : ''}</span>
                                </button>
                            ))}
                        </div>
                    </details>
                )}
            </section>
        </div>
    );
}
