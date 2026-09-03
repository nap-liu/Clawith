import { useMemo, useState } from 'react';
import { IconAlertTriangle, IconArrowRight, IconCheck, IconGitMerge } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { Modal, useDialog } from '../../../../components/Dialog/DialogProvider';
import SelectDropdown from '../../../../components/SelectDropdown';
import Avatar from '../../../../components/ui/Avatar';
import Button from '../../../../components/ui/Button';
import { fetchJson } from '../../utils/fetchJson';
import './IdentityConflictReviewModal.css';

type ResolutionAction =
    | 'rebind_source_to_highest_priority'
    | 'merge_users'
    | 'keep_people_separate';

type ConflictUser = {
    reference: string;
    display_name: string;
    avatar_url: string | null;
    title: string | null;
    phone: string | null;
    email: string | null;
    is_active: boolean;
};

type FieldEvidence = {
    field: 'phone' | 'email';
    source_value: string;
    candidate_user: ConflictUser | null;
    is_highest_priority: boolean;
    is_conflicting: boolean;
};

type IdentityConflict = {
    id: string;
    provider_name: string | null;
    source: string;
    masked_identifier: string;
    reason: string;
    status: 'pending' | 'reviewing' | 'reviewed' | 'resolved';
    resolution_action: ResolutionAction | null;
    bound_user: ConflictUser | null;
    merge_candidates: ConflictUser[];
    evidence: FieldEvidence[];
    allowed_actions: ResolutionAction[];
    repair_unavailable_reason: string | null;
    created_at: string;
};

type ConflictList = { items: IdentityConflict[]; total: number };
type ContactField = 'phone' | 'email';
type ContactOption = { value: string; label: string; contactValue: string };

const SOURCE_ACCOUNT_REFERENCE = 'SOURCE_ACCOUNT';

function UserSummary({ user, compact = false }: { user: ConflictUser | null; compact?: boolean }) {
    const { t } = useTranslation();
    if (!user) return <span className="identity-conflict__missing">{t('enterprise.org.identityConflicts.noTenantCandidate')}</span>;
    return (
        <span className="identity-conflict__user-summary">
            <Avatar src={user.avatar_url} name={user.display_name} />
            <span>
                <span className="identity-conflict__user-name">{user.display_name}</span>
                <span className="identity-conflict__reference">{user.reference}</span>
                {!compact && user.title && <span className="identity-conflict__title">{user.title}</span>}
            </span>
            {!compact && (
                <span className="identity-conflict__contacts">
                    {[user.phone, user.email].filter(Boolean).join(' · ') || t('enterprise.org.identityConflicts.noContact')}
                </span>
            )}
            {!user.is_active && <span className="identity-conflict__disabled">{t('enterprise.org.identityConflicts.userDisabled')}</span>}
        </span>
    );
}

function ConflictCard({ conflict }: { conflict: IdentityConflict }) {
    const { t } = useTranslation();
    const dialog = useDialog();
    const queryClient = useQueryClient();
    const recommendedTarget = useMemo(
        () => conflict.evidence.find((item) => item.is_highest_priority)?.candidate_user
            || conflict.merge_candidates.find((item) => item.is_active)
            || null,
        [conflict.evidence, conflict.merge_candidates],
    );
    const activeCandidates = useMemo(
        () => conflict.merge_candidates.filter((candidate) => candidate.is_active),
        [conflict.merge_candidates],
    );
    const contactOptions = (field: ContactField): ContactOption[] => {
        const options = conflict.merge_candidates
            .filter((candidate) => candidate[field])
            .map((candidate) => ({
                value: candidate.reference,
                label: `${candidate[field]} · ${candidate.display_name}`,
                contactValue: candidate[field] || '',
            }));
        const sourceValue = conflict.evidence.find((item) => item.field === field)?.source_value;
        if (sourceValue && !options.some((option) => option.contactValue === sourceValue)) {
            options.unshift({
                value: SOURCE_ACCOUNT_REFERENCE,
                label: `${sourceValue} · ${t('enterprise.org.identityConflicts.sourceAccount')}`,
                contactValue: sourceValue,
            });
        }
        return options;
    };
    const phoneOptions = contactOptions('phone');
    const emailOptions = contactOptions('email');
    const defaultContactSource = (field: ContactField, options: ContactOption[]) => {
        const evidence = conflict.evidence.find((item) => item.field === field);
        return options.find((option) => option.contactValue === evidence?.source_value)?.value
            || options[0]?.value
            || '';
    };
    const [retainedReference, setRetainedReference] = useState(recommendedTarget?.reference || '');
    const [phoneSource, setPhoneSource] = useState(defaultContactSource('phone', phoneOptions));
    const [emailSource, setEmailSource] = useState(defaultContactSource('email', emailOptions));
    const retainedTarget = activeCandidates.find(
        (candidate) => candidate.reference === retainedReference,
    ) || null;
    const candidateOptions = activeCandidates.map((candidate) => ({
        value: candidate.reference,
        label: `${candidate.display_name} · ${candidate.reference}`,
    }));
    const selectedPhone = phoneOptions.find((option) => option.value === phoneSource)?.contactValue || null;
    const selectedEmail = emailOptions.find((option) => option.value === emailSource)?.contactValue || null;
    const mergePreview = retainedTarget && {
        ...retainedTarget,
        phone: selectedPhone,
        email: selectedEmail,
    };
    const resolve = useMutation({
        mutationFn: ({ action, target, fieldSources }: {
            action: ResolutionAction;
            target?: string;
            fieldSources?: Record<string, string>;
        }) => fetchJson<IdentityConflict>(
            `/enterprise/identity-conflicts/${conflict.id}/resolve`,
            {
                method: 'POST',
                body: JSON.stringify({
                    action,
                    target_reference: target,
                    field_sources: fieldSources,
                }),
            },
        ),
        onSuccess: () => queryClient.invalidateQueries({ queryKey: ['identity-conflicts'] }),
    });
    const runAction = async (action: ResolutionAction) => {
        const confirmed = await dialog.confirm(
            t(`enterprise.org.identityConflicts.confirmations.${action}`, {
                name: retainedTarget?.display_name || '',
                phone: selectedPhone || '',
                email: selectedEmail || '',
            }),
            {
                title: t(`enterprise.org.identityConflicts.actions.${action}`),
                confirmLabel: t('enterprise.org.identityConflicts.confirmAction'),
                danger: action === 'merge_users',
            },
        );
        if (confirmed) resolve.mutate({
            action,
            target: action === 'merge_users' ? retainedReference : undefined,
            fieldSources: action === 'merge_users' ? {
                ...(phoneSource ? { phone: phoneSource } : {}),
                ...(emailSource ? { email: emailSource } : {}),
            } : undefined,
        });
    };
    const providerName = conflict.provider_name || t('enterprise.org.identityConflicts.unknownProvider');
    const canMerge = conflict.allowed_actions.includes('merge_users');

    return (
        <article className="identity-conflict" data-status={conflict.status}>
            <header className="identity-conflict__header">
                <div>
                    <div className="identity-conflict__provider">{providerName}</div>
                    <div className="identity-conflict__meta">
                        {t(`enterprise.org.identityConflicts.sources.${conflict.source}`)} · {conflict.masked_identifier}
                        {' · '}{new Date(conflict.created_at).toLocaleString()}
                    </div>
                </div>
                <span className="identity-conflict__status">{t(`enterprise.org.identityConflicts.statuses.${conflict.status}`)}</span>
            </header>

            <div className="identity-conflict__reason">
                <IconAlertTriangle size={16} />
                <span>{t(`enterprise.org.identityConflicts.reasons.${conflict.reason}`)}</span>
            </div>

            {conflict.evidence.length > 0 && (
                <section className="identity-conflict__evidence" aria-label={t('enterprise.org.identityConflicts.evidenceTitle')}>
                    {conflict.evidence.map((entry) => (
                        <div className="identity-conflict__evidence-row" key={entry.field}>
                            <div className="identity-conflict__claim">
                                <span>{t(`enterprise.org.identityConflicts.fields.${entry.field}`)}</span>
                                <strong>{entry.source_value}</strong>
                            </div>
                            <IconArrowRight className="identity-conflict__arrow" size={16} />
                            <UserSummary user={entry.candidate_user} />
                            {entry.is_highest_priority && <span className="identity-conflict__priority">{t('enterprise.org.identityConflicts.highestPriority')}</span>}
                        </div>
                    ))}
                </section>
            )}

            {conflict.status !== 'resolved' && canMerge && (
                <section className="identity-conflict__resolution">
                    <div className="identity-conflict__resolution-heading">
                        <IconGitMerge size={17} />
                        <div>
                            <strong>{t('enterprise.org.identityConflicts.selectRetainedUser')}</strong>
                            <span>{t('enterprise.org.identityConflicts.mergeScope')}</span>
                        </div>
                    </div>
                    <div className="identity-conflict__merge-settings">
                        <label className="identity-conflict__merge-field">
                            <span>{t('enterprise.org.identityConflicts.masterUser')}</span>
                            <small>{t('enterprise.org.identityConflicts.masterUserHint')}</small>
                            <SelectDropdown
                                value={retainedReference}
                                options={candidateOptions}
                                onChange={setRetainedReference}
                                ariaLabel={t('enterprise.org.identityConflicts.masterUser')}
                            />
                        </label>
                        <label className="identity-conflict__merge-field">
                            <span>{t('enterprise.org.identityConflicts.phoneSource')}</span>
                            <small>{t('enterprise.org.identityConflicts.phoneSourceHint')}</small>
                            <SelectDropdown
                                value={phoneSource}
                                options={phoneOptions}
                                onChange={setPhoneSource}
                                ariaLabel={t('enterprise.org.identityConflicts.phoneSource')}
                                disabled={phoneOptions.length === 0}
                            />
                        </label>
                        <label className="identity-conflict__merge-field">
                            <span>{t('enterprise.org.identityConflicts.emailSource')}</span>
                            <small>{t('enterprise.org.identityConflicts.emailSourceHint')}</small>
                            <SelectDropdown
                                value={emailSource}
                                options={emailOptions}
                                onChange={setEmailSource}
                                ariaLabel={t('enterprise.org.identityConflicts.emailSource')}
                                disabled={emailOptions.length === 0}
                            />
                        </label>
                    </div>
                    <div className="identity-conflict__merge-preview">
                        <span>{t('enterprise.org.identityConflicts.mergePreview')}</span>
                        <UserSummary user={mergePreview} />
                    </div>
                    <div className="identity-conflict__actions">
                        <Button
                            type="button"
                            variant="primary"
                            disabled={
                                resolve.isPending
                                || !retainedTarget?.is_active
                                || (phoneOptions.length > 0 && !phoneSource)
                                || (emailOptions.length > 0 && !emailSource)
                            }
                            onClick={() => void runAction('merge_users')}
                        >
                            {resolve.isPending
                                ? t('enterprise.org.identityConflicts.processing')
                                : t('enterprise.org.identityConflicts.mergeInto', { name: retainedTarget?.display_name || '' })}
                        </Button>
                        <Button type="button" variant="secondary" disabled={resolve.isPending} onClick={() => void runAction('rebind_source_to_highest_priority')}>
                            {t('enterprise.org.identityConflicts.actions.rebind_source_to_highest_priority')}
                        </Button>
                        <Button type="button" variant="ghost" disabled={resolve.isPending} onClick={() => void runAction('keep_people_separate')}>
                            {t('enterprise.org.identityConflicts.actions.keep_people_separate')}
                        </Button>
                    </div>
                </section>
            )}

            {conflict.repair_unavailable_reason && (
                <div className="identity-conflict__unavailable">{t(`enterprise.org.identityConflicts.unavailable.${conflict.repair_unavailable_reason}`)}</div>
            )}
            {conflict.status === 'resolved' && conflict.resolution_action && (
                <div className="identity-conflict__resolved">
                    <IconCheck size={15} />
                    {t('enterprise.org.identityConflicts.resolvedWith', { action: t(`enterprise.org.identityConflicts.actions.${conflict.resolution_action}`) })}
                </div>
            )}
            {resolve.isError && (
                <div className="identity-conflict__error">
                    {resolve.error instanceof Error && resolve.error.message.includes('another tenant')
                        ? t('enterprise.org.identityConflicts.crossTenantContact')
                        : t('enterprise.org.identityConflicts.resolveFailed')}
                </div>
            )}
        </article>
    );
}

export default function IdentityConflictReviewModal({ open, providerId, onClose }: {
    open: boolean;
    providerId: string;
    onClose: () => void;
}) {
    const { t } = useTranslation();
    const conflicts = useQuery({
        queryKey: ['identity-conflicts', providerId],
        queryFn: () => fetchJson<ConflictList>(`/enterprise/identity-conflicts?provider_id=${encodeURIComponent(providerId)}&limit=100`),
        enabled: open && !!providerId,
    });

    return (
        <Modal open={open} onClose={onClose} ariaLabelledBy="identity-conflict-review-title" className="identity-conflict-modal">
            <div className="identity-conflict-modal__header">
                <div>
                    <h2 id="identity-conflict-review-title">{t('enterprise.org.identityConflicts.title')}</h2>
                    <p>{t('enterprise.org.identityConflicts.safeReviewHint')}</p>
                </div>
                <Button type="button" variant="ghost" className="btn-sm" onClick={onClose}>{t('common.close')}</Button>
            </div>
            {conflicts.isLoading && <div className="identity-conflict-modal__state">{t('common.loading')}</div>}
            {conflicts.isError && <div className="identity-conflict-modal__state identity-conflict__error">{t('enterprise.org.identityConflicts.loadFailed')}</div>}
            {conflicts.data?.items.length === 0 && <div className="identity-conflict-modal__state">{t('enterprise.org.identityConflicts.empty')}</div>}
            <div className="identity-conflict-modal__list">
                {conflicts.data?.items.map((conflict) => <ConflictCard key={conflict.id} conflict={conflict} />)}
            </div>
        </Modal>
    );
}
