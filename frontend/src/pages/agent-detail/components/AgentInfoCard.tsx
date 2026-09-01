export default function AgentInfoCard({
    infoCardOpen,
    t,
    agent,
    formatAgentDate,
    expiryLabel,
    canManage,
    openExpiryModal,
    modelLabel,
    modelProvider,
    todayParts,
    monthParts,
    totalParts,
    formatTokens,
    cacheReadToday,
    cacheHitRateToday,
}: any) {
    return (
        <div className={`agent-info-card${infoCardOpen ? ' agent-info-card--open' : ''}`}>
            <div className="agent-info-card-inner">
                <div className="agent-info-card-glow" />
                <div className="agent-info-card-grid">
                    <div className="agent-info-card-section">
                        <div className="agent-info-card-section-header">
                            <span className="agent-info-section-icon agent-info-section-icon--indigo">
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="8" r="4" /><path d="M20 21a8 8 0 0 0-16 0" /></svg>
                            </span>
                            <span className="agent-info-card-section-title">{t('agent.profile.title', 'Agent Profile')}</span>
                        </div>
                        <div className="agent-info-card-body">
                            <div className="agent-info-profile-panel">
                                {agent.role_description && (
                                    <div className="agent-info-profile-role" title={agent.role_description}>{agent.role_description}</div>
                                )}
                                <div className="agent-info-meta-list agent-info-profile-meta">
                                    <div className="agent-info-meta-row">
                                        <span>{t('agent.profile.created')}</span>
                                        <span>{formatAgentDate(agent.created_at)}</span>
                                    </div>
                                    <div className="agent-info-meta-row">
                                        <span>{t('agent.fields.createdBy', 'Created by')}</span>
                                        <span>{(agent as any).creator_username ? `@${(agent as any).creator_username}` : '—'}</span>
                                    </div>
                                    <div className="agent-info-meta-row">
                                        <span>{t('agent.profile.timezone')}</span>
                                        <span>{(agent as any).effective_timezone || agent.timezone || 'UTC'}</span>
                                    </div>
                                    <div className="agent-info-meta-row">
                                        <span>{t('agent.settings.expiry.title')}</span>
                                        <span className={(agent as any).is_expired ? 'agent-info-expiry--expired' : ''}>{expiryLabel}</span>
                                    </div>
                                </div>
                                {canManage && (
                                    <button
                                        type="button"
                                        className="agent-info-expiry-button"
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            openExpiryModal();
                                        }}
                                    >
                                        {t('agent.settings.expiry.title')}
                                    </button>
                                )}
                            </div>
                        </div>
                    </div>
                    <div className="agent-info-card-section agent-info-card-section--stacked">
                        <div className="agent-info-subsection">
                            <div className="agent-info-card-section-header">
                                <span className="agent-info-section-icon agent-info-section-icon--indigo">
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" /><circle cx="12" cy="12" r="3" /></svg>
                                </span>
                                <span className="agent-info-card-section-title">{t('agent.modelConfig.title', 'Configuration')}</span>
                            </div>
                            <div className="agent-info-card-body agent-info-card-body--compact">
                                <div className="agent-info-model-card">
                                    <div className="agent-info-model-card-text">
                                        <span className="agent-info-model-card-label">{t('agent.modelConfig.model')}</span>
                                        <span className="agent-info-model-card-name" title={modelLabel}>{modelLabel}</span>
                                    </div>
                                </div>
                                <div className="agent-info-meta-list">
                                    <div className="agent-info-meta-row">
                                        <span>{t('agent.modelConfig.provider', 'Provider')}</span>
                                        <span>{modelProvider}</span>
                                    </div>
                                </div>
                            </div>
                        </div>
                        <div className="agent-info-subsection">
                            <div className="agent-info-card-section-header">
                                <span className="agent-info-section-icon agent-info-section-icon--blue">
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10" /><line x1="12" y1="20" x2="12" y2="4" /><line x1="6" y1="20" x2="6" y2="14" /></svg>
                                </span>
                                <span className="agent-info-card-section-title">Token</span>
                            </div>
                            <div className="agent-info-card-body agent-info-card-body--compact">
                                <div className="agent-info-token-glass">
                                    <div className="agent-info-token-hero">
                                        <span className="agent-info-token-hero-label">{t('agent.settings.today')}</span>
                                        <span className="agent-info-token-hero-value">
                                            {todayParts.value}
                                            {todayParts.unit && <span className="agent-info-token-hero-unit">{todayParts.unit}</span>}
                                        </span>
                                    </div>
                                    <div className="agent-info-token-stats">
                                        <div className="agent-info-stat-item">
                                            <span className="agent-info-stat-label">{t('agent.settings.month')}</span>
                                            <span className="agent-info-stat-value">
                                                {monthParts.value}
                                                {monthParts.unit && <span className="agent-info-stat-unit">{monthParts.unit}</span>}
                                            </span>
                                        </div>
                                        <div className="agent-info-stat-item">
                                            <span className="agent-info-stat-label">Cache</span>
                                            <span className="agent-info-stat-value" title={`Today cache hit: ${formatTokens(cacheReadToday)} · ${cacheHitRateToday}%`}>
                                                {formatTokens(cacheReadToday)}
                                                <span className="agent-info-stat-unit">{cacheHitRateToday}%</span>
                                            </span>
                                        </div>
                                        <div className="agent-info-stat-item">
                                            <span className="agent-info-stat-label">{t('agent.status.totalToken')}</span>
                                            <span className="agent-info-stat-value">
                                                {totalParts.value}
                                                {totalParts.unit && <span className="agent-info-stat-unit">{totalParts.unit}</span>}
                                            </span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
