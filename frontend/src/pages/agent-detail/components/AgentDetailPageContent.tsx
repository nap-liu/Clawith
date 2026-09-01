import React from 'react';
import {
    IconBrain,
    IconFolder,
    IconMessageCircle,
    IconSettings,
} from '@tabler/icons-react';

import ConfirmModal from '../../../components/ConfirmModal';
import type { FileBrowserApi } from '../../../components/FileBrowser';
import FileBrowser from '../../../components/FileBrowser';
import ChatImageLightbox from '../../../components/ChatImageLightbox';
import SessionViewerDrawer from '../../../components/SessionViewerDrawer';
import PromptModal from '../../../components/PromptModal';
import { agentApi, fileApi } from '../../../services/api';

import { AGENT_DETAIL_TABS } from '../agentDetailTabs';
import ApprovalsTab from '../tabs/ApprovalsTab';
import MindTab from '../tabs/MindTab';
import SceneConfigTab from '../tabs/SceneConfigTab';
import SettingsTab from '../tabs/SettingsTab';
import SkillsTab from '../tabs/SkillsTab';
import ToolsTab from '../tabs/ToolsTab';
import AccessPermissionsPanel from './AccessPermissionsPanel';
import ActivityLogTabContent from './ActivityLogTabContent';
import AgentInfoCard from './AgentInfoCard';
import AwareTabContent from './AwareTabContent';
import ChatTabContent from './ChatTabContent';
import RelationshipEditor from './RelationshipEditor';
import StatusTabContent from './StatusTabContent';
import { formatTokens } from '../shared';

export default function AgentDetailPageContent(props: Record<string, any>) {
    const {
        t,
        i18n,
        dialog,
        activeTab,
        isChatRoute,
        isSettingsRoute,
        location,
        navigate,
        queryClient,
        id,
        agent,
        canManage,
        token,
        livePanelVisible,
        sidePanelTab,
        clearCardCloseTimer,
        scheduleCardClose,
        editingName,
        nameInput,
        infoCardOpen,
        formatAgentDate,
        expiryLabel,
        openExpiryModal,
        modelLabel,
        modelProvider,
        todayParts,
        monthParts,
        totalParts,
        cacheReadToday,
        cacheHitRateToday,
        setSceneConfigDirty,
        promptModal,
        deleteConfirm,
        chatImagePreview,
        subagentSessionRun,
        uploadToast,
        showExpiryModal,
        expiryValue,
        expiryQuickHours,
        expirySaving,
        workspacePath,
        unavailableAttachmentKeys,
    } = props;

    const workspaceApi: FileBrowserApi = {
        list: (path) => fileApi.list(id, path),
        read: (path) => fileApi.read(id, path),
        write: (path, content) => fileApi.write(id, path, content),
        delete: (path) => fileApi.delete(id, path),
        upload: (file, path, onProgress) => fileApi.upload(id, file, `${path}/`, onProgress),
        downloadUrl: (path) => fileApi.downloadUrl(id, path),
    };

    return (
        <>
            <div className={`agent-detail-page ${activeTab === 'chat' ? 'agent-detail-page--chat' : 'agent-detail-page--settings'}`}>
                {activeTab === 'chat' && (
                    <div className="page-header agent-detail-header">
                        <div
                            className="agent-detail-identity agent-detail-identity--compact"
                            onMouseEnter={clearCardCloseTimer}
                            onMouseLeave={scheduleCardClose}
                        >
                            <div className="agent-detail-identity-trigger">
                                <div className="agent-detail-avatar" style={{ overflow: 'hidden' }}>
                                    {agent.avatar_url ? (
                                        <img
                                            src={agent.avatar_url.startsWith('/api') ? `${agent.avatar_url}${agent.avatar_url.includes('?') ? '&' : '?'}token=${token}` : agent.avatar_url}
                                            alt=""
                                            style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                                        />
                                    ) : (
                                        (Array.from(agent.name || 'A')[0] as string || 'A').toUpperCase()
                                    )}
                                </div>
                                <div style={{ flex: 1, minWidth: 0, overflow: 'hidden' }}>
                                    {canManage && editingName ? (
                                        <input
                                            className="page-title"
                                            autoFocus
                                            value={nameInput}
                                            onChange={(event) => props.setNameInput(event.target.value)}
                                            onBlur={async () => {
                                                props.setEditingName(false);
                                                if (props.nameInput.trim() && props.nameInput !== agent.name) {
                                                    await agentApi.update(id, { name: props.nameInput.trim() } as any);
                                                    queryClient.invalidateQueries({ queryKey: ['agent', id] });
                                                } else {
                                                    props.setNameInput(agent.name);
                                                }
                                            }}
                                            onKeyDown={async (event) => {
                                                if (event.key === 'Enter') (event.target as HTMLInputElement).blur();
                                                if (event.key === 'Escape') {
                                                    props.setEditingName(false);
                                                    props.setNameInput(agent.name);
                                                }
                                            }}
                                            style={{
                                                background: 'var(--bg-elevated)',
                                                border: '1px solid var(--accent-primary)',
                                                borderRadius: '6px',
                                                color: 'var(--text-primary)',
                                                padding: '4px 10px',
                                                minWidth: '320px',
                                                width: 'auto',
                                                outline: 'none',
                                                marginBottom: '0',
                                                display: 'block',
                                            }}
                                        />
                                    ) : (
                                        <h1
                                            className="page-title"
                                            title={canManage ? 'Click to edit name' : undefined}
                                            onClick={() => {
                                                if (canManage) {
                                                    props.setNameInput(agent.name);
                                                    props.setEditingName(true);
                                                }
                                            }}
                                            style={{ cursor: canManage ? 'text' : 'default', borderBottom: canManage ? '1px dashed transparent' : 'none', display: 'inline-block', marginBottom: '0' }}
                                            onMouseEnter={(event) => {
                                                if (canManage) event.currentTarget.style.borderBottomColor = 'var(--text-tertiary)';
                                            }}
                                            onMouseLeave={(event) => {
                                                if (canManage) event.currentTarget.style.borderBottomColor = 'transparent';
                                            }}
                                        >
                                            {agent.name}
                                        </h1>
                                    )}
                                </div>
                                <button
                                    className={`agent-info-chevron${infoCardOpen ? ' agent-info-chevron--open' : ''}`}
                                    onClick={(event) => {
                                        event.stopPropagation();
                                        props.setInfoCardOpen((previous: boolean) => !previous);
                                    }}
                                    aria-label="Toggle agent info"
                                >
                                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                        <path d="m6 9 6 6 6-6" />
                                    </svg>
                                </button>
                            </div>
                            <AgentInfoCard
                                infoCardOpen={infoCardOpen}
                                t={t}
                                agent={agent}
                                formatAgentDate={formatAgentDate}
                                expiryLabel={expiryLabel}
                                canManage={canManage}
                                openExpiryModal={openExpiryModal}
                                modelLabel={modelLabel}
                                modelProvider={modelProvider}
                                todayParts={todayParts}
                                monthParts={monthParts}
                                totalParts={totalParts}
                                formatTokens={formatTokens}
                                cacheReadToday={cacheReadToday}
                                cacheHitRateToday={cacheHitRateToday}
                            />
                        </div>
                        <div className="agent-detail-actions">
                            <>
                                <button
                                    className={`btn btn-ghost agent-top-action ${livePanelVisible && sidePanelTab === 'workspace' ? 'active' : ''}`}
                                    onClick={() => props.togglePreviewPanel('workspace')}
                                >
                                    <IconFolder size={16} stroke={1.7} />
                                    <span>{t('agent.tabs.workspace')}</span>
                                </button>
                                {agent.agent_type !== 'openclaw' && (
                                    <button
                                        className={`btn btn-ghost agent-top-action ${(isSettingsRoute && location.hash === '#aware') || (livePanelVisible && sidePanelTab === 'aware') ? 'active' : ''}`}
                                        onClick={() => (isChatRoute ? props.togglePreviewPanel('aware') : props.setActiveTab('aware'))}
                                    >
                                        <IconBrain size={16} stroke={1.7} />
                                        <span>{t('agent.tabs.aware')}</span>
                                    </button>
                                )}
                                <button
                                    className={`btn btn-ghost agent-top-action ${isSettingsRoute ? 'active' : ''}`}
                                    onClick={() => navigate(`/agents/${id}/settings`)}
                                >
                                    <IconSettings size={16} stroke={1.7} />
                                    <span>{t('agent.tabs.settings')}</span>
                                </button>
                            </>
                            {agent.agent_type !== 'openclaw' && (
                                <>
                                    {canManage && agent.status === 'stopped' && (
                                        <button className="btn btn-secondary" onClick={async () => { await agentApi.start(id); queryClient.invalidateQueries({ queryKey: ['agent', id] }); }}>{t('agent.actions.start')}</button>
                                    )}
                                    {canManage && agent.status === 'running' && (
                                        <button className="btn btn-secondary" onClick={async () => { await agentApi.stop(id); queryClient.invalidateQueries({ queryKey: ['agent', id] }); }}>{t('agent.actions.stop')}</button>
                                    )}
                                </>
                            )}
                        </div>
                    </div>
                )}

                {activeTab !== 'chat' && (
                    <div className="tabs">
                        {AGENT_DETAIL_TABS.filter((tab) => {
                            if (['workspace', 'chat'].includes(tab)) return false;
                            if (tab === 'scenes' && (!canManage || !agent.scene_config_enabled)) return false;
                            if (agent.access_level === 'use') {
                                if (tab === 'settings' || tab === 'approvals') return false;
                            }
                            if (agent.agent_type === 'openclaw') {
                                return ['status', 'relationships', 'chat', 'activityLog', 'settings'].includes(tab);
                            }
                            return true;
                        }).map((tab) => (
                            <div key={tab} className={`tab ${activeTab === tab ? 'active' : ''}`} onClick={() => props.setActiveTab(tab)}>
                                {t(`agent.tabs.${tab}`)}
                            </div>
                        ))}
                        <button className="btn btn-ghost agent-top-action agent-tabs-chat-action" onClick={() => props.setActiveTab('chat')}>
                            <IconMessageCircle size={16} stroke={1.7} />
                            <span>{t('agent.actions.chat')}</span>
                        </button>
                    </div>
                )}

                {activeTab === 'status' && <StatusTabContent {...(props as any)} formatTokens={formatTokens} />}

                {activeTab === 'aware' && <AwareTabContent {...(props as any)} />}

                {activeTab === 'mind' && id && <MindTab agentId={id} canEdit={agent.access_level !== 'use'} />}

                {activeTab === 'tools' && id && <ToolsTab agentId={id} agentName={agent.name || 'Agent'} canManage={canManage} />}

                {activeTab === 'scenes' && id && canManage && agent.scene_config_enabled && (
                    <SceneConfigTab agentId={id} onDirtyChange={setSceneConfigDirty} />
                )}

                {activeTab === 'skills' && id && <SkillsTab agentId={id} canManage={canManage} />}

                {activeTab === 'relationships' && <RelationshipEditor agentId={id} readOnly={!canManage} />}

                {activeTab === 'workspace' && (
                    <FileBrowser
                        api={workspaceApi}
                        rootPath="workspace"
                        features={{ upload: canManage, newFile: canManage, newFolder: canManage, edit: canManage, delete: canManage, directoryNavigation: true }}
                    />
                )}

                {activeTab === 'chat' && <ChatTabContent {...(props as any)} />}

                {activeTab === 'activityLog' && <ActivityLogTabContent {...(props as any)} />}

                {activeTab === 'approvals' && id && <ApprovalsTab agentId={id} canManage={canManage} />}

                {activeTab === 'settings' && id && (
                    <SettingsTab
                        {...(props as any)}
                        agentId={id}
                        accessPermissionsPanel={(
                            <AccessPermissionsPanel
                                agentId={id}
                                permData={props.permData}
                                canManage={canManage}
                                queryClient={queryClient}
                            />
                        )}
                        formatTokens={formatTokens}
                        onDeleteAgent={async () => {
                            try {
                                await agentApi.delete(id);
                                queryClient.invalidateQueries({ queryKey: ['agents'] });
                                navigate('/');
                            } catch (error: any) {
                                await dialog.alert('删除数字员工失败', { type: 'error', details: String(error?.message || error) });
                            }
                        }}
                    />
                )}
            </div>

            <PromptModal
                open={!!promptModal}
                title={promptModal?.title || ''}
                placeholder={promptModal?.placeholder || ''}
                onCancel={() => props.setPromptModal(null)}
                onConfirm={async (value) => {
                    const action = promptModal?.action;
                    props.setPromptModal(null);
                    if (action === 'newFolder') {
                        await fileApi.write(id, `${workspacePath}/${value}/.gitkeep`, '');
                        queryClient.invalidateQueries({ queryKey: ['files', id, workspacePath] });
                    } else if (action === 'newFile') {
                        await fileApi.write(id, `${workspacePath}/${value}`, '');
                        queryClient.invalidateQueries({ queryKey: ['files', id, workspacePath] });
                        props.setViewingFile(`${workspacePath}/${value}`);
                        props.setFileEditing(true);
                        props.setFileDraft('');
                    } else if (action === 'newSkill') {
                        const template = `---\nname: ${value}\ndescription: Describe what this skill does\n---\n\n# ${value}\n\n## Overview\nDescribe the purpose and when to use this skill.\n\n## Process\n1. Step one\n2. Step two\n\n## Output Format\nDescribe the expected output format.\n`;
                        await fileApi.write(id, `skills/${value}/SKILL.md`, template);
                        queryClient.invalidateQueries({ queryKey: ['files', id, 'skills'] });
                        props.setViewingFile(`skills/${value}/SKILL.md`);
                        props.setFileEditing(true);
                        props.setFileDraft(template);
                    }
                }}
            />

            <ConfirmModal
                open={!!deleteConfirm}
                title={t('common.delete')}
                message={`${t('common.delete')}: ${deleteConfirm?.name}?`}
                confirmLabel={t('common.delete')}
                danger
                onCancel={() => props.setDeleteConfirm(null)}
                onConfirm={async () => {
                    const path = deleteConfirm?.path;
                    props.setDeleteConfirm(null);
                    if (path) {
                        try {
                            await fileApi.delete(id, path);
                            props.setViewingFile(null);
                            props.setFileEditing(false);
                            queryClient.invalidateQueries({ queryKey: ['files', id, workspacePath] });
                            props.showToast(t('common.delete'));
                        } catch {
                            props.showToast(t('agent.upload.failed'), 'error');
                        }
                    }
                }}
            />

            <ChatImageLightbox
                open={!!chatImagePreview}
                images={chatImagePreview?.images || []}
                index={chatImagePreview?.index || 0}
                mode="desktop"
                onClose={() => props.setChatImagePreview(null)}
                onIndexChange={(index) => props.setChatImagePreview((previous: any) => (previous ? { ...previous, index } : previous))}
            />

            <SessionViewerDrawer
                agentId={id}
                agentName={agent.name || 'Agent'}
                target={subagentSessionRun?.sessionId ? {
                    sessionId: subagentSessionRun.sessionId,
                    agentId: subagentSessionRun.executionAgentId,
                    title: subagentSessionRun.name || subagentSessionRun.task,
                    status: subagentSessionRun.status,
                    mode: subagentSessionRun.mode,
                    model: subagentSessionRun.model,
                } : null}
                onClose={props.closeSubagentSession}
                unavailableAttachmentKeys={unavailableAttachmentKeys}
                onAttachmentDownload={props.handleAttachmentDownload}
                onAttachmentUnavailable={props.markAttachmentUnavailable}
                onPreviewImages={(images, index) => props.setChatImagePreview({ images, index })}
            />

            {uploadToast && (
                <div style={{
                    position: 'fixed',
                    top: '20px',
                    right: '20px',
                    zIndex: 20000,
                    padding: '12px 20px',
                    borderRadius: '8px',
                    background: uploadToast.type === 'success' ? 'rgba(34, 197, 94, 0.9)' : 'rgba(239, 68, 68, 0.9)',
                    color: '#fff',
                    fontSize: '14px',
                    fontWeight: 500,
                    boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
                }}>
                    {''}
                    {uploadToast.message}
                </div>
            )}

            {showExpiryModal && (
                <div className="agent-expiry-modal-backdrop" onClick={() => props.setShowExpiryModal(false)}>
                    <div className="agent-expiry-modal" onClick={(event) => event.stopPropagation()}>
                        <div className="agent-expiry-modal-header">
                            <div>
                                <h3>{t('agent.settings.expiry.title')}</h3>
                                <div className="agent-expiry-current">
                                    {agent.is_expired ? (
                                        <span className="agent-expiry-status agent-expiry-status--expired">{t('agent.settings.expiry.expired')}</span>
                                    ) : agent.expires_at ? (
                                        <>
                                            {t('agent.settings.expiry.currentExpiry')}
                                            {' '}
                                            <strong>{new Date(agent.expires_at).toLocaleString(i18n.language === 'zh' ? 'zh-CN' : 'en-US')}</strong>
                                        </>
                                    ) : (
                                        <span className="agent-expiry-status">{t('agent.settings.expiry.neverExpires')}</span>
                                    )}
                                </div>
                            </div>
                            <button className="agent-expiry-close" onClick={() => props.setShowExpiryModal(false)} aria-label={t('common.close', 'Close')}>×</button>
                        </div>
                        <div className="agent-expiry-section">
                            <div className="agent-expiry-label">{t('agent.settings.expiry.quickRenew')}</div>
                            <div className="agent-expiry-quick-actions">
                                {([
                                    ['+ 24h', 24],
                                    [`+ ${t('agent.settings.expiry.days', { count: 7 })}`, 168],
                                    [`+ ${t('agent.settings.expiry.days', { count: 30 })}`, 720],
                                    [`+ ${t('agent.settings.expiry.days', { count: 90 })}`, 2160],
                                ] as [string, number][]).map(([label, hours]) => (
                                    <button
                                        key={hours}
                                        onClick={() => props.addHours(hours)}
                                        className={`agent-expiry-chip${expiryQuickHours === hours ? ' agent-expiry-chip--selected' : ''}`}
                                        aria-pressed={expiryQuickHours === hours}
                                    >
                                        {label}
                                    </button>
                                ))}
                            </div>
                        </div>
                        <div className="agent-expiry-section">
                            <div className="agent-expiry-label">{t('agent.settings.expiry.customDeadline')}</div>
                            <input
                                type="datetime-local"
                                value={expiryValue}
                                onChange={(event) => {
                                    props.setExpiryValue(event.target.value);
                                    props.setExpiryQuickHours(null);
                                }}
                                className="agent-expiry-input"
                            />
                        </div>
                        <div className="agent-expiry-actions">
                            <button onClick={() => props.saveExpiry(true)} disabled={expirySaving} className="agent-expiry-secondary-action">
                                {t('agent.settings.expiry.neverExpires')}
                            </button>
                            <div className="agent-expiry-action-group">
                                <button onClick={() => props.setShowExpiryModal(false)} disabled={expirySaving} className="agent-expiry-secondary-action">
                                    {t('common.cancel')}
                                </button>
                                <button onClick={() => props.saveExpiry(false)} disabled={expirySaving || !expiryValue} className="agent-expiry-primary-action">
                                    {expirySaving ? t('agent.settings.expiry.saving') : t('common.save')}
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            )}
        </>
    );
}
