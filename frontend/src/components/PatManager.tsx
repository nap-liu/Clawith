import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconCopy, IconCheck, IconTrash, IconPlus, IconX } from '@tabler/icons-react';
import { patApi, type Pat, type PatCreated } from '../services/api';
import { useToast } from './Toast/ToastProvider';

/* ── helpers ───────────────────────────────────────────── */

function fmtDate(iso: string | null): string {
    if (!iso) return '';
    return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

// Robust copy: the async Clipboard API only works in a secure context
// (HTTPS or localhost). Over plain http on a custom host it throws, so fall
// back to the legacy execCommand path which works there too.
async function copyText(text: string): Promise<boolean> {
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch { /* fall through to legacy path */ }
    try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        ta.setAttribute('readonly', '');
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
    } catch {
        return false;
    }
}

function CopyButton({ text, label }: { text: string; label: string }) {
    const [copied, setCopied] = useState(false);
    const { t } = useTranslation();
    const toast = useToast();
    const handleCopy = async () => {
        if (!(await copyText(text))) { toast.error(t('pat.copyFailed')); return; }
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
    };
    return (
        <button
            type="button"
            onClick={handleCopy}
            title={label}
            style={{
                display: 'inline-flex', alignItems: 'center', gap: '4px',
                padding: '4px 10px', fontSize: '11px', borderRadius: '5px',
                border: '1px solid var(--border-subtle)',
                background: copied ? 'rgba(0,180,120,0.1)' : 'var(--bg-secondary)',
                color: copied ? 'var(--success)' : 'var(--text-secondary)',
                cursor: 'pointer', whiteSpace: 'nowrap',
            }}
        >
            {copied ? <IconCheck size={12} stroke={2.4} /> : <IconCopy size={12} stroke={1.75} />}
            {copied ? t('pat.copied') : label}
        </button>
    );
}

/* ── config snippets ───────────────────────────────────── */

function ConfigSnippets({ token }: { token: string | null }) {
    const { t } = useTranslation();
    const host = window.location.origin;
    const mcpUrl = `${host}/mcp/`;
    const tok = token ?? '<your-token>';

    const claudeSnippet = `claude mcp add --transport http clawith ${mcpUrl} --header "Authorization: Bearer ${tok}"`;
    const codexSnippet = `[mcp_servers.clawith]
transport = "http"
url = "${mcpUrl}"

[mcp_servers.clawith.headers]
Authorization = "Bearer ${tok}"`;
    const stdioSnippet = `npx mcp-remote ${mcpUrl} --header "Authorization: Bearer ${tok}"`;

    const desc = token ? t('pat.configDesc', { TOKEN: tok }) : t('pat.configDescStatic');

    return (
        <div style={{ marginTop: '16px' }}>
            <h4 style={{ margin: '0 0 6px', fontSize: '13px', color: 'var(--text-secondary)' }}>{t('pat.configTitle')}</h4>
            <p style={{ margin: '0 0 12px', fontSize: '12px', color: 'var(--text-tertiary)', lineHeight: 1.5 }}>{desc}</p>

            <SnippetBlock label={t('pat.claudeCode')} code={claudeSnippet} lang="shell" />
            <SnippetBlock label={t('pat.codex')} code={codexSnippet} lang="toml" />
            <SnippetBlock label={t('pat.stdioFallback')} code={stdioSnippet} lang="shell" />
        </div>
    );
}

function SnippetBlock({ label, code, lang }: { label: string; code: string; lang: string }) {
    const { t } = useTranslation();
    const toast = useToast();
    const [copied, setCopied] = useState(false);
    const handleCopy = async () => {
        if (!(await copyText(code))) { toast.error(t('pat.copyFailed')); return; }
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
    };
    return (
        <div style={{ marginBottom: '12px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '4px' }}>
                <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontWeight: 500 }}>{label}</span>
                <button
                    type="button"
                    onClick={handleCopy}
                    style={{
                        display: 'inline-flex', alignItems: 'center', gap: '4px',
                        padding: '2px 8px', fontSize: '11px', borderRadius: '4px',
                        border: '1px solid var(--border-subtle)',
                        background: copied ? 'rgba(0,180,120,0.1)' : 'var(--bg-secondary)',
                        color: copied ? 'var(--success)' : 'var(--text-tertiary)',
                        cursor: 'pointer',
                    }}
                >
                    {copied ? <IconCheck size={11} stroke={2.4} /> : <IconCopy size={11} stroke={1.75} />}
                    {copied ? t('pat.snippetCopied') : t('pat.copySnippet')}
                </button>
            </div>
            <pre
                data-lang={lang}
                style={{
                    margin: 0, padding: '10px 12px',
                    background: 'var(--bg-tertiary)',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: '6px',
                    fontSize: '11px', lineHeight: 1.6,
                    color: 'var(--text-primary)',
                    fontFamily: 'var(--font-mono, monospace)',
                    overflowX: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                }}
            >{code}</pre>
        </div>
    );
}

/* ── create modal ──────────────────────────────────────── */

interface CreateModalProps {
    onClose: () => void;
    onCreated: (pat: PatCreated) => void;
}

function CreateModal({ onClose, onCreated }: CreateModalProps) {
    const { t } = useTranslation();
    const toast = useToast();
    const [name, setName] = useState('');
    const [expiresAt, setExpiresAt] = useState('');

    const mutation = useMutation({
        mutationFn: () => patApi.create({ name: name.trim(), ...(expiresAt ? { expires_at: new Date(expiresAt).toISOString() } : {}) }),
        onSuccess: (data) => {
            toast.success(t('pat.tokenCreated'));
            onCreated(data);
        },
        onError: (e: any) => toast.error(e.message || t('pat.actionFailed')),
    });

    const labelStyle: React.CSSProperties = { display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px', color: 'var(--text-secondary)' };
    const inputStyle: React.CSSProperties = { width: '100%', fontSize: '13px', boxSizing: 'border-box' };

    return (
        <div
            style={{ position: 'fixed', inset: 0, zIndex: 10001, background: 'rgba(0,0,0,0.55)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
            onClick={onClose}
        >
            <div
                style={{ background: 'var(--bg-primary)', borderRadius: '12px', border: '1px solid var(--border-subtle)', width: '380px', padding: '24px', boxShadow: '0 20px 60px rgba(0,0,0,0.35)' }}
                onClick={e => e.stopPropagation()}
            >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
                    <h3 style={{ margin: 0, fontSize: '15px' }}>{t('pat.newToken')}</h3>
                    <button onClick={onClose} aria-label={t('pat.close')} style={{ background: 'none', border: 'none', color: 'var(--text-tertiary)', fontSize: '18px', cursor: 'pointer', padding: '2px 6px' }}>
                        <IconX size={16} stroke={1.75} />
                    </button>
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                    <div>
                        <label style={labelStyle}>{t('pat.name')}</label>
                        <input
                            className="form-input"
                            style={inputStyle}
                            placeholder={t('pat.namePlaceholder')}
                            value={name}
                            onChange={e => setName(e.target.value)}
                            autoFocus
                        />
                    </div>
                    <div>
                        <label style={labelStyle}>{t('pat.expiresAt')}</label>
                        <input
                            className="form-input"
                            style={inputStyle}
                            type="date"
                            value={expiresAt}
                            onChange={e => setExpiresAt(e.target.value)}
                            min={new Date().toISOString().slice(0, 10)}
                        />
                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('pat.expiresAtHint')}</div>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '4px' }}>
                        <button
                            className="btn btn-primary"
                            style={{ padding: '6px 18px', fontSize: '12px' }}
                            disabled={!name.trim() || mutation.isPending}
                            onClick={() => mutation.mutate()}
                        >
                            {mutation.isPending ? t('pat.creating') : t('pat.create')}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}

/* ── reveal modal (shown after create) ────────────────── */

interface RevealModalProps {
    pat: PatCreated;
    onClose: () => void;
}

function RevealModal({ pat, onClose }: RevealModalProps) {
    const { t } = useTranslation();
    return (
        <div
            style={{ position: 'fixed', inset: 0, zIndex: 10002, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
            onClick={onClose}
        >
            <div
                style={{ background: 'var(--bg-primary)', borderRadius: '12px', border: '1px solid var(--border-subtle)', width: '460px', padding: '24px', boxShadow: '0 20px 60px rgba(0,0,0,0.4)' }}
                onClick={e => e.stopPropagation()}
            >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                    <h3 style={{ margin: 0, fontSize: '15px' }}>{t('pat.tokenCreated')}</h3>
                    <button onClick={onClose} aria-label={t('pat.close')} style={{ background: 'none', border: 'none', color: 'var(--text-tertiary)', cursor: 'pointer', padding: '2px 6px' }}>
                        <IconX size={16} stroke={1.75} />
                    </button>
                </div>

                <div style={{
                    padding: '10px 14px', marginBottom: '12px',
                    background: 'rgba(255,170,0,0.1)', border: '1px solid rgba(255,170,0,0.3)',
                    borderRadius: '7px', fontSize: '12px', color: 'var(--warning)', lineHeight: 1.5,
                }}>
                    ⚠ {t('pat.onceWarning')}
                </div>

                <div style={{ marginBottom: '16px' }}>
                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px', fontWeight: 500 }}>{pat.name}</div>
                    <div style={{
                        display: 'flex', alignItems: 'center', gap: '8px',
                        padding: '10px 12px',
                        background: 'var(--bg-tertiary)',
                        border: '1px solid var(--border-subtle)',
                        borderRadius: '7px',
                    }}>
                        <code style={{ flex: 1, fontSize: '12px', fontFamily: 'var(--font-mono, monospace)', wordBreak: 'break-all', color: 'var(--text-primary)' }}>
                            {pat.token}
                        </code>
                        <CopyButton text={pat.token} label={t('pat.copyToken')} />
                    </div>
                </div>

                <ConfigSnippets token={pat.token} />

                <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '16px' }}>
                    <button className="btn btn-primary" style={{ padding: '6px 18px', fontSize: '12px' }} onClick={onClose}>
                        {t('pat.close')}
                    </button>
                </div>
            </div>
        </div>
    );
}

/* ── main component ────────────────────────────────────── */

export default function PatManager() {
    const { t } = useTranslation();
    const toast = useToast();
    const queryClient = useQueryClient();
    const [showCreate, setShowCreate] = useState(false);
    const [revealed, setRevealed] = useState<PatCreated | null>(null);

    const { data: pats = [], isLoading } = useQuery<Pat[]>({
        queryKey: ['pats'],
        queryFn: patApi.list,
    });

    const revokeMutation = useMutation({
        mutationFn: (id: string) => patApi.revoke(id),
        onSuccess: () => {
            toast.success(t('pat.tokenRevoked'));
            queryClient.invalidateQueries({ queryKey: ['pats'] });
        },
        onError: (e: any) => toast.error(e.message || t('pat.actionFailed')),
    });

    const handleRevoke = (pat: Pat) => {
        if (!window.confirm(t('pat.revokeConfirm'))) return;
        revokeMutation.mutate(pat.id);
    };

    const handleCreated = (pat: PatCreated) => {
        setShowCreate(false);
        setRevealed(pat);
        queryClient.invalidateQueries({ queryKey: ['pats'] });
    };

    const labelStyle: React.CSSProperties = { fontSize: '12px', color: 'var(--text-secondary)' };

    return (
        <>
            <div style={{ marginTop: '8px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '8px' }}>
                    <div>
                        <h4 style={{ margin: '0 0 4px', fontSize: '13px', color: 'var(--text-secondary)' }}>{t('pat.sectionTitle')}</h4>
                        <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-tertiary)', lineHeight: 1.5 }}>{t('pat.sectionDesc')}</p>
                    </div>
                    <button
                        className="btn btn-primary"
                        style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', padding: '5px 12px', fontSize: '12px', whiteSpace: 'nowrap', marginLeft: '12px', flexShrink: 0 }}
                        onClick={() => setShowCreate(true)}
                    >
                        <IconPlus size={13} stroke={2} />
                        {t('pat.newToken')}
                    </button>
                </div>

                {isLoading ? (
                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', padding: '12px 0' }}>...</div>
                ) : pats.length === 0 ? (
                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', padding: '12px 0' }}>{t('pat.noTokens')}</div>
                ) : (
                    <div style={{ overflowX: 'auto' }}>
                        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
                            <thead>
                                <tr style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                                    {[t('pat.colName'), t('pat.colPrefix'), t('pat.colCreated'), t('pat.colLastUsed'), t('pat.colExpires'), ''].map((h, i) => (
                                        <th key={i} style={{ ...labelStyle, textAlign: 'left', padding: '6px 8px 6px 0', fontWeight: 600, whiteSpace: 'nowrap' }}>{h}</th>
                                    ))}
                                </tr>
                            </thead>
                            <tbody>
                                {pats.map(pat => (
                                    <tr key={pat.id} style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                                        <td style={{ padding: '8px 8px 8px 0', color: 'var(--text-primary)', fontWeight: 500 }}>{pat.name}</td>
                                        <td style={{ padding: '8px 8px 8px 0' }}>
                                            <code style={{ fontFamily: 'var(--font-mono, monospace)', color: 'var(--text-secondary)', fontSize: '11px' }}>{pat.token_prefix}…</code>
                                        </td>
                                        <td style={{ padding: '8px 8px 8px 0', color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>{fmtDate(pat.created_at)}</td>
                                        <td style={{ padding: '8px 8px 8px 0', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>{pat.last_used_at ? fmtDate(pat.last_used_at) : '—'}</td>
                                        <td style={{ padding: '8px 8px 8px 0', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>{pat.expires_at ? fmtDate(pat.expires_at) : t('pat.never')}</td>
                                        <td style={{ padding: '8px 0', textAlign: 'right' }}>
                                            <button
                                                type="button"
                                                title={t('pat.revoke')}
                                                disabled={revokeMutation.isPending}
                                                onClick={() => handleRevoke(pat)}
                                                style={{
                                                    background: 'none', border: 'none', cursor: 'pointer',
                                                    color: 'var(--error)', padding: '3px 6px', borderRadius: '4px',
                                                    display: 'inline-flex', alignItems: 'center', gap: '3px', fontSize: '11px',
                                                }}
                                            >
                                                <IconTrash size={13} stroke={1.75} />
                                                {t('pat.revoke')}
                                            </button>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}

                {/* Static config block when no fresh token is available */}
                {!revealed && <ConfigSnippets token={null} />}
            </div>

            {showCreate && (
                <CreateModal onClose={() => setShowCreate(false)} onCreated={handleCreated} />
            )}

            {revealed && (
                <RevealModal pat={revealed} onClose={() => setRevealed(null)} />
            )}
        </>
    );
}
