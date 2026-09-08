export type ApplicationConfig = {
    name: string;
    enabled: boolean;
    trust_user_identity: boolean;
    scopes: string[];
    embed_origins: string[];
    redirect_origins: string[];
    rate_limit_per_minute: number;
    expires_at: string | null;
};

export type Application = ApplicationConfig & {
    id: string;
    client_id: string;
    tenant_id: string;
    revoked_at: string | null;
    created_at: string;
    client_secret?: string;
};

export type AuditEntry = {
    id: string;
    action: string;
    created_at: string;
    details: { outcome?: string; request_id?: string };
};

export const applicationPath = '/enterprise/openapi/applications';

export const emptyConfig = (): ApplicationConfig => ({
    name: '', enabled: true, trust_user_identity: false,
    scopes: ['employees:read', 'auth:login'],
    embed_origins: [], redirect_origins: [],
    rate_limit_per_minute: 120, expires_at: null,
});

export function configOf(item: Application): ApplicationConfig {
    return {
        name: item.name,
        enabled: item.enabled,
        trust_user_identity: item.trust_user_identity,
        scopes: item.scopes,
        embed_origins: item.embed_origins,
        redirect_origins: item.redirect_origins,
        rate_limit_per_minute: item.rate_limit_per_minute,
        expires_at: item.expires_at,
    };
}
