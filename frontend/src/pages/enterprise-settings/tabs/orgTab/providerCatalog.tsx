import type { TFunction } from 'i18next';
import type React from 'react';

export type ProviderKind = {
    type: string;
    name: string;
    desc: string;
    icon: React.ReactNode;
};

export type ProviderEntry = {
    idp: ProviderKind;
    existingProvider: any | null;
    key: string;
};

function providerKinds(t: TFunction): ProviderKind[] {
    return [
        { type: 'feishu', name: t('enterprise.identity.providers.feishu.name'), desc: t('enterprise.identity.providers.feishu.desc'), icon: <img src="/feishu.png" width="20" height="20" alt="Feishu" /> },
        { type: 'wecom', name: t('enterprise.identity.providers.wecom.name'), desc: t('enterprise.identity.providers.wecom.desc'), icon: <img src="/wecom.png" width="20" height="20" style={{ borderRadius: '4px' }} alt="WeCom" /> },
        { type: 'dingtalk', name: t('enterprise.identity.providers.dingtalk.name'), desc: t('enterprise.identity.providers.dingtalk.desc'), icon: <img src="/dingtalk.png" width="20" height="20" style={{ borderRadius: '4px' }} alt="DingTalk" /> },
        { type: 'google_workspace', name: t('enterprise.identity.providers.google_workspace.name'), desc: t('enterprise.identity.providers.google_workspace.desc'), icon: <img src="/google.svg" width="20" height="20" alt="Google" /> },
        { type: 'oauth2', name: t('enterprise.identity.providers.oauth2.name'), desc: t('enterprise.identity.providers.oauth2.desc'), icon: <div style={{ width: 20, height: 20, background: 'var(--accent-primary)', borderRadius: 4, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#fff', fontSize: 10, fontWeight: 700 }}>O</div> },
    ];
}

export function buildProviderEntries(providers: any[], t: TFunction): ProviderEntry[] {
    const kinds = providerKinds(t);
    return kinds.map(idp => {
        const existingProvider = providers.find(provider => provider.provider_type === idp.type) || null;
        return {
            idp,
            existingProvider,
            key: existingProvider?.id || `new:${idp.type}`,
        };
    });
}

export function hasDirectoryCapability(provider: any): boolean {
    if (['feishu', 'wecom', 'dingtalk', 'google_workspace', 'scim'].includes(provider.provider_type)) return true;
    const config = provider.config || {};
    return Boolean(config.directory_protocol || config.capabilities?.directory_protocol);
}
